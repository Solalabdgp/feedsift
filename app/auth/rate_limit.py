"""Защита логина от перебора: счётчик неудач по IP и растущая блокировка.

Уровней защиты два, и они решают разные задачи.

1. slowapi (`limiter`, 5/минуту на роут) — процессный, в памяти. Дешёвый отсекатель
   шквала: не пускает поток запросов даже до Argon2, который стоит 64 MiB и
   десятки миллисекунд. При перезапуске обнуляется — и это нормально, его работа
   измеряется секундами.

2. Этот модуль — в Redis, переживает перезапуск и общий для всех воркеров uvicorn.
   Он и есть настоящая защита: 5 неудач за 15 минут -> блокировка, каждая
   следующая серия блокирует дольше (1м, 5м, 15м, 1ч, 6ч). Смысл лестницы —
   развести человека, который трижды промахнулся мимо раскладки, и скрипт:
   первому стоит минуту подождать, второму за сутки достанется несколько
   десятков попыток вместо миллионов.

Счётчик per-IP, и это осознанный размен. Ботнет с тысячи адресов лестницу
обойдёт — против него работает энтропия пароля и Argon2. Зато один
заблокированный адрес не выключает вход для владельца, а глобальный
счётчик «на аккаунт» именно это бы и делал: любой желающий закрывал бы
владельцу доступ в собственный дашборд, отправляя мусор на /auth/login.
"""
import structlog
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.redis_client import get_redis

log = structlog.get_logger(__name__)

KEY_FAIL = "dash:login:fail:{ip}"  # неудач в текущем окне
KEY_LOCK = "dash:login:lock:{ip}"  # активная блокировка, TTL = её остаток
KEY_LEVEL = "dash:login:locklvl:{ip}"  # какая по счёту блокировка, для лестницы

# Лестница блокировок, секунды: 1м, 5м, 15м, 1ч, 6ч. Дальше держится 6ч.
LOCK_LADDER = (60, 300, 900, 3600, 21600)
# Уровень лестницы живёт сутки: серия попыток вчера не должна наказывать сегодня.
LEVEL_TTL_SEC = 86400


def client_ip(request: Request) -> str:
    """IP клиента с оглядкой на реверс-прокси.

    X-Forwarded-For берётся ТОЛЬКО при dashboard_trust_proxy и только последним
    элементом. Последний — тот, что дописал наш собственный прокси; всё, что
    левее, прислал клиент, и подделать это ничего не стоит. Читать оттуда
    первый элемент (частая ошибка) означало бы позволить кому угодно
    обнулять себе счётчик блокировок одним заголовком.
    """
    if settings.dashboard_trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            if hops:
                return hops[-1]
    return get_remote_address(request) or "unknown"


# key_func тот же, что у ручного счётчика: иначе два уровня защиты считали бы
# «клиента» по-разному и slowapi лимитировал бы сам прокси целиком.
limiter = Limiter(key_func=client_ip)


async def is_locked(ip: str) -> int:
    """Секунд до конца блокировки. 0 — не заблокирован."""
    ttl = await get_redis().ttl(KEY_LOCK.format(ip=ip))
    # -2 — ключа нет, -1 — ключ без TTL (такого мы не создаём, но подстрахуемся).
    return ttl if ttl and ttl > 0 else 0


async def register_failure(ip: str) -> int:
    """Учесть неудачный вход. Возвращает длину назначенной блокировки (0 — ещё нет)."""
    redis = get_redis()
    fail_key = KEY_FAIL.format(ip=ip)

    fails = await redis.incr(fail_key)
    if fails == 1:
        # Окно стартует с первой неудачи и НЕ продлевается следующими: иначе
        # окно уезжало бы вперёд от каждой попытки и никогда не закрывалось.
        await redis.expire(fail_key, settings.dashboard_login_window_sec)

    if fails < settings.dashboard_login_max_attempts:
        log.warning("dashboard.login.failed", ip=ip, fails=fails)
        return 0

    level = await redis.incr(KEY_LEVEL.format(ip=ip))
    await redis.expire(KEY_LEVEL.format(ip=ip), LEVEL_TTL_SEC)
    delay = LOCK_LADDER[min(level, len(LOCK_LADDER)) - 1]

    await redis.set(KEY_LOCK.format(ip=ip), str(level), ex=delay)
    # Счётчик обнуляется вместе с назначением блокировки: следующая серия
    # считается с нуля, а «сколько серий было» помнит KEY_LEVEL.
    await redis.delete(fail_key)
    log.warning("dashboard.login.locked", ip=ip, level=level, delay_sec=delay)
    return delay


async def register_success(ip: str) -> None:
    """Успешный вход снимает и счётчик, и лестницу: адрес доказал, что свой."""
    redis = get_redis()
    await redis.delete(
        KEY_FAIL.format(ip=ip),
        KEY_LOCK.format(ip=ip),
        KEY_LEVEL.format(ip=ip),
    )
