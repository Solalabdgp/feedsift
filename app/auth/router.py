"""POST /auth/login, POST /auth/logout.

Два представления, как и у CRM-роутов (см. app/dashboard.py): htmx-форма шлёт
form-urlencoded и ждёт HTML, JSON-клиент шлёт JSON и ждёт JSON. Различаются
не только тела, но и коды ответа на провал, и это не каприз фронтенда:

  * htmx свапает содержимое только на 2xx. На 401 он не заменит #login-error,
    и человек нажмёт «Войти» ещё раз, не увидев ни слова о том, что пароль
    неверный. Поэтому для htmx провал — это 200 с фрагментом текста ошибки.
  * У JSON-клиента такой проблемы нет, и 401 там остаётся 401: подменять код
    ответа в машинном API ради удобства одного конкретного фронтенда нельзя.

На успех для htmx отдаётся 200 с заголовком HX-Redirect. Обычный 303 не
подошёл бы: XHR отрабатывает редирект прозрачно, htmx получил бы в ответ
HTML целой страницы и вставил бы её внутрь #login-error.


Кука `dash_session`: HttpOnly + Secure + SameSite=Strict, Path=/.
  * HttpOnly — JS её не читает, поэтому XSS на странице дашборда не превращается
    в кражу сессии (украсть можно действия, но не пропуск);
  * Secure — по http не уезжает вовсе;
  * SameSite=Strict — не прикладывается к запросам, начатым с чужого сайта;
    первая линия против CSRF, вторая — origin_guard.

Оба роута открыты (без require_session): логин по определению, логаут —
чтобы выход из уже протухшей сессии не отвечал 401 вместо того, чтобы просто
стереть куку.
"""
import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from app.auth.dependency import COOKIE_NAME
from app.auth.rate_limit import client_ip, is_locked, limiter, register_failure, register_success
from app.auth.security import verify_owner_password
from app.auth.session import create_session, destroy_session
from app.config import settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # max_length не про удобство: без него Argon2 звали бы на тело произвольного
    # размера. HMAC-пеппер (app/auth/security.py) уже сводит вход к 64 символам,
    # так что это второй рубеж, отсекающий мусор до разбора.
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    ok: bool
    expires_in: int


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.dashboard_session_ttl_sec,
        httponly=True,
        secure=settings.dashboard_cookie_secure,
        samesite="strict",
        path="/",
    )


def _wants_html(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _error_fragment(text: str) -> str:
    """Текст ошибки для #login-error. escape обязателен: сюда попадают в том
    числе сообщения с подставленным числом секунд, а привычка форматировать
    HTML строками — ровно то, из чего вырастает XSS в соседнем месте."""
    from html import escape

    return escape(text)


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request) -> Response:
    """Проверить пароль и выдать сессию.

    Ответ на неверный пароль и на «пароль не настроен» одинаков по содержанию
    и по времени (см. verify_owner_password): снаружи состояние конфига не видно.
    """
    password = await _read_password(request)
    as_html = _wants_html(request)
    ip = client_ip(request)

    remaining = await is_locked(ip)
    if remaining:
        # 429, а не 403: причина временная и клиенту сообщается, когда пробовать.
        return _deny(as_html, _locked(remaining))

    if not verify_owner_password(password):
        delay = await register_failure(ip)
        return _deny(as_html, _locked(delay) if delay else _unauthorized())

    await register_success(ip)
    token = await create_session(ip=ip, user_agent=request.headers.get("user-agent"))
    log.info("dashboard.login.ok", ip=ip)

    if as_html:
        response: Response = Response(status_code=status.HTTP_200_OK)
        # htmx сам выполнит переход по этому заголовку. Редирект-статусом это
        # сделать нельзя: XHR отработает его прозрачно, до htmx он не дойдёт.
        response.headers["HX-Redirect"] = "/"
    else:
        response = JSONResponse(
            LoginResponse(ok=True, expires_in=settings.dashboard_session_ttl_sec).model_dump()
        )
    _set_session_cookie(response, token)
    return response


async def _read_password(request: Request) -> str:
    """Пароль из JSON или из формы. htmx шлёт форму, JSON-клиент — JSON."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            raw = await request.json()
        except ValueError:
            raw = {}
    else:
        raw = dict(await request.form())

    try:
        return LoginRequest.model_validate(raw).password
    except ValidationError:
        # Пустое/кривое тело — это тот же неуспешный вход, а не отдельный сорт
        # ошибки. Возвращаем пустую строку и идём общим путём: 422 с разбором
        # полей здесь ничего не даёт владельцу и подсказывает перебирающему,
        # что именно сервер сумел разобрать.
        return ""


def _deny(as_html: bool, error: HTTPException) -> Response:
    """Отказ в том виде, который понимает клиент.

    Для htmx это 200 с текстом: на 4xx он не заменит #login-error, и человек
    не увидит причину отказа вовсе. Код ответа здесь — деталь транспорта,
    а не разрешение: сессия не выдана в обоих случаях.
    """
    if as_html:
        return HTMLResponse(
            _error_fragment(str(error.detail)),
            status_code=status.HTTP_200_OK,
            headers=error.headers or {},
        )
    raise error


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    """Погасить сессию в Redis и стереть куку. Идемпотентно."""
    await destroy_session(request.cookies.get(COOKIE_NAME, ""))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    # Параметры удаления обязаны совпасть с параметрами выдачи, иначе браузер
    # сочтёт это другой кукой и оставит исходную на месте.
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.dashboard_cookie_secure,
        samesite="strict",
    )
    return response


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Неверный пароль",
    )


def _locked(seconds: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Слишком много неудачных попыток. Повтори через {seconds} с.",
        headers={"Retry-After": str(seconds)},
    )


def rate_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    """Ответ на срабатывание slowapi. Свой, а не штатный, ради формы тела:
    у остальных ошибок API это {"detail": ...}, и клиенту незачем разбирать
    два разных формата."""
    log.warning("dashboard.login.throttled", ip=client_ip(request))
    return JSONResponse(
        {"detail": "Слишком часто. Повтори через минуту."},
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        headers={"Retry-After": "60"},
    )
