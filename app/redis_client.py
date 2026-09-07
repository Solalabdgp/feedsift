"""Ключи Redis. Плоские, без namespace-префикса.

Это безопасно ТОЛЬКО потому, что у проекта отдельный Redis-контейнер. Не подключать
сервис к общему Redis: имена ключей не уникальны в пределах инстанса и параллельные
пайплайны затрут друг друга.
"""
import asyncio

import redis.asyncio as aioredis
import redis as sync_redis

from app.config import settings

STREAM_RAW = "queue:raw"
STREAM_GROUP = "workers"

NOTIFY_QUEUE = "queue:notify"  # list of match_id: worker/llm-worker -> bot
DIGEST_QUEUE = "queue:digest"  # list of match_id, накоплено в quiet hours
DUP_NOTIFY_QUEUE = "queue:dup_notify"
# browse-контур: уведомления идут ТУДА ЖЕ, что и main — та же NOTIFY_QUEUE/DIGEST_QUEUE
# выше, отдельной очереди на push нет. browse обрабатывается точно так же, как main;
# разница только в том, из каких учётных данных фида и какого LLM-ключа идёт приём
# (см. app/notify_router.py).

KEY_DEDUP_EXACT = "dedup:exact:{hash}"
KEY_SIMHASH_BAND = "dedup:simhash:band:{i}:{v}"
KEY_SEEN_ITEM = "seen:item:{item_id}"  # коллектор: не публиковать одну запись дважды

# heartbeat:collector:{circuit} — шаблон, а не плоский ключ. Два коллектора
# (main/browse) — два независимых процесса, каждый со своим циклом опроса; плоский
# ключ означал бы, что они перезатирают heartbeat друг друга и heartbeat_check
# (app/celery_app.py) не заметит падение одного из них, пока жив другой.
# .format(circuit=settings.circuit) на каждый вызов.
KEY_HEARTBEAT_COLLECTOR = "heartbeat:collector:{circuit}"
KEY_RELOAD_SIGNAL = "signal:reload_rules"
KEY_PAUSED = "settings:paused"

# Квота LLM: минутный и дневной счётчики.
# Имена привязаны к провайдеру И к контуру: у browse-контура свой ключ, и его расход
# не должен списываться со счётчика основного пайплайна — иначе трафик browse мог бы
# преждевременно fail-closed'ить реальные лиды контура main даже без общего API-ключа.
KEY_RATE_LLM_MIN = "rate:groq:{circuit}:min"
KEY_RATE_LLM_DAY = "rate:groq:{circuit}:day"

# Пул запасных ключей (см. app/llm.py:call_llm_with_pool). Пул есть только у main
# (settings.groq_key_pool) — на реальном объёме main упирается в дневной токен-лимит
# ключа. browse НЕ использует пул и никогда не пишет по этому ключу — у него свой
# изолированный ключ. Шаблон по circuit — для единообразия с KEY_RATE_LLM_*.
KEY_GROQ_POOL_BLOCKED = "groq:pool_blocked:{circuit}:{index}"

# ETag/Last-Modified кэш ответа фида, чтобы не тянуть неизменившийся фид целиком.
# Шаблонизировано по той же причине, что и heartbeat выше: два независимых коллектора
# не должны делить один кэш ответа на два разных URL-запроса (разные наборы источников).
KEY_FEED_ETAG = "feed:etag:{circuit}"
KEY_FEED_LAST_MODIFIED = "feed:last_modified:{circuit}"

CHANNEL_ALERTS = "channel:alerts"  # pub/sub -> бот пересылает владельцу


_async_state: dict = {"loop": None, "client": None}


def get_redis() -> aioredis.Redis:
    """Один клиент на event loop.

    Та же оговорка, что в app/db.py: Celery-задачи создают новый loop на каждый вызов,
    а клиент со старым пулом к нему не подходит. Плюс без кэша каждый вызов из хендлера
    бота плодил бы новый пул соединений.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return aioredis.from_url(settings.redis_url, decode_responses=True)
    if _async_state["loop"] is not loop:
        _async_state["client"] = aioredis.from_url(settings.redis_url, decode_responses=True)
        _async_state["loop"] = loop
    return _async_state["client"]


def get_sync_redis() -> sync_redis.Redis:
    """Для коллектора и Celery-воркера — там синхронный цикл, не asyncio."""
    return sync_redis.from_url(settings.redis_url, decode_responses=True)
