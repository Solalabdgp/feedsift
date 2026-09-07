"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-25

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("category", sa.Text, nullable=False, server_default="other"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("muted_until", sa.TIMESTAMP(timezone=True)),
        sa.Column("priority", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("added_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True)),
    )

    op.create_table(
        "raw_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text, sa.ForeignKey("sources.name"), nullable=False),
        sa.Column("item_id", sa.Text, nullable=False),
        sa.Column("author_handle", sa.Text),
        sa.Column("permalink", sa.Text),
        sa.Column("tag", sa.Text),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("text_norm", sa.Text, nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("simhash", sa.BigInteger, nullable=False),
        sa.Column("has_body", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("struct_features", JSONB, nullable=False, server_default="{}"),
        sa.Column("reject_rule", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("item_id", name="uq_raw_item_id"),
    )
    op.create_index("ix_raw_items_posted_at", "raw_items", [sa.text("posted_at DESC")])
    op.create_index("ix_raw_items_hash", "raw_items", ["text_hash"])
    op.create_index("ix_raw_items_source", "raw_items", ["source"])
    op.create_index(
        "ix_raw_items_passed", "raw_items", ["id"], postgresql_where=sa.text("reject_rule IS NULL")
    )

    op.create_table(
        "matches",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("raw_item_id", sa.BigInteger, sa.ForeignKey("raw_items.id"), nullable=False),
        sa.Column("score", sa.SmallInteger, nullable=False),
        sa.Column("signals", JSONB, nullable=False, server_default="{}"),
        sa.Column("stack_matched", sa.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("intent_tag", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("compensation", sa.Text),
        sa.Column("contact", sa.Text),
        sa.Column("headline", sa.Text),
        sa.Column("decided_by", sa.Text, nullable=False, server_default="rules"),
        sa.Column("llm_verdict", JSONB),
        sa.Column("llm_summary_ru", sa.Text),
        sa.Column("llm_prob", sa.Float),
        sa.Column("llm_status", sa.Text, nullable=False, server_default="not_needed"),
        sa.Column("rules_version", sa.Text, nullable=False),
        sa.Column("model_version", sa.Text),
        sa.Column("duplicate_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("duplicate_of", sa.BigInteger, sa.ForeignKey("matches.id")),
        sa.Column("notified_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("notification_msg_id", sa.BigInteger),
        sa.Column("is_favorite", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_matches_created", "matches", [sa.text("created_at DESC")])
    op.create_index("ix_matches_status", "matches", ["status"])
    op.create_index("ix_matches_raw_item", "matches", ["raw_item_id"])

    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("match_id", sa.BigInteger, sa.ForeignKey("matches.id"), nullable=False),
        sa.Column("verdict", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_feedback_created", "feedback", [sa.text("created_at DESC")])

    op.create_table(
        "keywords",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("dict_type", sa.Text, nullable=False),
        sa.Column("term", sa.Text, nullable=False),
        sa.Column("weight", sa.SmallInteger, nullable=False, server_default="1"),
        sa.Column("maps_to", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("dict_type", "term", name="uq_keywords_type_term"),
    )

    op.create_table(
        "rule_weights",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("weight", sa.SmallInteger, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "authors",
        sa.Column("handle", sa.Text, primary_key=True),
        sa.Column("good_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("bad_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("reputation", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "llm_usage_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("match_id", sa.BigInteger),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger),
        sa.Column("output_tokens", sa.BigInteger),
        sa.Column("latency_ms", sa.BigInteger),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_llm_usage_created", "llm_usage_log", [sa.text("created_at DESC")])

    op.create_table(
        "settings",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )


def downgrade() -> None:
    for table in (
        "settings", "llm_usage_log", "authors", "rule_weights",
        "keywords", "feedback", "matches", "raw_items", "sources",
    ):
        op.drop_table(table)
