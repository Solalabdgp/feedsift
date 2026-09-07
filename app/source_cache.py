import time
from dataclasses import dataclass

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Source

REFRESH_INTERVAL_SEC = 60


@dataclass
class SourceInfo:
    name: str
    category: str
    enabled: bool
    muted_until: object
    priority: int
    # main|browse — источник истины Source.circuit (app/models.py). worker.py читает
    # это поле, чтобы решить, какой LLM-промпт/очередь/маршрут уведомления
    # применить к записи.
    circuit: str = "main"


class SourceCache:
    def __init__(self) -> None:
        self._by_name: dict[str, SourceInfo] = {}
        self._loaded_at: float = 0.0

    async def _refresh(self) -> None:
        async with SessionLocal() as session:
            rows = (await session.execute(select(Source))).scalars().all()
        self._by_name = {
            r.name.lower(): SourceInfo(
                r.name, r.category, r.enabled, r.muted_until, r.priority, r.circuit
            )
            for r in rows
        }
        self._loaded_at = time.time()

    async def get(self, name: str) -> SourceInfo | None:
        if time.time() - self._loaded_at > REFRESH_INTERVAL_SEC:
            await self._refresh()
        return self._by_name.get(name.lower())

    async def enabled_names(self) -> list[str]:
        if time.time() - self._loaded_at > REFRESH_INTERVAL_SEC:
            await self._refresh()
        return [i.name for i in self._by_name.values() if i.enabled]
