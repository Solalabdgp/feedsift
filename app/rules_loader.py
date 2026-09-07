from sqlalchemy import select

from app.db import SessionLocal
from app.models import Keyword, RuleWeight
from app.rules_seed import empty_cache
from app.scoring import RulesCache


async def load_rules_cache(version: int = 0) -> RulesCache:
    async with SessionLocal() as session:
        kw_rows = (await session.execute(select(Keyword).where(Keyword.enabled.is_(True)))).scalars().all()
        rw_rows = (await session.execute(select(RuleWeight))).scalars().all()

    cache = empty_cache(version)
    for row in kw_rows:
        if row.dict_type in ("stack_core", "stack_periph"):
            cache.stack_weights[row.term] = row.weight
        elif row.dict_type == "hiring_intent":
            cache.hiring_weights[row.term] = row.weight
        elif row.dict_type == "pain_point":
            cache.pain_weights[row.term] = row.weight
        elif row.dict_type == "money":
            cache.money_weights[row.term] = row.weight
        elif row.dict_type == "anti":
            cache.anti_terms.append(row.term)
        elif row.dict_type == "closed":
            cache.closed_terms.append(row.term)
        elif row.dict_type == "noise":
            cache.noise_weights[row.term] = row.weight
        elif row.dict_type == "alias" and row.maps_to:
            cache.aliases[row.term] = row.maps_to

    for row in rw_rows:
        if row.key == "__notify_threshold__":
            cache.notify_threshold = row.weight
        else:
            cache.rule_weights[row.key] = row.weight

    cache.compile()
    return cache
