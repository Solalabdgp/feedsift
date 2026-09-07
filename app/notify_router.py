"""Единая точка решения "слать / отложить в дайджест / подавить".
Используется и worker'ом (решение по правилам), и llm-worker'ом (решение после LLM),
чтобы логика quiet hours не разъезжалась между двумя процессами.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import update

from app.config import settings
from app.models import Match
from app.redis_client import DIGEST_QUEUE, NOTIFY_QUEUE

QUIET_SCORE_OVERRIDE = 15  # очень сильный лид будит и ночью


def in_quiet_hours(now_utc: datetime) -> bool:
    local = now_utc.astimezone(ZoneInfo(settings.tz))
    start, end = settings.quiet_start, settings.quiet_end
    t = local.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # диапазон через полночь


async def route(session, redis, match_id: int, score: int, will_notify: bool, circuit: str = "main") -> str:
    """Ставит статус матча и кладёт его в нужную очередь. Возвращает итоговый статус.

    circuit принимается для совместимости сигнатуры с вызывающим кодом (worker.py,
    celery_app.py), но НЕ влияет на маршрутизацию — browse-контур уведомляется
    ТОЧНО ТАК ЖЕ, как main: тот же telegram_owner_id, та же NOTIFY_QUEUE/DIGEST_QUEUE,
    тот же _format_card, тот же статус-флоу "notified"/"suppressed"/"digest_pending".
    Разница между контурами — только на входе (свои учётные данные фида, свой ключ
    LLM, более мягкий критерий отбора, см. app/worker.py и app/llm.py), в уведомлении
    её нет вообще.
    """
    if not will_notify:
        await session.execute(update(Match).where(Match.id == match_id).values(status="suppressed"))
        return "suppressed"

    quiet = in_quiet_hours(datetime.now(timezone.utc)) and score < QUIET_SCORE_OVERRIDE
    status = "digest_pending" if quiet else "new"
    await session.execute(update(Match).where(Match.id == match_id).values(status=status))
    await redis.rpush(DIGEST_QUEUE if quiet else NOTIFY_QUEUE, match_id)
    return status
