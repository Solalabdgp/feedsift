"""seed sources, keyword dictionaries and rule weights

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-25

Единая seed-миграция. Данные — плейсхолдеры из app/sources.py и app/dictionaries.py:
боевые списки источников и словари живут в БД и правятся на живую из бота
(/keywords add ..., /category ...), в код они не возвращаются.

INSERT ... ON CONFLICT DO NOTHING, а не bulk_insert с DELETE: миграция должна быть
безопасна для повторного прогона и не должна затирать ручные правки весов,
сделанные через бота.
"""
import os
import sys

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

sys.path.insert(0, os.getcwd())
from app.dictionaries import (  # noqa: E402
    ALIASES,
    ANTI,
    CLOSED,
    HIRING_INTENT_STRONG,
    HIRING_INTENT_WEAK,
    MONEY,
    NOISE,
    OFF_NICHE,
    PAIN_POINT,
    RULE_WEIGHTS,
    STACK_CORE,
    STACK_PERIPH,
)
from app.sources import SOURCES  # noqa: E402

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

sources = sa.table(
    "sources",
    sa.column("name", sa.Text),
    sa.column("category", sa.Text),
    sa.column("priority", sa.SmallInteger),
    sa.column("enabled", sa.Boolean),
)

keywords = sa.table(
    "keywords",
    sa.column("dict_type", sa.Text),
    sa.column("term", sa.Text),
    sa.column("weight", sa.SmallInteger),
    sa.column("maps_to", sa.Text),
    sa.column("enabled", sa.Boolean),
)

rule_weights = sa.table(
    "rule_weights",
    sa.column("key", sa.Text),
    sa.column("weight", sa.SmallInteger),
)


def _terms(dict_type: str, source: list[str], weight: int) -> list[dict]:
    out, seen = [], set()
    for t in source:
        term = t.lower().strip()
        if term and term not in seen:
            seen.add(term)
            out.append(
                {"dict_type": dict_type, "term": term, "weight": weight, "maps_to": None,
                 "enabled": True}
            )
    return out


def upgrade() -> None:
    conn = op.get_bind()

    for name, category, priority in SOURCES:
        conn.execute(
            pg_insert(sources)
            .values(name=name, category=category, priority=priority, enabled=True)
            .on_conflict_do_nothing(index_elements=["name"])
        )

    rows: list[dict] = []
    rows += _terms("stack_core", STACK_CORE, 3)
    rows += _terms("stack_periph", STACK_PERIPH, 1)
    rows += _terms("hiring_intent", HIRING_INTENT_STRONG, 4)
    rows += _terms("hiring_intent", HIRING_INTENT_WEAK, 2)
    rows += _terms("pain_point", PAIN_POINT, 3)
    rows += _terms("money", MONEY, 2)
    rows += _terms("anti", ANTI, 0)
    rows += _terms("closed", CLOSED, 0)
    rows += _terms("noise", NOISE, -4)
    rows += _terms("noise", OFF_NICHE, -5)
    rows += [
        {"dict_type": "alias", "term": k, "weight": 0, "maps_to": v, "enabled": True}
        for k, v in ALIASES.items()
    ]

    # (dict_type, term) уникальны: один термин мог попасть и в core, и в periph,
    # и в strong, и в weak. Побеждает первое вхождение — то есть более сильный вес.
    dedup: dict[tuple[str, str], dict] = {}
    for r in rows:
        dedup.setdefault((r["dict_type"], r["term"]), r)
    for r in dedup.values():
        conn.execute(
            pg_insert(keywords).values(**r).on_conflict_do_nothing(
                index_elements=["dict_type", "term"]
            )
        )

    for key, weight in RULE_WEIGHTS.items():
        conn.execute(
            pg_insert(rule_weights)
            .values(key=key, weight=weight)
            .on_conflict_do_nothing(index_elements=["key"])
        )


def downgrade() -> None:
    op.execute("DELETE FROM rule_weights")
    op.execute("DELETE FROM keywords")
    op.execute(
        "DELETE FROM sources WHERE NOT EXISTS "
        "(SELECT 1 FROM raw_items WHERE raw_items.source = sources.name)"
    )
