"""Дашборд: CRM-слой поверх лидов (matches) — HTTP API плюс серверный рендер.

Отдельный процесс и отдельный энтрипоинт — не оформление, а требование.
Бот и воркеры это долгоживущие циклы без входящих соединений; дашборд —
единственная часть системы, которая слушает порт и принимает запросы снаружи.
Держать их в одном процессе значило бы, что дыра в веб-слое достаёт до
Telegram-токена и до пайплайна, а падение веб-слоя останавливает приём лидов.

Что здесь есть и чего нет:
  * читает matches и raw_posts — только на чтение, ни одного UPDATE;
  * пишет исключительно в lead_crm (см. миграцию 0004);
  * Match.status не трогает вообще: это статус доставки в Telegram, его ведёт бот.

Модель данных: отсутствие строки в lead_crm = «не написал». Строка заводится
лениво первым CRM-действием через INSERT ... ON CONFLICT (match_id) DO UPDATE,
удаление — мягкое (deleted_at), строки matches не удаляются никогда.

Два представления одних и тех же роутов
---------------------------------------
Фронтенд написан на htmx: он ждёт от тех же URL готовые куски HTML, а не JSON.
Заводить ради этого вторую параллельную ветку роутов (/ui/leads рядом с
/api/leads) означало бы дублировать всю выборку и все проверки доступа, и рано
или поздно две ветки разъехались бы. Поэтому представление выбирается по
заголовку HX-Request, который htmx ставит сам:

    HX-Request: true  -> HTML-фрагмент (шаблоны app/templates/dashboard/)
    без заголовка     -> JSON, как и раньше

Выбор представления НИКОГДА не влияет на права: и то и другое идёт через
require_session, заголовок управляет только формой ответа. Ответы помечаются
`Vary: HX-Request`, чтобы промежуточный кэш не отдал HTML JSON-клиенту.

Тела запросов принимаются и как JSON, и как form-urlencoded: htmx по умолчанию
кодирует всё, кроме GET, в форму, а JSON-клиентам ломать контракт незачем.

Запуск:
    python -m app.dashboard
    # или: uvicorn app.dashboard:app --host 0.0.0.0 --port 8080
"""
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal, TypeVar

import structlog
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path as PathParam, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError, field_validator
from slowapi.errors import RateLimitExceeded
from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.auth.dependency import COOKIE_NAME, require_session
from app.auth.origin_guard import OriginGuardMiddleware
from app.auth.rate_limit import limiter
from app.auth.router import rate_limit_handler
from app.auth.router import router as auth_router
from app.auth.security_headers import SecurityHeadersMiddleware
from app.auth.session import touch_session
from app.config import require, settings
from app.db import SessionLocal
from app.logging_setup import configure_logging
from app.models import LeadCrm, Match, RawItem

log = structlog.get_logger(__name__)

LeadFilter = Literal["all", "not_contacted", "contacted", "postponed", "in_work", "no_deal"]

PAGE_SIZE = 30  # столько же, сколько по умолчанию ждёт index.html

_APP_DIR = Path(__file__).resolve().parent
# Корень загрузчика — app/templates, а НЕ app/templates/dashboard: сами шаблоны
# ссылаются друг на друга как "dashboard/base.html" и "dashboard/partials/...",
# и при более узком корне ни один extends/include не разрезолвился бы.
TEMPLATES_DIR = _APP_DIR / "templates"
# А вот статика монтируется именно узко: наружу отдаётся только каталог дашборда,
# а не всё app/static целиком.
STATIC_DIR = _APP_DIR / "static" / "dashboard"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Состояние контакта, как его видит дашборд: нет строки -> «не написал».
_STATE = func.coalesce(LeadCrm.contact_state, "not_contacted")
# Мягко удалённый лид скрыт из ВСЕХ списков. Условие работает и для отсутствующей
# строки: у неё deleted_at тоже NULL. Класть его в ON-часть LEFT JOIN нельзя —
# тогда удалённый лид не отфильтровался бы, а вернулся в список как «не написал».
_VISIBLE = LeadCrm.deleted_at.is_(None)

_FILTERS: dict[str, ColumnElement[bool] | None] = {
    "all": None,
    "not_contacted": _STATE == "not_contacted",
    "contacted": _STATE == "contacted",
    "postponed": _STATE == "postponed",
    # taken_in_work: NULL = ответа/решения ещё нет, поэтому IS TRUE / IS FALSE,
    # а не == True / != True — NULL не должен попасть ни в одну из двух корзин.
    "in_work": LeadCrm.taken_in_work.is_(True),
    "no_deal": LeadCrm.taken_in_work.is_(False),
}


