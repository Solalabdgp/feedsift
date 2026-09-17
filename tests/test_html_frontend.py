"""HTML-ветка: страницы, htmx-фрагменты, форма логина, OOB-обновления.

Проверяется ровно то, чем HTML отличается от JSON: выбор представления по
HX-Request, коды ответов, которые понимает htmx (200 вместо 401 на форме входа,
200 вместо 204 на удалении), и наличие OOB-блоков — без них шапка и строка
списка разъезжаются с реальным состоянием до перезагрузки страницы.
"""
import pytest

from app.auth.dependency import COOKIE_NAME
from tests.conftest import TEST_PASSWORD, requires_db

HX = {"HX-Request": "true"}


# ----------------------------------------------------------------- форма входа


async def test_login_accepts_form_and_redirects_via_header(client) -> None:
    """htmx-форма шлёт form-urlencoded и ждёт HX-Redirect, а не 3xx."""
    response = await client.post("/auth/login", data={"password": TEST_PASSWORD}, headers=HX)

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/"
    assert COOKIE_NAME in client.cookies


async def test_login_failure_for_htmx_is_200_with_text(client) -> None:
    """На 4xx htmx не свапнет #login-error, и человек не увидит причину отказа."""
    response = await client.post("/auth/login", data={"password": "нет"}, headers=HX)

    assert response.status_code == 200
    assert "HX-Redirect" not in response.headers
    assert "пароль" in response.text.lower()
    assert COOKIE_NAME not in client.cookies


async def test_login_failure_for_json_client_stays_401(client) -> None:
    """Подменять код ответа в машинном API ради фронтенда нельзя."""
    response = await client.post("/auth/login", json={"password": "нет"})
    assert response.status_code == 401


async def test_login_json_still_returns_json(client) -> None:
    response = await client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "expires_in": 43200}
    assert "HX-Redirect" not in response.headers


async def test_empty_form_is_a_plain_denial(client) -> None:
    """Пустая форма — это неуспешный вход, а не 422 с разбором полей."""
    response = await client.post("/auth/login", data={}, headers=HX)
    assert response.status_code == 200
    assert COOKIE_NAME not in client.cookies


# -------------------------------------------------------------------- страницы


async def test_index_redirects_to_login_without_session(client) -> None:
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_login_page_renders_form(client) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    assert 'hx-post="/auth/login"' in response.text
    assert 'name="password"' in response.text


async def test_login_page_redirects_when_already_signed_in(auth_client) -> None:
    response = await auth_client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_vendored_scripts_not_cdn(client) -> None:
    """CSP разрешает script-src 'self' — CDN-скрипт браузер бы не выполнил."""
    page = (await client.get("/login")).text
    assert "cdn.jsdelivr.net" not in page
    assert "/static/vendor/htmx-2.0.10.min.js" in page
    # Именно CSP-сборка Alpine: обычная требует 'unsafe-eval'.
    assert "/static/vendor/alpine-csp-3.17.3.min.js" in page


async def test_static_files_are_served(client) -> None:
    for path in (
        "/static/dashboard.css",
        "/static/dashboard.js",
        "/static/vendor/htmx-2.0.10.min.js",
        "/static/vendor/alpine-csp-3.17.3.min.js",
        "/static/vendor/alpine-collapse-3.17.3.min.js",
    ):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert len(response.content) > 0, path


# --------------------------------------------------------- фрагменты списка

pytestmark_db = requires_db


@requires_db
async def test_index_renders_leads_and_stats(seeded) -> None:
    client, ids = seeded
    response = await client.get("/")

    assert response.status_code == 200
    assert 'id="stats-bar"' in response.text
    for lead_id in ids:
        assert f'id="lead-row-{lead_id}"' in response.text


@requires_db
async def test_leads_returns_html_for_htmx(seeded) -> None:
    client, ids = seeded
    response = await client.get("/api/leads", headers=HX)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert f'id="lead-row-{ids[0]}"' in response.text
    # Vary обязателен: тот же URL отдаёт два разных представления.
    assert "HX-Request" in response.headers["vary"]


@requires_db
async def test_leads_still_json_without_header(seeded) -> None:
    client, _ = seeded
    response = await client.get("/api/leads")
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["total"] == 3


@requires_db
async def test_load_more_button_carries_next_offset(seeded) -> None:
    client, _ = seeded
    response = await client.get("/api/leads", params={"limit": 2, "offset": 0}, headers=HX)
    assert '"offset": 2' in response.text

    # Последняя страница короче лимита — кнопки «Показать ещё» быть не должно.
    tail = await client.get("/api/leads", params={"limit": 2, "offset": 2}, headers=HX)
    assert "load-more" not in tail.text


@requires_db
async def test_empty_filter_shows_empty_state(seeded) -> None:
    client, _ = seeded
    response = await client.get("/api/leads", params={"filter": "in_work"}, headers=HX)
    assert "empty-state" in response.text


