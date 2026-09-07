"""Seed-список источников — плейсхолдеры.

Боевой список источников живёт в БД (таблица sources) и правится на живую из бота;
этот модуль нужен только как статический фолбэк на случай, если Postgres недоступен
в момент старта коллектора, и как форма данных для миграции-сида.

priority — прибавка к скорингу (слой D "контекст источника"), не порядок опроса.
"""

CATEGORY_JOB_BOARD = "job_board"
CATEGORY_WEBDEV = "webdev_business"
CATEGORY_AI = "ai_automation"
CATEGORY_STARTUP = "startup_mvp"
CATEGORY_LEADGEN = "leadgen_agency"
# Отдельный кластер: не доска найма и не техническая дискуссия, а владельцы малого
# бизнеса, которые описывают операционную рутину и НЕ формулируют это как заказ.
CATEGORY_BUSINESS_OPS = "business_ops"

# (имя источника как в URL фида, категория, priority)
SOURCES: list[tuple[str, str, int]] = [
    ("source-01", CATEGORY_JOB_BOARD, 3),
    ("source-02", CATEGORY_JOB_BOARD, 0),
    ("source-03", CATEGORY_WEBDEV, 1),
    ("source-04", CATEGORY_WEBDEV, 1),
    ("source-05", CATEGORY_WEBDEV, 0),
    ("source-06", CATEGORY_AI, 2),
    ("source-07", CATEGORY_AI, 2),
    ("source-08", CATEGORY_AI, 1),
    ("source-09", CATEGORY_STARTUP, 1),
    ("source-10", CATEGORY_STARTUP, 0),
    ("source-11", CATEGORY_LEADGEN, 1),
    ("source-12", CATEGORY_BUSINESS_OPS, 2),
    ("source-13", CATEGORY_BUSINESS_OPS, 1),
    ("source-14", CATEGORY_BUSINESS_OPS, 0),
]

SOURCE_NAMES = [name for name, _cat, _prio in SOURCES]

# --- Browse-контур ---
# Второй, независимый мониторинг поверх того же стека: низкоприоритетный
# "browsing"-поток для ручного просмотра через /browse, НЕ автоматизированные
# лид-уведомления. Одна и та же строка sources.name не может числиться в двух
# контурах одновременно (circuit — не relation, а функция от имени источника).
CATEGORY_BROWSE = "browse_niche"

SOURCES_BROWSE: list[tuple[str, str, int]] = [
    ("browse-source-01", CATEGORY_BROWSE, 0),
    ("browse-source-02", CATEGORY_BROWSE, 0),
    ("browse-source-03", CATEGORY_BROWSE, 0),
    ("browse-source-04", CATEGORY_BROWSE, 0),
    ("browse-source-05", CATEGORY_BROWSE, 0),
    ("browse-source-06", CATEGORY_BROWSE, 0),
]

SOURCE_NAMES_BROWSE = [name for name, _cat, _prio in SOURCES_BROWSE]
