"""CRM-слой поверх matches: отдельная таблица lead_crm (1:1)

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-16

Веб-дашборд ведёт воронку по тем же лидам, что уже лежат в matches, но его
состояние (написал / не написал / отложил, ответ клиента, взят в работу,
заработок) — это ДРУГАЯ предметная область, чем matches.status.
matches.status — технический статус доставки в Telegram
(new|awaiting_llm|notified|suppressed|digest_pending), его пишет бот
(app/bot.py: UPDATE matches SET status='notified'). Смешивать их в одной
колонке нельзя, и в одной таблице — не нужно.

Почему отдельная таблица, а не ADD COLUMN в matches:
  1. Ноль DDL на горячей прод-таблице. CREATE TABLE не берёт ACCESS EXCLUSIVE
     на matches и не трогает её каталог вообще; бот на время миграции даже
     не замечает её выполнения.
  2. Модель Match в app/models.py остаётся байт-в-байт прежней — нечему
     ломаться при рассинхроне «код бота обновили / миграцию не накатили»
     и наоборот. Деплой дашборда и деплой бота развязаны.
  3. UPDATE в Postgres = новая версия строки целиком. matches — широкая
     (signals/llm_verdict JSONB, stack_matched ARRAY), и правки CRM гоняли бы
     весь этот вес по heap при каждом клике в дашборде. Узкая lead_crm держит
     bloat при себе.
  4. Данные разреженные: CRM-касание получит меньшинство лидов.
  5. Агрегация от этого не усложняется, а упрощается: «написано / в работе /
     заработок» целиком считаются по lead_crm одним GROUP BY без join,
     join нужен только для списка лидов в дашборде (LEFT JOIN по уникальному
     match_id).

Отсутствие строки в lead_crm = «не написал». Строка заводится лениво, первым
CRM-действием, через INSERT ... ON CONFLICT (match_id) DO UPDATE. Дашборд
читает состояние как coalesce(c.contact_state, 'not_contacted').

Удаление лида из CRM-вида — soft delete (deleted_at). Обоснование в отчёте
к задаче; коротко: hard delete строки lead_crm не ломает FK (duplicate_of
смотрит на matches, а не сюда), но теряет историю и заработок, а сам лид всё
равно остался бы в matches и вернулся бы в список как «не написал» — признак
скрытия обязан быть персистентным. Строки matches не удаляются никогда.

contact_state — Text + CHECK, а не PG ENUM: так же, как status/intent_tag/
llm_status в 0001, плюс список значений правится обычной миграцией, а не
ALTER TYPE (значение из enum в Postgres не удаляется вовсе).

Аддитивная и обратимая: downgrade сносит только собственную таблицу.
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# not_contacted — дефолт; contacted — написал; postponed — не написал, отложил
CONTACT_STATES = ("not_contacted", "contacted", "postponed")


def upgrade() -> None:
    op.create_table(
        "lead_crm",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "match_id",
            sa.BigInteger,
            sa.ForeignKey("matches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contact_state", sa.Text, nullable=False, server_default="not_contacted"),
        sa.Column("reply_text", sa.Text),
        sa.Column("taken_in_work", sa.Boolean),  # NULL = ответа ещё нет / решения нет
        sa.Column("earnings", sa.Numeric(12, 2)),
        sa.Column("currency", sa.Text, nullable=False, server_default="USD"),
        sa.Column("contacted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("in_work_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True)),  # soft delete
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # 1:1 с matches. UNIQUE обязателен: он и даёт конфликт-таргет для
        # ON CONFLICT (match_id) при ленивом заведении строки.
        sa.UniqueConstraint("match_id", name="uq_lead_crm_match"),
        sa.CheckConstraint(
            "contact_state IN ('not_contacted', 'contacted', 'postponed')",
            name="ck_lead_crm_contact_state",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_lead_crm_currency"),
        sa.CheckConstraint(
            "earnings IS NULL OR earnings >= 0", name="ck_lead_crm_earnings_non_negative"
        ),
        # Сумма имеет смысл только у взятого в работу — иначе она утекала бы
        # в статистику заработка из лидов, по которым сделки не было.
        sa.CheckConstraint(
            "earnings IS NULL OR taken_in_work IS TRUE", name="ck_lead_crm_earnings_requires_work"
        ),
        sa.CheckConstraint(
            "contact_state <> 'contacted' OR contacted_at IS NOT NULL",
            name="ck_lead_crm_contacted_at",
        ),
        sa.CheckConstraint(
            "taken_in_work IS NOT TRUE OR in_work_at IS NOT NULL", name="ck_lead_crm_in_work_at"
        ),
    )

    # Партиал под воронку/статистику: удалённые из вида в неё не попадают,
    # индекс не несёт их вес.
    op.create_index(
        "ix_lead_crm_state",
        "lead_crm",
        ["contact_state"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # Лента «последние касания» в дашборде.
    op.create_index("ix_lead_crm_updated", "lead_crm", [sa.text("updated_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_lead_crm_updated", table_name="lead_crm")
    op.drop_index("ix_lead_crm_state", table_name="lead_crm")
    op.drop_table("lead_crm")