@requires_db
async def test_detail_fragment_has_forms(seeded) -> None:
    client, ids = seeded
    response = await client.get(f"/api/leads/{ids[0]}", headers=HX)

    assert response.status_code == 200
    assert f'hx-patch="/api/leads/{ids[0]}/response"' in response.text
    assert f'hx-patch="/api/leads/{ids[0]}/earnings"' in response.text
    assert "Текст записи" in response.text
    assert "Пересказ" in response.text


@requires_db
async def test_lead_text_is_escaped(seeded, db_engine) -> None:
    """Тексты лидов приходят из чужих постов — в HTML они не доверяются."""
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import RawItem

    client, ids = seeded
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await session.execute(
            update(RawItem).values(text="<script>alert('xss')</script>")
        )
        await session.commit()

    response = await client.get(f"/api/leads/{ids[0]}", headers=HX)
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text


# ------------------------------------------------------- htmx-мутации и OOB


@requires_db
async def test_contact_via_form_returns_row_and_oob_stats(seeded) -> None:
    client, ids = seeded
    # htmx кодирует hx-vals в форму, а не в JSON.
    response = await client.patch(
        f"/api/leads/{ids[0]}/contact", data={"contact_state": "contacted"}, headers=HX
    )

    assert response.status_code == 200
    assert f'id="lead-row-main-{ids[0]}"' in response.text
    assert "написал" in response.text
    # Счётчики в шапке обновляются тем же ответом, без второго запроса.
    assert 'id="stats-bar" hx-swap-oob="true"' in response.text


@requires_db
async def test_response_form_tri_state_null(seeded) -> None:
    """Радио «Пока не знаю» присылает строку "null" — это None, а не False."""
    client, ids = seeded
    lead = ids[0]
    await client.patch(f"/api/leads/{lead}/contact", data={"contact_state": "contacted"}, headers=HX)

    response = await client.patch(
        f"/api/leads/{lead}/response",
        data={"response_text": "ответил, думает", "taken_in_work": "null"},
        headers=HX,
    )
    assert response.status_code == 200

    crm = (await client.get(f"/api/leads/{lead}")).json()["crm"]
    assert crm["taken_in_work"] is None
    assert crm["in_work_at"] is None


@requires_db
async def test_response_form_true_sets_in_work(seeded) -> None:
    client, ids = seeded
    lead = ids[0]
    await client.patch(f"/api/leads/{lead}/contact", data={"contact_state": "contacted"}, headers=HX)

    response = await client.patch(
        f"/api/leads/{lead}/response",
        data={"response_text": "берём", "taken_in_work": "true"},
        headers=HX,
    )
    assert response.status_code == 200
    # Фрагмент деталей + OOB-строка списка + OOB-статистика в одном ответе.
    assert "earnings-block" in response.text
    # Обновляется ШАПКА строки, а не строка целиком: иначе ответ схлопнул бы
    # раскрытую панель, из которой этот самый запрос и был отправлен.
    assert f'id="lead-row-main-{lead}"' in response.text
    assert 'hx-swap-oob="true"' in response.text
    # Строка целиком НЕ заменяется — иначе схлопнулась бы панель, из которой
    # этот самый запрос и был отправлен.
    assert f'id="lead-row-{lead}" class="lead-row"' not in response.text
    assert 'id="stats-bar" hx-swap-oob="true"' in response.text

    crm = (await client.get(f"/api/leads/{lead}")).json()["crm"]
    assert crm["taken_in_work"] is True
    assert crm["in_work_at"] is not None


@requires_db
async def test_earnings_form_updates_stats_oob(seeded) -> None:
    client, ids = seeded
    lead = ids[0]
    await client.patch(f"/api/leads/{lead}/contact", data={"contact_state": "contacted"}, headers=HX)
    await client.patch(
        f"/api/leads/{lead}/response",
        data={"response_text": "берём", "taken_in_work": "true"},
        headers=HX,
    )

    response = await client.patch(
        f"/api/leads/{lead}/earnings", data={"earnings": "450.50", "currency": "EUR"}, headers=HX
    )
    assert response.status_code == 200
    assert "450.50 EUR" in response.text  # hero-плитка заработка в OOB-статистике


@requires_db
async def test_delete_for_htmx_is_200_with_empty_body(seeded) -> None:
    """204 для htmx означает «ничего не менять» — удалённая строка осталась бы."""
    client, ids = seeded
    response = await client.delete(f"/api/leads/{ids[0]}", headers=HX)

    assert response.status_code == 200
    assert 'id="stats-bar" hx-swap-oob="true"' in response.text
    # Основная часть ответа пуста: строка схлопывается на месте.
    assert f'id="lead-row-{ids[0]}"' not in response.text


@requires_db
async def test_delete_for_json_client_stays_204(seeded) -> None:
    client, ids = seeded
    assert (await client.delete(f"/api/leads/{ids[0]}")).status_code == 204


