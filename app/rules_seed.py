"""RulesCache прямо из app/dictionaries.py, без похода в БД.

Отдельный модуль (а не часть rules_loader) намеренно: rules_loader тянет app.db,
то есть создаёт engine к Postgres на импорте. Offline-прогону (scripts/offline_dryrun.py)
Postgres не нужен вовсе. В проде правила всегда читаются из таблицы keywords —
её можно править на живую из бота.
"""
from app.config import settings
from app.scoring import RulesCache


def empty_cache(version: int = 0) -> RulesCache:
    return RulesCache(
        version=version,
        stack_cap=settings.stack_score_cap,
        intent_cap=settings.intent_score_cap,
        noise_cap=settings.noise_penalty_cap,
        notify_threshold=settings.notify_threshold,
        rules_version_tag=settings.rules_version,
    )


def build_seed_rules() -> RulesCache:
    from app import dictionaries as d

    cache = empty_cache()
    for term in d.STACK_CORE:
        cache.stack_weights[term] = 3
    for term in d.STACK_PERIPH:
        cache.stack_weights.setdefault(term, 1)
    for term in d.HIRING_INTENT_STRONG:
        cache.hiring_weights[term] = 4
    for term in d.HIRING_INTENT_WEAK:
        cache.hiring_weights.setdefault(term, 2)
    for term in d.PAIN_POINT:
        cache.pain_weights[term] = 3
    for term in d.MONEY:
        cache.money_weights[term] = 2
    cache.anti_terms = list(d.ANTI)
    cache.closed_terms = list(d.CLOSED)
    for term in d.NOISE:
        cache.noise_weights[term] = -4
    for term in d.OFF_NICHE:
        cache.noise_weights[term] = -5
    cache.aliases = dict(d.ALIASES)
    cache.rule_weights = dict(d.RULE_WEIGHTS)
    cache.compile()
    return cache
