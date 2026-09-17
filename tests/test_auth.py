"""Аутентификация: пароль, сессия, блокировка перебора, CSRF, заголовки."""
import pytest

from app.auth import rate_limit
from app.auth.dependency import COOKIE_NAME
from app.auth.security import hash_password, verify_owner_password
from app.auth.session import create_session, destroy_session, touch_session
from app.config import settings
from tests.conftest import TEST_ORIGIN, TEST_PASSWORD

# pytest.ini: asyncio_mode = auto — отдельная пометка async-тестам не нужна.


# ------------------------------------------------------------------ хэш пароля


def test_password_roundtrip() -> None:
    assert verify_owner_password(TEST_PASSWORD) is True
    assert verify_owner_password(TEST_PASSWORD + " ") is False
    assert verify_owner_password("") is False


def test_hash_is_salted() -> None:
    """Два хэша одного пароля различаются — соль своя у каждого."""
    assert hash_password(TEST_PASSWORD) != hash_password(TEST_PASSWORD)


def test_pepper_is_part_of_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Хэш без правильного пеппера не проверяется. Ради этого пеппер и нужен:
    утёкший DASHBOARD_PASSWORD_HASH сам по себе не перебирается."""
    monkeypatch.setattr(settings, "dashboard_password_pepper", "другой-пеппер")
    assert verify_owner_password(TEST_PASSWORD) is False


def test_missing_hash_denies_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dashboard_password_hash", "")
    assert verify_owner_password(TEST_PASSWORD) is False
    assert verify_owner_password("") is False


def test_broken_hash_denies_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dashboard_password_hash", "не-PHC-строка")
    assert verify_owner_password(TEST_PASSWORD) is False


# --------------------------------------------------------------------- сессия


async def test_session_lifecycle(fake_redis) -> None:
    token = await create_session(ip="10.0.0.1")
    assert await touch_session(token) is True

    await destroy_session(token)
    assert await touch_session(token) is False


async def test_session_token_is_not_stored_verbatim(fake_redis) -> None:
    """В Redis лежит sha256 токена: дамп базы не даёт готовых кук."""
    token = await create_session()
    keys = await fake_redis.keys("dash:session:*")
    assert len(keys) == 1
    assert token not in keys[0]


async def test_unknown_token_is_rejected(fake_redis) -> None:
    assert await touch_session("выдуманный") is False
    assert await touch_session("") is False


async def test_absolute_ttl_caps_sliding_window(
    fake_redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Скользящее окно не продлевает сессию за абсолютный потолок."""
    token = await create_session()
    monkeypatch.setattr(settings, "dashboard_session_absolute_ttl_sec", 0)
    assert await touch_session(token) is False
    # Просроченная сессия ещё и удаляется, а не остаётся лежать до конца TTL.
    assert await fake_redis.keys("dash:session:*") == []


# ------------------------------------------------------- блокировка перебора


async def test_lockout_after_max_attempts(fake_redis) -> None:
    ip = "203.0.113.7"
    for _ in range(settings.dashboard_login_max_attempts - 1):
        assert await rate_limit.register_failure(ip) == 0
    assert await rate_limit.is_locked(ip) == 0

    delay = await rate_limit.register_failure(ip)
    assert delay == rate_limit.LOCK_LADDER[0]
    assert 0 < await rate_limit.is_locked(ip) <= delay


async def test_lock_escalates(fake_redis) -> None:
    ip = "203.0.113.8"
    delays = []
    for _ in range(3):
        for _ in range(settings.dashboard_login_max_attempts):
            delay = await rate_limit.register_failure(ip)
        delays.append(delay)
    assert delays == list(rate_limit.LOCK_LADDER[:3])


async def test_success_clears_lockout(fake_redis) -> None:
    ip = "203.0.113.9"
    for _ in range(settings.dashboard_login_max_attempts):
        await rate_limit.register_failure(ip)
    assert await rate_limit.is_locked(ip) > 0

    await rate_limit.register_success(ip)
    assert await rate_limit.is_locked(ip) == 0
    # И лестница тоже: следующая серия начнётся с первой ступени.
    assert await rate_limit.register_failure(ip) == 0


