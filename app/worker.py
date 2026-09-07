"""Worker: потребляет queue:raw из Redis Stream и гоняет пайплайн
нормализация -> дедуп -> жёсткие фильтры -> keyword-скоринг -> matches.

LLM вызывается НЕ на весь поток, а только на две ситуации:
  - серая зона скоринга (правила сами не уверены);
  - запись прошла только по pain_point без явного hiring_intent
    (regex не отличит баг-репорт от скрытого запроса на исполнителя);
плюс роль B — перевод для тех, что уже решены к отправке.
"""
import asyncio
import json
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import require, settings
from app.db import SessionLocal
from app.logging_setup import configure_logging
from app.dedup import (
    exact_duplicate_of,
    hamming_distance,
    register_exact,
    register_simhash,
    simhash64,
    text_hash,
    to_signed64,
    simhash_candidates,
)
from app.filters import run_hard_filters
from app.models import Author, Match, RawItem, Source
from app.normalize import apply_aliases, compute_struct_features, extract_headline, normalize_text
from app.notify_router import route
from app.redis_client import (
    DUP_NOTIFY_QUEUE,
    KEY_RELOAD_SIGNAL,
    STREAM_GROUP,
    STREAM_RAW,
    get_redis,
)
from app.rules_loader import load_rules_cache
from app.scoring import (
    _matched_terms,
    detect_help_request,
    needs_llm_classification,
    score_item,
)
from app.source_cache import SourceCache

log = structlog.get_logger("worker")

CONSUMER_NAME = "worker-1"


async def _ensure_source_stub(session, name: str) -> None:
    stmt = pg_insert(Source).values(name=name, category="other").on_conflict_do_nothing(
        index_elements=[Source.name]
    )
    await session.execute(stmt)


async def _author_bonus(session, handle: str | None, rules) -> int:
    if not handle:
        return 0
    row = (
        await session.execute(select(Author).where(Author.handle == handle))
    ).scalar_one_or_none()
    if row is None:
        return 0
    if row.good_count >= 3:
        return rules.rule_weights.get("author_reputation_bonus", 2)
    if row.bad_count >= 5 and row.good_count == 0:
        return -5
    return 0


def _enqueue_llm(
    match_id: int, mode: str, rule_hint: str, rules_fallback_notify: bool, circuit: str = "main"
) -> None:
    from app.celery_app import celery_app

    # circuit="main" -> очередь "llm" (как раньше, без изменений). Любой другой контур
    # (сейчас только "browse") -> отдельная очередь "llm_browse", которую слушает
    # отдельный контейнер llm-worker-browse со своим Groq-ключом (docker-compose.yml).
    # Изоляция rate-limit между контурами достигается тем, что это два разных
    # worker-процесса Celery, каждый со своим in-memory токен-бакетом на одно и то же
    # имя задачи app.celery_app.analyze_match — не нужно менять task_routes/annotations.
    queue = "llm" if circuit == "main" else "llm_browse"
    celery_app.send_task(
        "app.celery_app.analyze_match",
        args=[match_id, mode, rule_hint, rules_fallback_notify, circuit],
        queue=queue,
    )


