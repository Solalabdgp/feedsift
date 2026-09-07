"""Схема БД.

Источники (sources) -> сырые записи (raw_items) -> совпадения (matches),
плюс LLM-поля в matches и таблица llm_usage_log.
"""
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(Text, primary_key=True)  # имя источника как в URL фида
    category: Mapped[str] = mapped_column(Text, nullable=False, default="other")
    # job_board|webdev_business|ai_automation|startup_mvp|leadgen_agency|business_ops|browse_niche|other
    circuit: Mapped[str] = mapped_column(Text, nullable=False, default="main")
    # main|browse — источник истины для контура. collector.py фильтрует список источников
    # по нему; worker.py читает через SourceCache и денормализует на RawItem/Match.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    muted_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    added_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class RawItem(Base):
    __tablename__ = "raw_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, ForeignKey("sources.name"), nullable=False)
    # Денормализация Source.circuit на момент приёма записи — тот же паттерн,
    # что уже применён к source/tag/author_handle в этой модели (не join).
    circuit: Mapped[str] = mapped_column(Text, nullable=False, default="main")
    item_id: Mapped[str] = mapped_column(Text, nullable=False)  # идентификатор записи из фида
    author_handle: Mapped[str | None] = mapped_column(Text)
    permalink: Mapped[str | None] = mapped_column(Text)
    tag: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)  # title + body
    text_norm: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    simhash: Mapped[int] = mapped_column(BigInteger, nullable=False)
    has_body: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    struct_features: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reject_rule: Mapped[str | None] = mapped_column(Text)  # NULL = прошёл жёсткие фильтры
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("raw_items.id"), nullable=False)
    # Денормализация от Source.circuit через RawItem — так /browse в боте фильтрует
    # matches без join. Индекс см. миграцию 0003 (ix_matches_circuit_status).
    circuit: Mapped[str] = mapped_column(Text, nullable=False, default="main")
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    signals: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    stack_matched: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    intent_tag: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    # hiring_intent|pain_point|both|unknown
    compensation: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text)

    decided_by: Mapped[str] = mapped_column(Text, nullable=False, default="rules")
    # rules|llm|rules_only  (rules_only = LLM был нужен, но квота исчерпана — fail-closed)
    llm_verdict: Mapped[dict | None] = mapped_column(JSONB)
    llm_summary_ru: Mapped[str | None] = mapped_column(Text)
    llm_prob: Mapped[float | None] = mapped_column(Float)
    llm_status: Mapped[str] = mapped_column(Text, nullable=False, default="not_needed")
    # not_needed|pending|done|skipped_quota|failed

    rules_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str | None] = mapped_column(Text)
    duplicate_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    duplicate_of: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("matches.id"))
    notified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    notification_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    # new|awaiting_llm|notified|suppressed|digest_pending — общий статус-флоу для
    # ОБОИХ контуров (main и browse): browse уведомляется точно так же, как main,
    # той же NOTIFY_QUEUE/DIGEST_QUEUE (см. app/notify_router.py). circuit выше
    # различает контуры только для отчётности/фильтрации, не для статус-флоу.
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    raw_item: Mapped["RawItem"] = relationship(lazy="joined")


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("matches.id"), nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)  # good|bad
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dict_type: Mapped[str] = mapped_column(Text, nullable=False)
    # stack_core|stack_periph|hiring_intent|pain_point|money|anti|closed|noise|alias
    term: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    maps_to: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RuleWeight(Base):
    __tablename__ = "rule_weights"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    weight: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Author(Base):
    __tablename__ = "authors"

    handle: Mapped[str] = mapped_column(Text, primary_key=True)
    good_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    bad_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    reputation: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class LlmUsageLog(Base):
    __tablename__ = "llm_usage_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int | None] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(Text, nullable=False)  # classify_and_translate|translate
    # Для расщепления /quota и weekly_report по контурам (два разных ключа/бюджета).
    circuit: Mapped[str] = mapped_column(Text, nullable=False, default="main")
    model: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)  # ok|quota_exhausted|error
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
