"""Подключение к Postgres.

Тонкость, которая стоила бага на первом же прогоне стека: Celery-задачи вызывают
asyncio.run() и каждый раз создают НОВЫЙ event loop. Модульный async-engine с пулом
переживает вызов, но его соединения остаются привязаны к прошлому циклу, и второй
вызов падает с "got Future attached to a different loop".

Решение: engine привязан к текущему циклу и пересоздаётся, когда цикл сменился.
NullPool — чтобы от брошенного engine не оставалось живых соединений (dispose()
из другого цикла корректно не сделать). Объёмы здесь сотни записей в сутки,
цена лишнего коннекта на сессию несущественна.
"""
import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

_state: dict = {"loop": None, "engine": None, "sessionmaker": None}


def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    if not settings.database_url:
        # Страховка на случай, если сервис поднялся мимо require() из main()
        raise RuntimeError("DATABASE_URL не задан — подключение к Postgres невозможно.")
    loop = asyncio.get_running_loop()
    if _state["loop"] is not loop:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        _state["engine"] = engine
        _state["sessionmaker"] = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        _state["loop"] = loop
    return _state["sessionmaker"]


def SessionLocal() -> AsyncSession:  # noqa: N802 — фабрика сессий, не класс
    return _sessionmaker()()