# --------------------------------------------------------------------------- схемы


class LeadListItem(BaseModel):
    id: int
    headline: str | None
    score: int
    intent_tag: str
    source: str  # raw_posts.subreddit — источник, из которого пришла запись
    circuit: str
    created_at: datetime
    contact_state: str
    taken_in_work: bool | None
    # Decimal, а не float: суммы сделок. В JSON уезжает строкой — это сознательно,
    # float в JS не умеет хранить деньги без потерь.
    earnings: Decimal | None
    currency: str | None


class LeadListResponse(BaseModel):
    items: list[LeadListItem]
    total: int  # всего подходящих под фильтр, не длина страницы
    limit: int
    offset: int


class LeadCrmOut(BaseModel):
    """CRM-состояние лида. None целиком — CRM-строки ещё нет («не написал»)."""

    contact_state: str
    # В БД колонка называется reply_text (суть ответа клиента); наружу отдаётся
    # как response_text — так её назвал контракт API. Переименовывать колонку
    # на живой таблице ради совпадения имён смысла нет.
    response_text: str | None
    taken_in_work: bool | None
    earnings: Decimal | None
    currency: str | None
    contacted_at: datetime | None
    in_work_at: datetime | None
    updated_at: datetime | None


class RawItemOut(BaseModel):
    id: int
    source: str
    author_handle: str | None
    url: str | None  # raw_posts.permalink
    title: str
    text: str
    posted_at: datetime


class LeadDetail(BaseModel):
    id: int
    circuit: str
    score: int
    intent_tag: str
    headline: str | None
    signals: dict
    stack_matched: list[str]
    compensation: str | None
    contact: str | None
    decided_by: str
    llm_summary_ru: str | None
    llm_prob: float | None
    llm_status: str
    duplicate_count: int
    is_favorite: bool
    created_at: datetime
    raw_item: RawItemOut
    crm: LeadCrmOut | None


class ContactPatch(BaseModel):
    # not_contacted намеренно не принимается: это состояние по умолчанию, в него
    # не «переводят». Сбросить лид в исходное — это DELETE (мягкое удаление).
    contact_state: Literal["contacted", "postponed"]


class ResponsePatch(BaseModel):
    response_text: str = Field(min_length=1, max_length=10_000)
    # Три состояния, а не два. NULL в колонке означает «ответ есть, решения пока
    # нет», и форма в partials/lead_detail.html даёт ровно этот третий вариант
    # («Пока не знаю»). Радио-кнопка присылает его строкой "null".
    # Поле обязательное (дефолта нет): пропущенное значение — это опечатка
    # клиента, а не осознанное «не знаю», и молча трактовать одно как другое
    # значит терять разницу между ними.
    taken_in_work: bool | None

    @field_validator("taken_in_work", mode="before")
    @classmethod
    def _tri_state(cls, value: Any) -> Any:
        # Форма шлёт строки; пустая строка и "null" — это «не знаю», а не False.
        if isinstance(value, str) and value.strip().lower() in {"", "null", "none"}:
            return None
        return value


class EarningsPatch(BaseModel):
    # Границы совпадают с CHECK-ограничениями таблицы (Numeric(12,2), >= 0):
    # 422 с внятным телом лучше, чем 500 от нарушения CHECK в Postgres.
    earnings: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class EarningsByCurrency(BaseModel):
    currency: str
    amount: Decimal


class StatsResponse(BaseModel):
    total_leads: int
    not_contacted: int
    contacted: int
    postponed: int
    in_work: int
    no_deal: int
    removed: int
    # Всегда список, даже при одной валюте: форма ответа не должна зависеть
    # от данных, иначе клиенту приходится разбирать два разных формата.
    # Складывать разные валюты в одно число нельзя без курса на дату сделки.
    earnings_total: list[EarningsByCurrency]


# ----------------------------------------------------------------------- инфраструктура


async def db_session() -> AsyncIterator[AsyncSession]:
    """Сессия на запрос. Переиспользует app/db.py — своего engine здесь нет.

    Оговорка про пересоздание engine при смене event loop (см. app/db.py) для
    uvicorn неактуальна: цикл здесь один на всё время жизни процесса.
    """
    async with SessionLocal() as session:
        yield session


Db = Annotated[AsyncSession, Depends(db_session)]

