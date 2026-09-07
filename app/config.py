from datetime import time

from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Контур (main|browse) ---
    # Второй, независимый мониторинг-контур поверх того же стека. Одно и то же поле
    # Settings (source_feed_*, groq_api_key/model, groq_rpm_limit/rpd_limit) резолвится
    # по-разному в двух контейнерах через разные env — сам код не форкается.
    # circuit читает: collector.py (какой список источников опрашивать),
    # llm.py/celery_app.py (префикс ключей квоты в Redis), redis_client.py
    # (шаблон ETag/heartbeat-ключей). worker.py и bot.py — процессы ОБЩИЕ на оба
    # контура: контур записи определяется через Source.circuit из БД, а не через env.
    circuit: str = "main"
    # Какие circuit-коллекторы проверяет heartbeat_check (app/celery_app.py).
    monitored_collector_circuits: str = "main"

    @property
    def monitored_collector_circuits_list(self) -> list[str]:
        return [c.strip() for c in self.monitored_collector_circuits.split(",") if c.strip()]

    # --- Feed adapter (collector) ---
    # Учётные данные фида. По сути read-credentials — хранить как пароль.
    source_feed_user: str = ""
    source_feed_token: str = ""
    source_user_agent: str = "feedsift/0.1"
    # Шаблон URL фида. Дефолта нет намеренно: адрес источника задаётся через env,
    # чтобы не зашивать конкретного провайдера в код. {sources} подставляется
    # склейкой имён источников (см. app/collector.py:build_url).
    source_feed_url_template: str = ""
    poll_interval_min_sec: int = 90
    poll_interval_max_sec: int = 120
    feed_limit: int = 100
    max_item_age_min: int = 360

    # --- Telegram bot (notifier) ---
    telegram_bot_token: str = ""
    telegram_owner_id: int = 0

    # --- LLM ---
    groq_api_key: str = ""
    groq_model: str = "qwen/qwen3.8-27b"
    groq_timeout_sec: float = 60.0
    # Связывающее ограничение — токены, а не запросы: промпт сам по себе занимает
    # заметную долю окна, поэтому лимит запросов в минуту держим низким.
    groq_rpm_limit: int = 2
    groq_rpd_limit: int = 900
    groq_celery_rate_limit: str = "2/m"
    llm_enabled: bool = True

    # --- Пул запасных ключей (main-контур) ---
    # При 429 от текущего ключа app/llm.py (call_llm_with_pool) пробует следующий
    # доступный ключ из пула для ТОГО ЖЕ вызова. Плоские переменные, а не список:
    # pydantic-settings не парсит списки из простых env-строк без доп. конфигурации.
    # ТОЛЬКО для main — у browse свой изолированный groq_api_key, пул не трогает.
    groq_api_key_main_pool_1: str = ""
    groq_api_key_main_pool_2: str = ""
    groq_api_key_main_pool_3: str = ""

    @property
    def groq_key_pool(self) -> list[str]:
        """[groq_api_key] + доступные запасные, в порядке ротации. Незаданные (пустые)
        пропускаются — пул деградирует до одного ключа, если часть переменных не задана,
        и до пустого списка (единый путь через settings.groq_api_key), если пул вообще
        не используется — см. call_llm_with_pool."""
        keys = [
            self.groq_api_key,
            self.groq_api_key_main_pool_1,
            self.groq_api_key_main_pool_2,
            self.groq_api_key_main_pool_3,
        ]
        return [k for k in keys if k]

    llm_confidence_threshold: float = 0.55

    # --- Infra ---
    # Дефолта НЕТ намеренно: в строке подключения лежит пароль Postgres, и зашитый
    # дефолт означал бы, что при выпадении переменной из .env сервис молча
    # поднимется с общеизвестным паролем. Пусто + явный отказ на старте —
    # тот же приём, что для source_feed_* и telegram_bot_token.
    database_url: str = ""
    # У redis_url дефолт оставлен: в нём нет учётных данных, контейнер не публикует порт.
    redis_url: str = "redis://redis:6379/0"

    # --- Scoring ---
    notify_threshold: int = 10
    grey_low: int = 6
    grey_high: int = 14
    # Минимальный балл, с которого запись, опознанная ТОЛЬКО мягким сигналом
    # "вопрос по теме", имеет право на вызов LLM. Отдельная ручка от grey_low:
    # короткий валидный вопрос набирает мало баллов из-за штрафа за длину,
    # и порогом grey_low отсекался бы зря.
    help_request_min_score: int = 3
    stack_score_cap: int = 9
    intent_score_cap: int = 8
    noise_penalty_cap: int = -8

    # --- General ---
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"
    tz: str = "UTC"
    rules_version: str = "v1"
    log_level: str = "INFO"
    log_format: str = "console"  # console|json
    retention_days: int = 30

    @property
    def quiet_start(self) -> time:
        return _parse_hhmm(self.quiet_hours_start)

    @property
    def quiet_end(self) -> time:
        return _parse_hhmm(self.quiet_hours_end)


settings = Settings()

_HINTS = {
    "database_url": "DATABASE_URL",
    "redis_url": "REDIS_URL",
    "source_feed_user": "SOURCE_FEED_USER",
    "source_feed_token": "SOURCE_FEED_TOKEN",
    "source_feed_url_template": "SOURCE_FEED_URL_TEMPLATE",
    "groq_api_key": "GROQ_API_KEY",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_owner_id": "TELEGRAM_OWNER_ID",
}


def require(*names: str) -> None:
    """Отказ на старте, если обязательная настройка не задана.

    Вызывается из main() каждого сервиса. Смысл — не дать процессу подняться
    наполовину рабочим и молча делать не то: без пароля БД, без учётных данных
    фида, без токена бота. Список требуемого у каждого сервиса свой.
    """
    missing = [n for n in names if not getattr(settings, n, None)]
    if missing:
        details = ", ".join(_HINTS.get(n, n.upper()) for n in missing)
        raise RuntimeError(f"Не заданы обязательные настройки: {details}. Проверь .env.")
