"""Проверка пароля владельца: Argon2id поверх HMAC-пеппера.

Два секрета вместо одного. DASHBOARD_PASSWORD_HASH — то, что можно увидеть
в `docker inspect`, в бэкапе .env, в выводе упавшего процесса. DASHBOARD_PASSWORD_PEPPER —
второй секрет, который в сам хэш не входит. Пароль перед Argon2 сворачивается
в HMAC-SHA256(pepper, password), поэтому утёкший хэш без пеппера не перебирается
офлайн вообще: у атакующего нет функции, вычисляющей кандидата.

Побочный эффект HMAC полезен сам по себе: на вход Argon2 всегда приходит ровно
64 шестнадцатеричных символа. Длина присланного пароля на стоимость хэширования
не влияет, то есть «пароль на мегабайт» в теле запроса не превращается
в дорогой вызов KDF.

Параметры Argon2id — рекомендация OWASP для варианта m=64 MiB: m=65536 KiB,
t=3, p=4. Один вход в 12 часов, задержка в десятки миллисекунд здесь бесплатна.

Сгенерировать хэш для .env:

    python -m app.auth.security
"""
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher, Type
from argon2.exceptions import (
    HashingError,
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

from app.config import settings

_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # KiB, то есть 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

def _dummy_hash() -> str:
    """Валидный хэш заведомо неизвестного пароля, считается один раз лениво.

    Нужен, чтобы путь «хэш не настроен» стоил ровно столько же, сколько путь
    «пароль неверный»: иначе по времени ответа снаружи видно, настроен дашборд
    или нет. Константой не зашит намеренно — захардкоженная PHC-строка
    протухает вместе с параметрами _HASHER, а невалидная отвергается мгновенно
    и ровно этот замер времени и возвращает.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _HASHER.hash(secrets.token_hex(32))
    return _DUMMY_HASH


_DUMMY_HASH: str | None = None


def _peppered(password: str) -> str:
    """Пароль -> HMAC-SHA256(pepper, password) в hex. Это и есть вход Argon2."""
    return hmac.new(
        settings.dashboard_password_pepper.encode("utf-8"),
        password.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def hash_password(password: str) -> str:
    """PHC-строка для DASHBOARD_PASSWORD_HASH. Зависит от текущего пеппера:
    смена пеппера обесценивает старый хэш, пароль придётся перехэшировать."""
    return _HASHER.hash(_peppered(password))


def verify_owner_password(password: str) -> bool:
    """True, если пароль совпал с DASHBOARD_PASSWORD_HASH.

    Исключения argon2 наружу не выпускаются: у вызывающего нет сценария, где
    «хэш битый» и «пароль не тот» обрабатывались бы по-разному, а 500 вместо 401
    на кривом .env — это ещё и сигнал атакующему, что конфиг сломан.
    """
    stored = settings.dashboard_password_hash
    if not stored:
        _burn_time(password)
        return False
    try:
        _HASHER.verify(stored, _peppered(password))
    except (
        VerifyMismatchError,
        VerificationError,
        InvalidHashError,
        HashingError,
        # UnicodeEncodeError (подкласс ValueError) и TypeError — это не «пароль не
        # тот», а «в DASHBOARD_PASSWORD_HASH лежит не PHC-строка»: argon2 требует
        # ASCII и падает на любом другом содержимом ДО сравнения. Без этой ветки
        # опечатка в .env превращала бы вход не в 401, а в 500 с трейсбеком.
        ValueError,
        TypeError,
    ):
        return False
    return True


def _burn_time(password: str) -> None:
    try:
        _HASHER.verify(_dummy_hash(), _peppered(password))
    except Exception:  # noqa: BLE001 — результат не нужен, нужна потраченная работа
        pass


def _main() -> None:
    import getpass
    import os

    if not settings.dashboard_password_pepper:
        pepper = secrets.token_urlsafe(32)
        os.environ["DASHBOARD_PASSWORD_PEPPER"] = pepper
        settings.dashboard_password_pepper = pepper
        print("DASHBOARD_PASSWORD_PEPPER не был задан, сгенерирован новый.")
        print(f"DASHBOARD_PASSWORD_PEPPER={pepper}")

    first = getpass.getpass("Пароль дашборда: ")
    if len(first) < 12:
        raise SystemExit("Слишком короткий пароль: минимум 12 символов.")
    if first != getpass.getpass("Повтори: "):
        raise SystemExit("Пароли не совпали.")

    print(f"DASHBOARD_PASSWORD_HASH={hash_password(first)}")
    print("\nОбе строки — в .env. Сам пароль никуда не записывать.")


if __name__ == "__main__":
    _main()