ModelT = TypeVar("ModelT", bound=BaseModel)


def wants_html(request: Request) -> bool:
    """htmx ли это. Заголовок ставит сам htmx на каждый свой запрос.

    Подделать заголовок может кто угодно, и это ничего не даёт: он выбирает
    только формат ответа, а не права на него — данные в обоих представлениях
    одни и те же и одинаково закрыты require_session.
    """
    return request.headers.get("hx-request", "").lower() == "true"


async def parse_body(request: Request, model: type[ModelT]) -> ModelT:
    """Тело запроса как JSON или как form-urlencoded — в одну и ту же модель.

    htmx кодирует всё, кроме GET, в форму (это его поведение по умолчанию,
    без расширения json-enc), а JSON-клиентам ломать контракт незачем.
    Ошибки валидации переупаковываются в RequestValidationError, чтобы 422
    выглядел ровно так же, как у остальных роутов FastAPI.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            raw: Any = await request.json()
        except ValueError:
            raise RequestValidationError(
                [{"loc": ("body",), "msg": "Тело не является корректным JSON", "type": "value_error"}]
            ) from None
    else:
        raw = dict(await request.form())

    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _body_doc(model: type[BaseModel]) -> dict:
    """Схема тела для OpenAPI.

    Нужна руками: тело разбирается в parse_body, а не объявлено параметром
    роута, поэтому сам FastAPI его не задокументирует. Без этого схема
    молча потеряла бы описание тела у трёх PATCH-роутов.
    """
    schema = model.model_json_schema()
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": schema},
                "application/x-www-form-urlencoded": {"schema": schema},
            },
        }
    }


def render(name: str, **context: Any) -> str:
    """Шаблон в строку. Автоэкранирование включено (Jinja2Templates по умолчанию) —
    тексты лидов приходят из чужих постов и в HTML не доверяются."""
    return templates.env.get_template(name).render(**context)


def html(*fragments: str, status_code: int = 200) -> HTMLResponse:
    """Склейка основного фрагмента с out-of-band блоками в один ответ.

    htmx вынимает из ответа всё, что помечено hx-swap-oob, кладёт по своим id,
    а остаток отправляет в hx-target. Так один PATCH обновляет и строку лида,
    и счётчики в шапке — без второго запроса и без рассинхрона между ними.
    """
    response = HTMLResponse("".join(fragments), status_code=status_code)
    response.headers["Vary"] = "HX-Request"
    return response


def _oob(element_id: str, inner: str) -> str:
    return f'<div id="{element_id}" hx-swap-oob="true">{inner}</div>'


def _crm_out(row: LeadCrm | None) -> LeadCrmOut | None:
    if row is None:
        return None
    return LeadCrmOut(
        contact_state=row.contact_state,
        response_text=row.reply_text,
        taken_in_work=row.taken_in_work,
        earnings=row.earnings,
        currency=row.currency,
        contacted_at=row.contacted_at,
        in_work_at=row.in_work_at,
        updated_at=row.updated_at,
    )


async def _require_match(session: AsyncSession, lead_id: int) -> None:
    """404, если лида нет вовсе или он мягко удалён.

    Удалённый отдаёт именно 404, а не 410/403: для клиента дашборда его не
    существует, и «удалён» — это уже лишнее знание о чужой строке.
    """
    exists = await session.scalar(
        select(Match.id)
        .outerjoin(LeadCrm, LeadCrm.match_id == Match.id)
        .where(Match.id == lead_id, _VISIBLE)
    )
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Лид не найден")


async def _lock_crm(session: AsyncSession, lead_id: int) -> LeadCrm | None:
    """CRM-строка под блокировкой до конца транзакции.

    SELECT ... FOR UPDATE здесь не перестраховка. /response и /earnings сначала
    читают состояние (взят ли в работу, написан ли), потом пишут; без блокировки
    два одновременных запроса оба прочитают старое значение и второй затрёт
    решение первого — классическая гонка read-modify-write. Блокировка строки
    выстраивает их в очередь.

    noload обязателен, а не оптимизация. У LeadCrm.match стоит lazy="joined",
    поэтому голый select(LeadCrm) подтягивает LEFT JOIN на matches и raw_posts,
    а Postgres отказывается брать FOR UPDATE на nullable-стороне внешнего
    соединения — запрос падал бы целиком. Сама связь здесь и не нужна.
    """
    return await session.scalar(
        select(LeadCrm)
        .options(noload(LeadCrm.match))
        .where(LeadCrm.match_id == lead_id)
        .with_for_update()
    )


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


# --------------------------------------------------------------------- выборки


def _list_query(conditions: list[ColumnElement[bool]]) -> Select:
    return (
        select(
            Match.id,
            Match.headline,
            Match.score,
            Match.intent_tag,
            Match.circuit,
            RawItem.source,
            Match.created_at,
            _STATE.label("contact_state"),
            LeadCrm.taken_in_work,
            LeadCrm.earnings,
            LeadCrm.currency,
        )
        .join(RawItem, RawItem.id == Match.raw_item_id)
        .outerjoin(LeadCrm, LeadCrm.match_id == Match.id)
        .where(*conditions)
        # id вторым ключом: created_at выставляется server_default'ом и у пачки
        # записей из одного прохода коллектора совпадает до микросекунд.
        # Без него страницы 1 и 2 могут показать одну запись дважды.
        .order_by(Match.created_at.desc(), Match.id.desc())
    )


def _conditions(lead_filter: str) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [_VISIBLE]
    extra = _FILTERS[lead_filter]
    if extra is not None:
        conditions.append(extra)
    return conditions


async def _fetch_leads(
    session: AsyncSession, lead_filter: str, limit: int, offset: int
) -> tuple[list[LeadListItem], int]:
    conditions = _conditions(lead_filter)
    rows = (await session.execute(_list_query(conditions).limit(limit).offset(offset))).all()
    total = await session.scalar(
        select(func.count())
        .select_from(Match)
        .join(RawItem, RawItem.id == Match.raw_item_id)
        .outerjoin(LeadCrm, LeadCrm.match_id == Match.id)
        .where(*conditions)
    )
    return [LeadListItem(**row._mapping) for row in rows], (total or 0)


async def _fetch_detail(session: AsyncSession, lead_id: int) -> LeadDetail:
    row = (
        await session.execute(
            select(Match, RawItem, LeadCrm)
            .join(RawItem, RawItem.id == Match.raw_item_id)
            .outerjoin(LeadCrm, LeadCrm.match_id == Match.id)
            .where(Match.id == lead_id, _VISIBLE)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Лид не найден")

    match, raw, crm = row
    return LeadDetail(
        id=match.id,
        circuit=match.circuit,
        score=match.score,
        intent_tag=match.intent_tag,
        headline=match.headline,
        signals=match.signals,
        stack_matched=match.stack_matched,
        compensation=match.compensation,
        contact=match.contact,
        decided_by=match.decided_by,
        # llm_summary_ru лежит на matches, а не на raw_posts: это результат
        # разбора записи, а не её часть.
        llm_summary_ru=match.llm_summary_ru,
        llm_prob=match.llm_prob,
        llm_status=match.llm_status,
        duplicate_count=match.duplicate_count,
        is_favorite=match.is_favorite,
        created_at=match.created_at,
        raw_item=RawItemOut(
            id=raw.id,
            source=raw.source,
            author_handle=raw.author_handle,
            url=raw.permalink,
            title=raw.title,
            text=raw.text,
            posted_at=raw.posted_at,
        ),
        crm=_crm_out(crm),
    )


async def _compute_stats(session: AsyncSession) -> StatsResponse:
    """Воронка одним запросом плюс заработок по валютам.

    Один проход с FILTER вместо шести отдельных COUNT: таблица сканируется
    один раз, и все цифры получаются из одного и того же снимка данных —
    иначе на живой базе суммы по срезам могут не сойтись с итогом.
    """
    row = (
        await session.execute(
            select(
                func.count().filter(_VISIBLE).label("total_leads"),
                func.count().filter(_VISIBLE, _STATE == "not_contacted").label("not_contacted"),
                func.count().filter(_VISIBLE, _STATE == "contacted").label("contacted"),
                func.count().filter(_VISIBLE, _STATE == "postponed").label("postponed"),
                func.count().filter(_VISIBLE, LeadCrm.taken_in_work.is_(True)).label("in_work"),
                func.count().filter(_VISIBLE, LeadCrm.taken_in_work.is_(False)).label("no_deal"),
                func.count().filter(LeadCrm.deleted_at.isnot(None)).label("removed"),
            )
            .select_from(Match)
            .outerjoin(LeadCrm, LeadCrm.match_id == Match.id)
        )
    ).one()

    # Заработок считается ПО ВСЕМ строкам, включая мягко удалённые: убрать лид
    # из рабочего списка — не то же самое, что отменить полученные по нему деньги
    # (см. комментарий к deleted_at в миграции 0004).
    earnings = (
        await session.execute(
            select(LeadCrm.currency, func.sum(LeadCrm.earnings))
            .where(LeadCrm.earnings.isnot(None))
            .group_by(LeadCrm.currency)
            .order_by(LeadCrm.currency)
        )
    ).all()

    return StatsResponse(
        **row._mapping,
        earnings_total=[
            EarningsByCurrency(currency=currency, amount=total) for currency, total in earnings
        ],
    )


# ------------------------------------------------------------- HTML-фрагменты


def _detail_context(detail: LeadDetail) -> dict:
    """Плоский `lead` для partials/lead_detail.html.

    Шаблон ждёт одну плоскую запись, а API отдаёт вложенную (raw_item / crm) —
    вложенность в JSON полезна, в шаблоне она превратилась бы в lead.crm.earnings
    с проверкой на None на каждом обращении. Сплющивание живёт здесь, в одном месте.
    """
    crm = detail.crm
    return {
        "id": detail.id,
        "headline": detail.headline,
        "score": detail.score,
        "intent_tag": detail.intent_tag,
        "source": detail.raw_item.source,
        "created_at": detail.created_at,
        "text": detail.raw_item.text,
        "url": detail.raw_item.url,
        "llm_summary_ru": detail.llm_summary_ru,
        "contact_state": crm.contact_state if crm else "not_contacted",
        "response_text": crm.response_text if crm else None,
        "taken_in_work": crm.taken_in_work if crm else None,
        "earnings": crm.earnings if crm else None,
        "currency": crm.currency if crm else None,
    }


async def _fetch_row_by_id(session: AsyncSession, lead_id: int) -> LeadListItem:
    row = (await session.execute(_list_query([_VISIBLE, Match.id == lead_id]))).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Лид не найден")
    return LeadListItem(**row._mapping)


async def _row_main_fragment(session: AsyncSession, lead_id: int) -> str:
    """Шапка строки — то, что PATCH /contact кладёт на место #lead-row-main-{id}."""
    item = await _fetch_row_by_id(session, lead_id)
    return render("dashboard/partials/lead_row_main.html", lead=item)