def _decide_llm_mode(score: int, intent_tag: str, rules, circuit: str = "main") -> tuple[str | None, str]:
    """Возвращает (mode, rule_hint). mode=None — LLM не нужен.

    circuit!="main" (browse-контур) обходит keyword-gate
    (needs_llm_classification/notify_threshold) целиком: словари hiring_re/pain_re
    и ядерный стек в detect_help_request откалиброваны под основной домен и дают
    ненадёжное частичное покрытие вне его. Единственный гейт до LLM для browse —
    HF-1/HF-3/HF-5/HF-6 в run_hard_filters (доменно-нейтральные, HF-2/HF-4 пропущены
    через skip_soft_gate в process_item) плюс дедуп; дальше решает более мягкий
    LLM-промпт (SYSTEM_BRIEF_BROWSE/SCHEMA_HINT_BROWSE в app/llm.py), а не балл правил.
    """
    if circuit != "main":
        return (
            "classify_and_translate",
            f"circuit={circuit}: широкий критерий — любая боль/проблема, отдалённо "
            "связанная с разработкой на заказ, автоматизацией, CRM и учётом для "
            "бизнеса; балл правил не гейтит вызов",
        )

    if needs_llm_classification(score, intent_tag):
        hint_parts = [f"балл правил {score} при пороге уведомления {rules.notify_threshold}"]
        if intent_tag == "pain_point":
            hint_parts.append("сработали только фразы про боль, явного найма нет")
        elif intent_tag == "help_request":
            hint_parts.append(
                "явных фраз найма и боли нет; пост опознан только как вопрос по теме "
                "(человек просит помощи там, где мы могли бы предложить решение)"
            )
        return "classify_and_translate", "; ".join(hint_parts)
    # После снятия обхода в needs_llm_classification эта ветка в живом пайплайне
    # недостижима: всё, что прошло HF-2, имеет непустой intent_tag и уходит
    # на классификацию. Режим "translate" оставлен для ручных прогонов — им, например,
    # догоняют переводы карточек, отправленных при исчерпанной квоте.
    if score >= rules.notify_threshold:
        return "translate", ""
    return None, ""


async def process_item(payload: dict, rules, source_cache: SourceCache, redis) -> None:
    source_name = payload.get("source")
    if not source_name:
        return

    async with SessionLocal() as session:
        src = await source_cache.get(source_name)
        if src is None:
            await _ensure_source_stub(session, source_name)
            await session.commit()
            src = await source_cache.get(source_name)
        if src is not None:
            if not src.enabled:
                return
            if src.muted_until is not None and src.muted_until > datetime.now(src.muted_until.tzinfo):
                return

        # circuit — источник истины Source.circuit (main|browse). Неизвестный источник
        # (src is None, только что застабленный _ensure_source_stub) по умолчанию
        # считается "main" — все browse-источники заранее заведены миграцией, так что
        # эта ветка для browse-контура в норме не срабатывает.
        circuit = src.circuit if src is not None else "main"

        title = payload["title"]
        body = payload.get("body") or ""
        has_body = bool(payload.get("has_body"))
        tag = payload.get("tag")
        raw_text = f"{title}\n\n{body}".strip()

        struct = compute_struct_features(raw_text, has_body)
        text_norm = apply_aliases(normalize_text(raw_text), rules.alias_re, rules.aliases)

        h = text_hash(text_norm)
        s64 = simhash64(text_norm)

        dup_of = await exact_duplicate_of(redis, h)
        if dup_of is None:
            candidates = await simhash_candidates(redis, s64)
            if candidates:
                rows = (
                    await session.execute(
                        select(RawItem.id, RawItem.simhash).where(RawItem.id.in_(candidates))
                    )
                ).all()
                for cand_id, cand_simhash in rows:
                    if hamming_distance(s64, cand_simhash) <= 3:
                        dup_of = cand_id
                        break

        hiring_matched = _matched_terms(rules.hiring_re, text_norm)
        pain_matched = _matched_terms(rules.pain_re, text_norm)
        # help_request нужен уже на этапе HF-2, поэтому стек считаем до фильтров
        stack_probe = _matched_terms(rules.stack_re, text_norm)
        noise_probe = _matched_terms(rules.noise_re, text_norm)
        help_request = detect_help_request(text_norm, struct, stack_probe, rules, noise_probe)

        reject_rule = "duplicate" if dup_of is not None else None
        if reject_rule is None:
            reject_rule = run_hard_filters(
                text_norm=text_norm,
                hiring_matched=hiring_matched,
                pain_matched=pain_matched,
                tag=tag,
                has_body=has_body,
                closed_re=rules.closed_re,
                anti_re=rules.anti_re,
                hiring_re=rules.hiring_re,
                help_request=help_request,
                # HF-2/HF-4 отключены только для browse: словарный сигнал ненадёжен
                # вне freelance/hiring-домена, см. run_hard_filters/hf2_no_signal.
                skip_soft_gate=(circuit != "main"),
            )

        posted_at = datetime.fromisoformat(payload["posted_at"])

        raw = RawItem(
            source=src.name if src else source_name,
            circuit=circuit,
            item_id=payload["item_id"],
            author_handle=payload.get("author_handle"),
            permalink=payload.get("permalink"),
            tag=tag,
            title=title,
            posted_at=posted_at,
            text=raw_text,
            text_norm=text_norm,
            text_hash=h,
            simhash=to_signed64(s64),
            has_body=has_body,
            struct_features=struct,
            reject_rule=reject_rule,
        )
        session.add(raw)
        await session.flush()

        await register_exact(redis, h, raw.id)
        await register_simhash(redis, s64, raw.id)

        if dup_of is not None:
            orig = (
                await session.execute(select(Match).where(Match.raw_item_id == dup_of))
            ).scalar_one_or_none()
            if orig is not None:
                orig.duplicate_count += 1
                await redis.rpush(DUP_NOTIFY_QUEUE, orig.id)
            await session.commit()
            return

        if reject_rule is not None:
            await session.commit()
            return

        bonus = await _author_bonus(session, payload.get("author_handle"), rules)
        result = score_item(
            text_norm=text_norm,
            struct_features=struct,
            tag=tag,
            category=src.category if src else "other",
            source_priority=src.priority if src else 0,
            author_reputation_bonus=bonus,
            rules=rules,
        )

        rules_would_notify = result.score >= rules.notify_threshold
        mode, rule_hint = _decide_llm_mode(result.score, result.intent_tag, rules, circuit=circuit)
        if not settings.llm_enabled:
            mode = None

        match = Match(
            raw_item_id=raw.id,
            circuit=circuit,
            score=result.score,
            signals=result.signals,
            stack_matched=result.stack_matched,
            intent_tag=result.intent_tag,
            compensation=result.compensation,
            contact=result.contact,
            headline=extract_headline(title),
            decided_by="rules",
            llm_status="pending" if mode else "not_needed",
            rules_version=rules.rules_version_tag,
            status="awaiting_llm" if mode else "new",
        )
        session.add(match)
        await session.flush()

        if mode is None:
            await route(session, redis, match.id, result.score, rules_would_notify, circuit=circuit)
            await session.commit()
            return

        match_id = match.id
        await session.commit()

    # send_task блокирующий — выносим из event loop, чтобы не тормозить консьюмер
    await asyncio.to_thread(_enqueue_llm, match_id, mode, rule_hint, rules_would_notify, circuit)
    log.info("llm_enqueued", match_id=match_id, mode=mode, circuit=circuit, score=result.score)


