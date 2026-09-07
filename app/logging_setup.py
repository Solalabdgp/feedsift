"""Явная настройка structlog. Вызывается из main() каждого сервиса.

Зачем явно, если и на дефолтах работает: дефолтный `ConsoleRenderer` выбирает
форматтер исключений по тому, установлен ли пакет `rich`. Сейчас его нет, и
трейсбек печатается plain. Но стоит любой транзитивной зависимости притащить
`rich` — structlog переключится на `rich_traceback`, а тот печатает локальные
переменные кадров стека. В кадрах этого проекта лежат учётные данные фида
(collector), ключ LLM (llm) и тела чужих записей. То есть безопасность логов
сейчас держится на случайности состава зависимостей — это и чинится.

Форматтер исключений зафиксирован как plain_traceback, рендерер выбирается
через LOG_FORMAT: console (по умолчанию, для локальной отладки) или json (для прода).
"""
import logging
import sys

import structlog

from app.config import settings


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    # httpx на INFO печатает полный URL каждого запроса. У коллектора учётные данные
    # фида едут именно в query-строке, то есть креды оседали бы открытым текстом
    # в лог-файлах контейнера (50 МБ × 3 ротации на диске хоста).
    # WARNING оставляет видимыми настоящие проблемы транспорта и убирает URL.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if settings.log_format.lower() == "json":
        renderer: structlog.typing.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=False,
            # Явно, а не по наличию rich в окружении. Локальные переменные кадров
            # в лог не попадают ни при каких зависимостях.
            exception_formatter=structlog.dev.plain_traceback,
        )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