async def _stats_oob(session: AsyncSession) -> str:
    """OOB-блок статистики. id совпадает с обёрткой #stats-bar в index.html."""
    stats = await _compute_stats(session)
    return _oob("stats-bar", render("dashboard/partials/stats.html", stats=stats))


async def _detail_fragment(session: AsyncSession, lead_id: int) -> str:
    detail = await _fetch_detail(session, lead_id)
    return render("dashboard/partials/lead_detail.html", lead=_detail_context(detail))


# ------------------------------------------------------------------------------ роуты

# Зависимость навешена на роутер целиком, а не на каждый роут. Так новый
# эндпоинт закрыт по умолчанию: забыть добавить Depends нельзя, можно только
# явно вынести роут за пределы этого роутера.
api = APIRouter(prefix="/api", tags=["crm"], dependencies=[Depends(require_session)])


@api.get("/leads", response_model=LeadListResponse)
async def list_leads(
    request: Request,
    session: Db,
    filter: LeadFilter = Query(default="all", description="Срез воронки"),
    limit: int = Query(default=PAGE_SIZE, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Any:
    """Лента лидов, новые сверху. Мягко удалённые не показываются ни при каком фильтре."""
    items, total = await _fetch_leads(session, filter, limit, offset)

    if wants_html(request):
        # Тот же партиал, что и при первой отдаче страницы: и смена таба,
        # и «Показать ещё» бьют сюда же. limit/offset отдаются шаблону, потому
        # что кнопка «Показать ещё» считает от них следующий offset.
        return html(
            render(
                "dashboard/partials/lead_list.html",
                leads=items,
                active_filter=filter,
                limit=limit,
                offset=offset,
            )
        )

    response = LeadListResponse(items=items, total=total, limit=limit, offset=offset)
    return JSONResponse(response.model_dump(mode="json"), headers={"Vary": "HX-Request"})


@api.get("/leads/{lead_id}", response_model=LeadDetail)
async def get_lead(request: Request, session: Db, lead_id: int = PathParam(ge=1)) -> Any:
    """Карточка лида: разбор из matches, исходная запись и CRM-состояние."""
    detail = await _fetch_detail(session, lead_id)

    if wants_html(request):
        return html(
            render("dashboard/partials/lead_detail.html", lead=_detail_context(detail))
        )

    return JSONResponse(detail.model_dump(mode="json"), headers={"Vary": "HX-Request"})


@api.patch("/leads/{lead_id}/contact", response_model=LeadCrmOut, openapi_extra=_body_doc(ContactPatch))
async def set_contact_state(request: Request, session: Db, lead_id: int = PathParam(ge=1)) -> Any:
    """Отметить «написал» или «отложил». Первое CRM-действие заводит строку."""
    payload = await parse_body(request, ContactPatch)
    await _require_match(session, lead_id)

    contacted_at = func.now() if payload.contact_state == "contacted" else None
    stmt = pg_insert(LeadCrm).values(
        match_id=lead_id,
        contact_state=payload.contact_state,
        contacted_at=contacted_at,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_lead_crm_match",
        set_={
            "contact_state": stmt.excluded.contact_state,
            # coalesce, а не присваивание: contacted_at — это «когда написал
            # впервые». Переключение contacted -> postponed -> contacted не
            # должно двигать дату первого касания.
            "contacted_at": func.coalesce(LeadCrm.contacted_at, stmt.excluded.contacted_at),
            "updated_at": func.now(),
        },
    )

    await session.execute(stmt)
    await session.commit()
    log.info("dashboard.crm.contact", lead_id=lead_id, state=payload.contact_state)

    if wants_html(request):
        # Кнопки «Написал»/«Отложить» таргетят шапку строки (#lead-row-main-{id},
        # hx-swap="outerHTML"), поэтому и возвращается шапка, а не вся строка:
        # раскрытая панель деталей при этом остаётся открытой. Плюс OOB-обновление
        # счётчиков тем же ответом — без второго запроса и без рассинхрона.
        return html(await _row_main_fragment(session, lead_id), await _stats_oob(session))

    # noload — чтобы lazy="joined" на LeadCrm.match не тащил сюда весь Match
    # с его JSONB и ARRAY ради четырёх полей CRM-состояния.
    row = await session.scalar(
        select(LeadCrm).options(noload(LeadCrm.match)).where(LeadCrm.match_id == lead_id)
    )
    return _crm_out(row)


@api.patch("/leads/{lead_id}/response", response_model=LeadCrmOut, openapi_extra=_body_doc(ResponsePatch))
async def set_response(request: Request, session: Db, lead_id: int = PathParam(ge=1)) -> Any:
    """Записать ответ клиента и решение «взят в работу».

    Требует, чтобы лид уже был помечен как contacted: ответ на неотправленное
    сообщение — не состояние воронки, а опечатка в клиенте.
    """
    payload = await parse_body(request, ResponsePatch)
    await _require_match(session, lead_id)
    crm = await _lock_crm(session, lead_id)

    if crm is None or crm.contact_state != "contacted":
        raise _bad_request("Сначала отметь лид как contacted — ответа на ненаписанное не бывает")

    if payload.taken_in_work is not True and crm.earnings is not None:
        # CHECK ck_lead_crm_earnings_requires_work этого всё равно не пропустит,
        # но 400 с объяснением лучше, чем 500 от нарушения ограничения. Молча
        # обнулять заработок здесь нельзя: это единственное место, где он хранится.
        raise _bad_request(
            "По лиду проставлен заработок — снять «взят в работу» можно только после его сброса"
        )

    crm.reply_text = payload.response_text
    crm.taken_in_work = payload.taken_in_work
    if payload.taken_in_work is True and crm.in_work_at is None:
        # func.now(), а не datetime.now() в питоне: время берётся из той же
        # транзакции Postgres, что и все остальные отметки времени в таблице,
        # и не зависит от часов контейнера с дашбордом.
        crm.in_work_at = func.now()
    await session.commit()
    await session.refresh(crm)
    log.info("dashboard.crm.response", lead_id=lead_id, taken_in_work=payload.taken_in_work)

    if wants_html(request):
        # Форма живёт внутри раскрытого блока деталей и туда же свапается,
        # а строка списка и счётчики обновляются out-of-band: без этого бейджи
        # в строке и цифры в шапке разъехались бы с реальным состоянием.
        return html(
            await _detail_fragment(session, lead_id),
            await _row_main_oob(session, lead_id),
            await _stats_oob(session),
        )
    return _crm_out(crm)


@api.patch("/leads/{lead_id}/earnings", response_model=LeadCrmOut, openapi_extra=_body_doc(EarningsPatch))
async def set_earnings(request: Request, session: Db, lead_id: int = PathParam(ge=1)) -> Any:
    """Проставить сумму по сделке. Только для лида, взятого в работу."""
    payload = await parse_body(request, EarningsPatch)
    await _require_match(session, lead_id)
    crm = await _lock_crm(session, lead_id)

    if crm is None or crm.taken_in_work is not True:
        raise _bad_request("Деньги проставляются только по лиду, взятому в работу")

    crm.earnings = payload.earnings
    crm.currency = payload.currency
    await session.commit()
    await session.refresh(crm)
    log.info("dashboard.crm.earnings", lead_id=lead_id, currency=payload.currency)

    if wants_html(request):
        return html(
            await _detail_fragment(session, lead_id),
            await _row_main_oob(session, lead_id),
            await _stats_oob(session),
        )
    return _crm_out(crm)


@api.delete("/leads/{lead_id}")
async def delete_lead(request: Request, session: Db, lead_id: int = PathParam(ge=1)) -> Any:
    """Убрать лид из CRM-вида. Мягко: deleted_at, строка matches не трогается.

    Идемпотентно — повторное удаление возвращает тот же успех. Жёсткое удаление
    не подходит: лид остался бы в matches и вернулся бы в список как «не
    написал», а история и заработок по нему исчезли бы из статистики.
    """
    match_exists = await session.scalar(select(Match.id).where(Match.id == lead_id))
    if match_exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Лид не найден")

    stmt = pg_insert(LeadCrm).values(
        match_id=lead_id,
        contact_state="not_contacted",
        deleted_at=func.now(),
        updated_at=func.now(),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_lead_crm_match",
            set_={
                # coalesce: повторное удаление не переписывает дату первого.
                "deleted_at": func.coalesce(LeadCrm.deleted_at, stmt.excluded.deleted_at),
                "updated_at": func.now(),
            },
        )
    )
    await session.commit()
    log.info("dashboard.crm.deleted", lead_id=lead_id)

    if wants_html(request):
        # 200 с ПУСТЫМ телом, а не 204. Для htmx 204 означает «ничего не менять»,
        # и удалённая строка осталась бы висеть на экране до перезагрузки.
        # Пустое тело в hx-swap="outerHTML" схлопывает строку, OOB обновляет шапку.
        return html("", await _stats_oob(session))

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.get("/stats", response_model=StatsResponse)
async def get_stats(request: Request, session: Db) -> Any:
    """Воронка и заработок по валютам."""
    stats = await _compute_stats(session)
    if wants_html(request):
        return html(render("dashboard/partials/stats.html", stats=stats))
    return JSONResponse(stats.model_dump(mode="json"), headers={"Vary": "HX-Request"})


async def _row_main_oob(session: AsyncSession, lead_id: int) -> str:
    """Шапка строки лида (бейджи и кнопки) как out-of-band блок.

    Именно шапка, а не строка целиком. Строка содержит слот #detail-{id}
    с раскрытой панелью и Alpine-состояние open на обёртке; заменив её целиком,
    ответ на «Сохранить ответ» схлопнул бы панель прямо под руками у того,
    кто эту кнопку нажал, и увёл бы у htmx цель основного свапа.
    """
    item = await _fetch_row_by_id(session, lead_id)
    return render("dashboard/partials/lead_row_main.html", lead=item, oob=True)


# ----------------------------------------------------------------- страницы


pages = APIRouter(include_in_schema=False)


async def _has_session(request: Request) -> bool:
    return await touch_session(request.cookies.get(COOKIE_NAME, ""))


@pages.get("/")
async def index_page(
    request: Request,
    session: Db,
    lead: int | None = Query(default=None, ge=1, description="Раскрыть этот лид сразу"),
) -> Any:
    """Страница дашборда. Без сессии — на форму входа.

    require_session здесь не годится: он отвечает 401, а человеку в браузере
    нужен переход на /login, а не JSON с ошибкой. Проверка та же самая,
    отличается только реакция на её провал.

    ?lead={id} — переход из карточки в Telegram. Карточка этого лида приезжает
    уже раскрытой и уже наполненной, прямо в первом ответе: подгружать её после
    загрузки через htmx означало бы лишний round-trip и моргание пустой панелью
    ровно в тот момент, когда человек и пришёл смотреть именно этот лид.

    Несуществующий или удалённый id молча игнорируется. Ссылка приходит из
    Telegram и запросто может пережить удаление лида; показать полный список
    здесь полезнее, чем 404 на весь дашборд из-за одного параметра.
    """
    if not await _has_session(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    leads, _ = await _fetch_leads(session, "all", PAGE_SIZE, 0)
    stats = await _compute_stats(session)

    expand_lead_id: int | None = None
    expand_detail: dict | None = None
    if lead is not None:
        try:
            expand_detail = _detail_context(await _fetch_detail(session, lead))
            expand_lead_id = lead
        except HTTPException:
            # Лида нет или он мягко удалён — параметр просто игнорируется.
            log.info("dashboard.deeplink.miss", lead_id=lead)

    if expand_lead_id is not None and all(item.id != expand_lead_id for item in leads):
        # Лид существует, но в первую страницу списка не попал (он старше
        # PAGE_SIZE последних). Без этого ссылка из Telegram открывала бы
        # обычный список без всякого намёка на то, ради чего по ней пришли.
        leads = [await _fetch_row_by_id(session, expand_lead_id), *leads]

    return HTMLResponse(
        render(
            "dashboard/index.html",
            stats=stats,
            leads=leads,
            active_filter="all",
            page_size=PAGE_SIZE,
            expand_lead_id=expand_lead_id,
            expand_detail=expand_detail,
        )
    )


@pages.get("/login")
async def login_page(request: Request) -> Any:
    """Форма входа. С живой сессией — сразу на дашборд, чтобы не показывать
    форму тому, кто уже вошёл."""
    if await _has_session(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(render("dashboard/login.html"))


# -------------------------------------------------------------------------- приложение


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    # Проверка здесь, а не только в main(): под `uvicorn app.dashboard:app`
    # (а именно так его и запустит compose) main() не вызывается вовсе,
    # и сервис поднялся бы без пароля — то есть открытым.
    require(
        "database_url",
        "dashboard_password_hash",
        "dashboard_password_pepper",
        "dashboard_origin",
    )
    log.info("dashboard.start", origin=settings.dashboard_origin)
    yield
    log.info("dashboard.stop")


def create_app() -> FastAPI:
    app = FastAPI(
        title="feedsift dashboard",
        description="CRM-слой поверх лидов: воронка, ответы, заработок.",
        version="1.0.0",
        lifespan=lifespan,
        # Штатные /docs и /redoc выключены: их HTML тянет скрипты со стороннего
        # CDN и не живёт под CSP из app/auth/security_headers.py. Схема отдаётся
        # ниже отдельным роутом и закрыта сессией.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

    # Порядок важен: add_middleware кладёт обработчик НАРУЖУ предыдущих, поэтому
    # заголовки безопасности добавляются последними и попадают в том числе
    # на 403 от origin guard.
    app.add_middleware(OriginGuardMiddleware, allowed_origin=settings.dashboard_origin)
    app.add_middleware(SecurityHeadersMiddleware)
    # CORS-мидлвари здесь нет намеренно. Фронтенд приезжает с того же origin,
    # а любой разрешённый чужой origin в связке с куками означал бы, что
    # SameSite=Strict и origin guard обходятся штатными средствами.

    # Узко: наружу отдаётся только статика дашборда, а не весь app/static.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(auth_router)
    app.include_router(api)
    app.include_router(pages)

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        """Для healthcheck в compose. Открыт и не ходит ни в БД, ни в Redis:
        healthcheck обязан отвечать быстро и не превращаться в бесплатный
        способ узнать снаружи, жива ли база."""
        return JSONResponse({"status": "ok"})

    @app.get(
        "/api/openapi.json",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def openapi_schema(request: Request) -> JSONResponse:
        return JSONResponse(request.app.openapi())

    return app


app = create_app()


def main() -> None:
    configure_logging()
    require(
        "database_url",
        "dashboard_password_hash",
        "dashboard_password_pepper",
        "dashboard_origin",
    )
    uvicorn.run(
        "app.dashboard:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        # Логи пишет structlog (app/logging_setup.py), собственный конфиг uvicorn
        # дублировал бы их вторым форматом.
        log_config=None,
        # За реверс-прокси X-Forwarded-* разбирает наш собственный client_ip
        # (app/auth/rate_limit.py). Встроенный proxy-headers uvicorn'а доверяет
        # первому элементу X-Forwarded-For, то есть тому, что прислал клиент.
        proxy_headers=False,
        server_header=False,
        date_header=True,
    )


if __name__ == "__main__":
    main()