@requires_db
async def test_stats_multi_currency_fragment(seeded) -> None:
    """Шаблон раньше форматировал earnings_total как число и падал на списке."""
    client, ids = seeded
    for lead, amount, currency in ((ids[0], "100", "USD"), (ids[1], "200", "EUR")):
        await client.patch(
            f"/api/leads/{lead}/contact", data={"contact_state": "contacted"}, headers=HX
        )
        await client.patch(
            f"/api/leads/{lead}/response",
            data={"response_text": "да", "taken_in_work": "true"},
            headers=HX,
        )
        await client.patch(
            f"/api/leads/{lead}/earnings",
            data={"earnings": amount, "currency": currency},
            headers=HX,
        )

    fragment = (await client.get("/api/stats", headers=HX)).text
    assert "200.00 EUR" in fragment
    assert "100.00 USD" in fragment


@requires_db
async def test_stats_fragment_with_no_earnings(seeded) -> None:
    client, _ = seeded
    fragment = (await client.get("/api/stats", headers=HX)).text
    assert "0.00 USD" in fragment


@requires_db
async def test_htmx_mutations_still_origin_checked(seeded, bare_client) -> None:
    """HX-Request не отменяет проверку Origin — заголовок ставит кто угодно."""
    client, ids = seeded
    bare_client.cookies.update(client.cookies)
    response = await bare_client.patch(
        f"/api/leads/{ids[0]}/contact", data={"contact_state": "contacted"}, headers=HX
    )
    assert response.status_code == 403


@requires_db
async def test_htmx_still_requires_session(seeded, client) -> None:
    await client.post("/auth/logout")
    response = await client.get("/api/leads", headers=HX)
    assert response.status_code == 401


# ------------------------------------------------- deep-link из Telegram


@requires_db
async def test_deeplink_expands_lead_server_side(seeded) -> None:
    """Карточка приезжает раскрытой и уже с деталями — без второго запроса."""
    client, ids = seeded
    lead = ids[0]
    response = await client.get("/", params={"lead": lead})

    assert response.status_code == 200
    body = response.text
    # Панель открыта сразу...
    assert f'id="lead-row-{lead}" class="lead-row" x-data="{{ open: true }}"' in body
    # ...и содержимое деталей уже внутри страницы, а не подгружается потом.
    assert f'hx-patch="/api/leads/{lead}/response"' in body
    assert "Текст записи" in body


@requires_db
async def test_deeplink_card_does_not_refetch_on_first_click(seeded) -> None:
    """У раскрытой карточки hx-get снят: детали уже в DOM."""
    client, ids = seeded
    body = (await client.get("/", params={"lead": ids[0]})).text

    assert f'hx-get="/api/leads/{ids[0]}"' not in body
    # У остальных карточек ленивая загрузка на месте.
    assert f'hx-get="/api/leads/{ids[1]}"' in body


@requires_db
async def test_other_cards_stay_collapsed(seeded) -> None:
    client, ids = seeded
    body = (await client.get("/", params={"lead": ids[0]})).text
    assert f'id="lead-row-{ids[1]}" class="lead-row" x-data="{{ open: false }}"' in body


@requires_db
async def test_deeplink_to_unknown_lead_is_ignored(seeded) -> None:
    """Ссылка из Telegram переживает удаление лида — это не повод на 404."""
    client, ids = seeded
    response = await client.get("/", params={"lead": 999999})

    assert response.status_code == 200
    assert 'x-data="{ open: true }"' not in response.text
    for lead_id in ids:
        assert f'id="lead-row-{lead_id}"' in response.text


@requires_db
async def test_deeplink_to_deleted_lead_is_ignored(seeded) -> None:
    client, ids = seeded
    await client.delete(f"/api/leads/{ids[0]}")

    response = await client.get("/", params={"lead": ids[0]})
    assert response.status_code == 200
    assert 'x-data="{ open: true }"' not in response.text
    assert f'id="lead-row-{ids[0]}"' not in response.text


@requires_db
async def test_deeplink_bad_value_rejected(seeded) -> None:
    client, _ = seeded
    assert (await client.get("/", params={"lead": "abc"})).status_code == 422
    assert (await client.get("/", params={"lead": 0})).status_code == 422


@requires_db
async def test_suppressed_lead_not_in_html_list(seeded, hidden_ids) -> None:
    client, _ = seeded
    body = (await client.get("/api/leads", params={"limit": 200}, headers=HX)).text
    for hidden in hidden_ids:
        assert f'id="lead-row-{hidden}"' not in body


@requires_db
async def test_deeplink_to_suppressed_lead_is_ignored(seeded, hidden_ids) -> None:
    """Ссылка вида /?lead={id} не должна быть обходом фильтра."""
    client, _ = seeded
    response = await client.get("/", params={"lead": hidden_ids[0]})

    assert response.status_code == 200
    assert 'x-data="{ open: true }"' not in response.text
    assert f'id="lead-row-{hidden_ids[0]}"' not in response.text
