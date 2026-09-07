"""Celery: (1) очередь llm с rate_limit под квоту LLM, (2) beat-задачи обслуживания.

Развёрнуто двумя контейнерами:
  llm-worker: celery -A app.celery_app worker -Q llm --concurrency=1
  beat:       celery -A app.celery_app worker -B -Q maintenance --concurrency=1
Разные очереди — чтобы отчёты и retention не занимали слот под rate_limit LLM.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from celery import Celery
from celery.schedules import crontab
from sqlalchemy import delete, func, select, update

from app.config import require, settings
from app.db import SessionLocal
from app.llm import call_llm_with_pool, quota_acquire, quota_state
from app.logging_setup import configure_logging
from app.models import Feedback, LlmUsageLog, Match, RawItem
from app.notify_router import route
from app.redis_client import (
    CHANNEL_ALERTS,
    KEY_HEARTBEAT_COLLECTOR,
    get_redis,
    get_sync_redis,
)

log = structlog.get_logger("celery")

# Celery-сервисы (llm-worker, beat) стартуют через CLI, своего main() у них нет —
# поэтому настройка логов и проверка обязательных переменных делается на импорте модуля.
# Ключ LLM здесь не требуем: этот же модуль поднимает beat, которому ключ не нужен.
# Отсутствие ключа ловится в llm.call_llm явным RuntimeError.
configure_logging()
require("database_url", "redis_url")

celery_app = Celery("feedsift", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.timezone = settings.tz
celery_app.conf.task_default_queue = "maintenance"
celery_app.conf.task_routes = {"app.celery_app.analyze_match": {"queue": "llm"}}
celery_app.conf.task_annotations = {
    "app.celery_app.analyze_match": {"rate_limit": settings.groq_celery_rate_limit}
}
celery_app.conf.beat_schedule = {
    "weekly-quality-report": {
        "task": "app.celery_app.weekly_report",
        "schedule": crontab(day_of_week=1, hour=9, minute=0),
        "options": {"queue": "maintenance"},
    },
    "retention-cleanup": {
        "task": "app.celery_app.retention_cleanup",
        "schedule": crontab(hour=4, minute=30),
        "options": {"queue": "maintenance"},
    },
    "heartbeat-check": {
        "task": "app.celery_app.heartbeat_check",
        "schedule": 600.0,
        "options": {"queue": "maintenance"},
    },
}


async def _alert(text: str) -> None:
    redis = get_redis()
    await redis.publish(CHANNEL_ALERTS, text)


# --------------------------------------------------------------------------- LLM


async def _finalize(
    match_id: int, values: dict, will_notify: bool, score: int, circuit: str = "main"
) -> str:
    redis = get_redis()
    async with SessionLocal() as session:
        await session.execute(update(Match).where(Match.id == match_id).values(**values))
        status = await route(session, redis, match_id, score, will_notify, circuit=circuit)
        await session.commit()
    return status


async def _log_usage(
    match_id: int,
    mode: str,
    outcome: str,
    meta: dict | None,
    error: str | None,
    circuit: str = "main",
) -> None:
    async with SessionLocal() as session:
        session.add(
            LlmUsageLog(
                match_id=match_id,
                mode=mode,
                circuit=circuit,
                model=(meta or {}).get("model", settings.groq_model),
                outcome=outcome,
                prompt_tokens=(meta or {}).get("prompt_tokens"),
                output_tokens=(meta or {}).get("output_tokens"),
                latency_ms=(meta or {}).get("latency_ms"),
                error=error,
            )
        )
        await session.commit()


async def _load_match(match_id: int):
    async with SessionLocal() as session:
        match = (await session.execute(select(Match).where(Match.id == match_id))).scalar_one_or_none()
        if match is None:
            return None
        raw = (await session.execute(select(RawItem).where(RawItem.id == match.raw_item_id))).scalar_one()
        return match, raw


async def _analyze_async(
    match_id: int, mode: str, rule_hint: str, rules_fallback_notify: bool, circuit: str = "main"
) -> str:
    loaded = await _load_match(match_id)
    if loaded is None:
        return "match_gone"
    match, raw = loaded

    # Слой 2-3 ограничения квоты. Fail-closed: отказ, а НЕ ретрай.
    # circuit="main" -> ключи rate:groq:main:*. circuit="browse" -> свои ключи
    # rate:groq:browse:* и свой ключ провайдера через settings.groq_api_key ЭТОГО
    # процесса (llm-worker-browse поднимается с другим ключом в env,
    # см. docker-compose.yml) — расход browse не режет квоту основного пайплайна.
    sync_redis = get_sync_redis()
    if not settings.llm_enabled or not quota_acquire(sync_redis, circuit=circuit):
        await _log_usage(match_id, mode, "quota_exhausted", None, None, circuit=circuit)
        await _finalize(
            match_id,
            {"decided_by": "rules_only", "llm_status": "skipped_quota"},
            rules_fallback_notify,
            match.score,
            circuit=circuit,
        )
        return "skipped_quota"

    prompt_variant = "strict" if circuit == "main" else "browse"
    try:
        # call_llm_with_pool: для circuit="main" пробует ключи из settings.groq_key_pool
        # по очереди на 429, прежде чем сдаться (app/llm.py). Для любого другого контура
        # (browse) — прямой проброс в call_llm с одним ключом settings.groq_api_key
        # этого процесса.
        verdict, meta = call_llm_with_pool(
            title=raw.title,
            body=raw.text,
            source=raw.source,
            redis=sync_redis,
            circuit=circuit,
            mode=mode,
            rule_hint=rule_hint,
            prompt_variant=prompt_variant,
        )
    except Exception as exc:  # noqa: BLE001 — любой сбой = откат на правила, без ретрая
        await _log_usage(match_id, mode, "error", None, type(exc).__name__, circuit=circuit)
        await _finalize(
            match_id,
            {"decided_by": "rules_only", "llm_status": "failed"},
            rules_fallback_notify,
            match.score,
            circuit=circuit,
        )
        log.warning("llm_failed_fallback_to_rules", match_id=match_id, circuit=circuit, error=type(exc).__name__)
        return "failed"

    confidence = float(verdict.get("confidence") or 0.0)
    is_lead = bool(verdict.get("is_lead"))
    if mode == "translate":
        will_notify = True
    else:
        will_notify = is_lead and confidence >= settings.llm_confidence_threshold

    lead_type = verdict.get("lead_type")
    values = {
        "decided_by": "llm",
        "llm_status": "done",
        "llm_verdict": verdict,
        "llm_summary_ru": verdict.get("summary_ru"),
        "llm_prob": confidence,
        "model_version": meta.get("model"),
    }
    if lead_type in ("hiring_intent", "pain_point"):
        values["intent_tag"] = lead_type

    await _log_usage(match_id, mode, "ok", meta, None, circuit=circuit)
    await _finalize(match_id, values, will_notify, match.score, circuit=circuit)
    return "done"


@celery_app.task(bind=True, max_retries=0, name="app.celery_app.analyze_match")
def analyze_match(
    self,
    match_id: int,
    mode: str,
    rule_hint: str = "",
    rules_fallback_notify: bool = False,
    circuit: str = "main",
) -> str:
    """max_retries=0 намеренно: ретрай при исчерпанной квоте сжёг бы дневной бюджет.

    circuit="main" — дефолт: worker.py всегда передаёт circuit явно, но вызовы
    send_task без этого аргумента не сломаются.
    """
    return asyncio.run(_analyze_async(match_id, mode, rule_hint, rules_fallback_notify, circuit))


# ------------------------------------------------------------------- maintenance


async def _weekly_report_async() -> None:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    async with SessionLocal() as session:
        notified = (
            await session.execute(select(func.count()).select_from(Match).where(Match.notified_at >= since))
        ).scalar_one()
        good = (
            await session.execute(
                select(func.count()).select_from(Feedback).where(Feedback.created_at >= since, Feedback.verdict == "good")
            )
        ).scalar_one()
        bad = (
            await session.execute(
                select(func.count()).select_from(Feedback).where(Feedback.created_at >= since, Feedback.verdict == "bad")
            )
        ).scalar_one()
        llm_calls = (
            await session.execute(
                select(LlmUsageLog.outcome, func.count())
                .where(LlmUsageLog.created_at >= since)
                .group_by(LlmUsageLog.outcome)
            )
        ).all()
        bad_by_source = (
            await session.execute(
                select(RawItem.source, func.count())
                .select_from(Feedback)
                .join(Match, Feedback.match_id == Match.id)
                .join(RawItem, RawItem.id == Match.raw_item_id)
                .where(Feedback.created_at >= since, Feedback.verdict == "bad")
                .group_by(RawItem.source)
                .order_by(func.count().desc())
                .limit(5)
            )
        ).all()

    precision = good / (good + bad) if (good + bad) else None
    lines = [
        "Еженедельный отчёт:",
        f"отправлено уведомлений: {notified}",
        f"фидбек: +{good} / -{bad}",
        f"precision по фидбеку: {precision:.2f}" if precision is not None else "фидбека пока нет",
        "вызовы LLM: " + (", ".join(f"{o}={c}" for o, c in llm_calls) or "не было"),
        "квота сейчас: " + str(quota_state(get_sync_redis())),
    ]
    if bad_by_source:
        lines.append("топ источников по ложным срабатываниям:")
        lines += [f"  {src}: {cnt}" for src, cnt in bad_by_source]
    await _alert("\n".join(lines))


@celery_app.task(name="app.celery_app.weekly_report")
def weekly_report() -> None:
    asyncio.run(_weekly_report_async())


async def _retention_cleanup_async() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.retention_days)
    deleted_total = 0
    async with SessionLocal() as session:
        while True:
            ids = [
                r[0]
                for r in (
                    await session.execute(
                        select(RawItem.id)
                        .where(RawItem.reject_rule.is_not(None), RawItem.created_at < cutoff)
                        .limit(10_000)
                    )
                ).all()
            ]
            if not ids:
                break
            await session.execute(delete(RawItem).where(RawItem.id.in_(ids)))
            await session.commit()
            deleted_total += len(ids)
    return deleted_total


@celery_app.task(name="app.celery_app.retention_cleanup")
def retention_cleanup() -> None:
    deleted = asyncio.run(_retention_cleanup_async())
    if deleted:
        asyncio.run(_alert(f"Retention cleanup: удалено {deleted} старых отклонённых записей."))


@celery_app.task(name="app.celery_app.heartbeat_check")
def heartbeat_check() -> None:
    async def _check() -> None:
        redis = get_redis()
        # settings.monitored_collector_circuits (config.py) — дефолт только "main",
        # чтобы не слать ложную тревогу за collector-browse, пока он не задеплоен.
        # Расширяется на "main,browse" вручную вместе с деплоем контейнера.
        for circuit in settings.monitored_collector_circuits_list:
            val = await redis.get(KEY_HEARTBEAT_COLLECTOR.format(circuit=circuit))
            if val is None:
                await _alert(f"Collector [{circuit}] молчит: heartbeat отсутствует.")
                continue
            age = int(datetime.now(timezone.utc).timestamp()) - int(val)
            if age > 900:
                await _alert(f"Collector [{circuit}] молчит {age // 60} мин — проверь контейнер.")

    asyncio.run(_check())
