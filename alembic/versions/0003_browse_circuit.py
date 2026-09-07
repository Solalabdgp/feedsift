"""browse-контур: колонка circuit на 4 таблицах + источники второго контура

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-30

Второй, независимый мониторинг-контур поверх того же стека — pull-модель через
/browse в боте, НЕ автоматизированные лид-уведомления. Дискриминатор `circuit`
(main|browse), не отдельная таблица: дешевле и не ломает переиспользование
route()/_format_card/process_item.

Аддитивная: ADD COLUMN с DEFAULT (не бьёт существующие строки), INSERT новых
источников через ON CONFLICT DO NOTHING. Веса/словари keywords НЕ трогает —
browse-контур сознательно обходит keyword-gate перед LLM (словари откалиброваны
под основной домен и дают дырявое покрытие в смежных нишах).

Источники держим списком прямо в миграции (не импортируем app.sources), чтобы
миграция была воспроизводима сама по себе, даже если модуль потом уедет дальше.
sources.name — первичный ключ, поэтому одна строка не может числиться в двух
контурах разом; пересечения с основным списком просто не заводятся.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

sources = sa.table(
    "sources",
    sa.column("name", sa.Text),
    sa.column("category", sa.Text),
    sa.column("circuit", sa.Text),
    sa.column("priority", sa.SmallInteger),
    sa.column("enabled", sa.Boolean),
)

CATEGORY_BROWSE = "browse_niche"
CIRCUIT_BROWSE = "browse"

BROWSE_SOURCES = [
    "browse-source-01",
    "browse-source-02",
    "browse-source-03",
    "browse-source-04",
    "browse-source-05",
    "browse-source-06",
]


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "sources",
        sa.Column("circuit", sa.Text, nullable=False, server_default="main"),
    )
    op.add_column(
        "raw_items",
        sa.Column("circuit", sa.Text, nullable=False, server_default="main"),
    )
    op.add_column(
        "matches",
        sa.Column("circuit", sa.Text, nullable=False, server_default="main"),
    )
    op.add_column(
        "llm_usage_log",
        sa.Column("circuit", sa.Text, nullable=False, server_default="main"),
    )

    op.create_index("ix_matches_circuit_status", "matches", ["circuit", "status"])

    for name in BROWSE_SOURCES:
        conn.execute(
            pg_insert(sources)
            .values(
                name=name,
                category=CATEGORY_BROWSE,
                circuit=CIRCUIT_BROWSE,
                priority=0,
                enabled=True,
            )
            .on_conflict_do_nothing(index_elements=["name"])
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM sources WHERE circuit = :c "
            "AND NOT EXISTS (SELECT 1 FROM raw_items WHERE raw_items.source = sources.name)"
        ).bindparams(c=CIRCUIT_BROWSE)
    )
    conn.execute(
        sa.text("UPDATE sources SET enabled = false WHERE circuit = :c").bindparams(
            c=CIRCUIT_BROWSE
        )
    )
    op.drop_index("ix_matches_circuit_status", table_name="matches")
    op.drop_column("llm_usage_log", "circuit")
    op.drop_column("matches", "circuit")
    op.drop_column("raw_items", "circuit")
    op.drop_column("sources", "circuit")
