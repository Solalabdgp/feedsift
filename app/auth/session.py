"""Сессия дашборда: непрозрачный токен в куке, состояние — в Redis.

Почему не JWT. Единственный плюс JWT — не ходить за состоянием; здесь и так
один Redis на расстоянии одного GET. Зато отозвать выданный JWT нельзя без того
же самого списка в Redis, то есть вся экономия обнуляется, а взамен получаем
подпись, алгоритм и срок жизни, которые надо не перепутать. Случайный токен
плюс ключ в Redis: выход = DEL.

В Redis кладётся НЕ сам токен, а его sha256. Дамп Redis (RDB на диске хоста,
`KEYS *` от соседнего процесса) тогда не даёт готовых к предъявлению кук —
из ключа токен не восстанавливается. Тот же приём, что с паролями в БД, только
токен случайный, поэтому хватает голого sha256 без KDF.

Два срока жизни:
  * скользящий  (dashboard_session_ttl_sec) — обновляется на каждом запросе,
    чтобы работающая вкладка не разлогинивалась под руками;
  * абсолютный  (dashboard_session_absolute_ttl_sec) — от момента входа
    и не продлевается ничем. Без него украденная кука живёт вечно, пока её
    касаются: скользящее окно само себя и продлевает.
"""
import hashlib
import json
import secrets
from datetime import datetime, timezone

import structlog

from app.config import settings
from app.redis_client import get_redis

log = structlog.get_logger(__name__)

# Плоский ключ без namespace — как и всё остальное в app/redis_client.py:
# у проекта отдельный инстанс Redis.
KEY_SESSION = "dash:session:{digest}"

_TOKEN_BYTES = 32  # 256 бит энтропии, в base64url это 43 символа


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_session(*, ip: str | None = None, user_agent: str | None = None) -> str:
    """Завести сессию и вернуть токен. Вызывается только после проверки пароля."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    now = _now()
    payload = {
        "created_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
        # Чисто для разбора инцидента: «кто и откуда вошёл». На проверку доступа
        # не влияет — привязка сессии к IP ломает мобильный интернет и VPN,
        # а к User-Agent — обновление браузера.
        "ip": ip,
        "user_agent": (user_agent or "")[:200],
    }
    redis = get_redis()
    await redis.set(
        KEY_SESSION.format(digest=_digest(token)),
        json.dumps(payload),
        ex=settings.dashboard_session_ttl_sec,
    )
    log.info("dashboard.session.created", ip=ip)
    return token


async def touch_session(token: str) -> bool:
    """Проверить сессию и продлить скользящее окно.

    False — токена нет, он протух или упёрся в абсолютный потолок. Вызывающему
    (require_session) в любом из этих случаев нужно одно и то же: 401.
    """
    if not token:
        return False
    key = KEY_SESSION.format(digest=_digest(token))
    redis = get_redis()
    raw = await redis.get(key)
    if raw is None:
        return False

    try:
        payload = json.loads(raw)
        created_at = datetime.fromisoformat(payload["created_at"])
    except (ValueError, KeyError, TypeError):
        # Мусор в значении — сессии нет. Ключ убираем, чтобы он не переживал
        # собственную непригодность до конца TTL.
        await redis.delete(key)
        return False

    # >=, а не >: сессия действительна, пока age СТРОГО меньше потолка. Иначе
    # нулевой потолок («считать все сессии просроченными») не срабатывал бы,
    # пока часы не тикнут — на Windows разрешение таймера ~15 мс, и два вызова
    # подряд дают ровно нулевой возраст.
    age = (_now() - created_at).total_seconds()
    if age >= settings.dashboard_session_absolute_ttl_sec:
        await redis.delete(key)
        log.info("dashboard.session.expired_absolute")
        return False

    payload["last_seen_at"] = _now().isoformat()
    await redis.set(key, json.dumps(payload), ex=settings.dashboard_session_ttl_sec)
    return True


async def destroy_session(token: str) -> None:
    """Идемпотентно. Выход из уже истёкшей сессии — не ошибка."""
    if not token:
        return
    await get_redis().delete(KEY_SESSION.format(digest=_digest(token)))
    log.info("dashboard.session.destroyed")
