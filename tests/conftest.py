"""Общая обвязка тестов дашборда.

Переменные окружения выставляются ДО импорта чего-либо из app: app/config.py
создаёт объект Settings прямо на импорте модуля, и .env из корня репозитория
для тестов не годится — там боевые значения либо пусто.

Redis настоящим не поднимается: fakeredis подменяет клиент, но клиент при этом
остаётся redis-py, то есть проверяется реальная семантика TTL, INCR и EXPIRE,
на которую рассчитывают session.py и rate_limit.py.

Postgres подменить нечем: схема опирается на JSONB, ARRAY и ON CONFLICT, и любая
замена проверяла бы не тот SQL, который поедет в прод. Поэтому тесты, которым
нужна база, запускаются только при заданном TEST_DATABASE_URL и иначе честно
пропускаются:

    TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/feedsift_test pytest
"""
import os

# --- до импорта app ---
TEST_PEPPER = "test-pepper-not-a-secret"
TEST_PASSWORD = "correct horse battery staple"
TEST_ORIGIN = "https://dash.test"

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ["DASHBOARD_PASSWORD_PEPPER"] = TEST_PEPPER
os.environ["DASHBOARD_ORIGIN"] = TEST_ORIGIN
# По http в тестах кука с Secure до клиента не доедет — httpx её просто не сохранит.
os.environ["DASHBOARD_COOKIE_SECURE"] = "false"
os.environ["LOG_LEVEL"] = "CRITICAL"

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.auth import rate_limit, session as session_mod  # noqa: E402
from app.auth.security import hash_password  # noqa: E402
from app.config import settings  # noqa: E402

os.environ["DASHBOARD_PASSWORD_HASH"] = hash_password(TEST_PASSWORD)
settings.dashboard_password_hash = os.environ["DASHBOARD_PASSWORD_HASH"]

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="нужен TEST_DATABASE_URL с пустой тестовой базой Postgres",
)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.aioredis.FakeRedis:
    """Подменяет Redis в обоих модулях, которые в него ходят.

    Патчить app.redis_client.get_redis бесполезно: session.py и rate_limit.py
    импортировали имя к себе на импорте модуля и держат ссылку на исходную
    функцию. Подменять нужно именно их атрибуты.
    """
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(session_mod, "get_redis", lambda: client)
    monkeypatch.setattr(rate_limit, "get_redis", lambda: client)
    return client


@pytest_asyncio.fixture
async def client(fake_redis) -> AsyncClient:
    """HTTP-клиент поверх ASGI-приложения, без сети и без сервера."""
    from app.dashboard import app

    # slowapi держит счётчик в памяти процесса и переживает отдельные тесты —
    # 5/минуту на /auth/login выбило бы соседние кейсы, которые про другое.
    app.state.limiter.reset()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url=TEST_ORIGIN,
        # Origin на каждом запросе: origin_guard отклоняет небезопасные методы
        # без него — ровно то поведение, которое отдельно проверяется в тестах.
        headers={"Origin": TEST_ORIGIN},
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def bare_client(fake_redis) -> AsyncClient:
    """Тот же клиент, но БЕЗ заголовка Origin по умолчанию — для проверок
    самого origin guard: httpx не умеет убирать заголовок клиента на одном
    запросе, поэтому его проще не ставить вовсе."""
    from app.dashboard import app

    app.state.limiter.reset()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=TEST_ORIGIN) as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_client(client: AsyncClient) -> AsyncClient:
    """Клиент с уже установленной кукой сессии."""
    response = await client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 200, response.text
    return client


# ------------------------------------------------------------------ база и данные


@pytest_asyncio.fixture
async def db_engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(db_engine, client):
    """Три лида в базе и приложение, ходящее в эту же базу.

    Зависимость db_session подменяется целиком: своего engine у дашборда нет,
    он берёт сессию из app/db.py, а тот смотрит в боевой DATABASE_URL.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.dashboard import app, db_session
    from app.models import Match, RawItem, Source

    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[db_session] = override

    now = datetime.now(timezone.utc)
    async with sessionmaker() as session:
        session.add(Source(name="alpha", category="job_board", circuit="main"))
        await session.flush()
        for i in range(5):
            raw = RawItem(
                source="alpha",
                circuit="main",
                item_id=f"item-{i}",
                author_handle=f"user{i}",
                permalink=f"https://example.test/{i}",
                title=f"Заголовок {i}",
                posted_at=now,
                text=f"Текст записи {i}",
                text_norm=f"текст записи {i}",
                text_hash=f"{i:064d}",
                simhash=i,
            )
            session.add(raw)
            await session.flush()
            session.add(
                Match(
                    raw_item_id=raw.id,
                    circuit="main",
                    score=10 + i,
                    intent_tag="hiring_intent",
                    headline=f"Лид {i}",
                    rules_version="v1",
                    llm_summary_ru=f"Пересказ {i}",
                    # Дашборд показывает ТОЛЬКО долетевшее до Telegram. Первые
                    # три — долетели, последние два нет: на них проверяется,
                    # что отсеянное не течёт ни в список, ни в статистику,
                    # ни в карточку по прямой ссылке.
                    status="notified" if i < 3 else ("suppressed" if i == 3 else "new"),
                )
            )
        await session.commit()

        ids = list(
            (
                await session.execute(
                    select(Match.id).where(Match.status == "notified").order_by(Match.id)
                )
            )
            .scalars()
            .all()
        )

    login = await client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert login.status_code == 200, login.text
    yield client, ids
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def hidden_ids(seeded, db_engine) -> list[int]:
    """id матчей, которые до Telegram не дошли (suppressed / new).

    Зависит от seeded явно, а не только от db_engine: иначе порядок создания
    фикстур определялся бы порядком аргументов в сигнатуре теста, и запрос
    мог бы уйти в ещё пустую таблицу.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import Match

    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        return list(
            (
                await session.execute(
                    select(Match.id).where(Match.status != "notified").order_by(Match.id)
                )
            )
            .scalars()
            .all()
        )