async def reload_watcher(state: dict) -> None:
    redis = get_redis()
    while True:
        try:
            val = await redis.get(KEY_RELOAD_SIGNAL)
            version = int(val) if val else 0
            if version != state.get("version", -1):
                state["rules"] = await load_rules_cache(version)
                state["version"] = version
                log.info("rules_reloaded", version=version)
        except Exception:
            log.exception("reload_watcher_failed")
        await asyncio.sleep(15)


async def main() -> None:
    configure_logging()
    require("database_url", "redis_url")

    redis = get_redis()
    try:
        await redis.xgroup_create(STREAM_RAW, STREAM_GROUP, id="0", mkstream=True)
    except Exception:
        pass  # группа уже есть

    state: dict = {"version": -1}
    state["rules"] = await load_rules_cache(0)
    state["version"] = 0
    source_cache = SourceCache()

    asyncio.create_task(reload_watcher(state))
    log.info("worker_started")

    while True:
        try:
            resp = await redis.xreadgroup(
                STREAM_GROUP, CONSUMER_NAME, {STREAM_RAW: ">"}, count=20, block=5000
            )
        except Exception:
            log.exception("xreadgroup_failed")
            await asyncio.sleep(2)
            continue
        if not resp:
            continue
        for _stream, messages in resp:
            for msg_id, fields in messages:
                try:
                    payload = json.loads(fields["data"])
                    await process_item(payload, state["rules"], source_cache, redis)
                except Exception:
                    log.exception("process_item_failed", msg_id=msg_id)
                finally:
                    await redis.xack(STREAM_RAW, STREAM_GROUP, msg_id)


if __name__ == "__main__":
    asyncio.run(main())