def test_forwarded_for_takes_the_last_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Клиентская часть X-Forwarded-For игнорируется — иначе блокировка
    обходилась бы подстановкой чужого адреса в заголовок."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.2.3.4, 198.51.100.5")],
        "client": ("172.17.0.1", 5000),
    }
    monkeypatch.setattr(settings, "dashboard_trust_proxy", True)
    assert rate_limit.client_ip(Request(scope)) == "198.51.100.5"

    monkeypatch.setattr(settings, "dashboard_trust_proxy", False)
    assert rate_limit.client_ip(Request(scope)) == "172.17.0.1"


# ----------------------------------------------------------------- HTTP-слой


async def test_login_sets_httponly_cookie(client) -> None:
    response = await client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 200

    cookie = response.headers["set-cookie"]
    assert COOKIE_NAME in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie.replace("samesite", "SameSite")


async def test_login_with_wrong_password(client) -> None:
    response = await client.post("/auth/login", json={"password": "нет"})
    assert response.status_code == 401
    assert COOKIE_NAME not in client.cookies


async def test_login_locks_after_repeated_failures(client, fake_redis) -> None:
    from app.dashboard import app

    for _ in range(settings.dashboard_login_max_attempts):
        response = await client.post("/auth/login", json={"password": "нет"})
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    # Блокировка лежит в Redis, а не только в памяти процесса: перезапуск
    # дашборда её не снимает.
    locks = await fake_redis.keys("dash:login:lock:*")
    assert len(locks) == 1
    assert await fake_redis.ttl(locks[0]) > 0

    # Правильный пароль во время блокировки тоже не пускает. Счётчик slowapi
    # сбрасывается явно, иначе неясно, какой из двух уровней защиты ответил.
    app.state.limiter.reset()
    response = await client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 429


async def test_protected_route_requires_session(client) -> None:
    assert (await client.get("/api/stats")).status_code == 401
    assert (await client.get("/api/leads")).status_code == 401
    assert (await client.get("/api/leads/1")).status_code == 401
    assert (await client.delete("/api/leads/1")).status_code == 401


async def test_logout_invalidates_session(auth_client) -> None:
    assert (await auth_client.post("/auth/logout")).status_code == 204
    # Кука стёрта и сессия погашена в Redis — повтор уже не проходит.
    assert (await auth_client.get("/api/stats")).status_code == 401


async def test_tampered_cookie_is_rejected(client) -> None:
    response = await client.get("/api/stats", headers={"Cookie": f"{COOKIE_NAME}=tampered-token"})
    assert response.status_code == 401


async def test_health_is_open(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_openapi_requires_session(client) -> None:
    assert (await client.get("/api/openapi.json")).status_code == 401


async def test_openapi_served_to_session(auth_client) -> None:
    response = await auth_client.get("/api/openapi.json")
    assert response.status_code == 200
    assert "/api/leads" in response.json()["paths"]


# -------------------------------------------------------------- CSRF и заголовки


async def test_unsafe_method_without_origin_is_rejected(bare_client) -> None:
    response = await bare_client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 403


async def test_unsafe_method_from_foreign_origin_is_rejected(client) -> None:
    response = await client.post(
        "/auth/login",
        json={"password": TEST_PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


async def test_referer_is_accepted_when_origin_is_absent(bare_client) -> None:
    response = await bare_client.post(
        "/auth/login",
        json={"password": TEST_PASSWORD},
        headers={"Referer": f"{TEST_ORIGIN}/leads"},
    )
    assert response.status_code == 200


async def test_get_is_not_origin_checked(client) -> None:
    """Безопасные методы состояние не меняют, требовать с них Origin незачем."""
    response = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200


async def test_security_headers_on_every_response(client) -> None:
    response = await client.get("/health")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_security_headers_on_rejected_request(bare_client) -> None:
    """403 от origin guard тоже приходит с заголовками: он снаружи."""
    response = await bare_client.post("/auth/login", json={"password": "x"})
    assert response.status_code == 403
    assert response.headers["x-frame-options"] == "DENY"
