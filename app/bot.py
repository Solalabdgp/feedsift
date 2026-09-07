"""Telegram-бот доставки: owner-only декоратор, NOTIFY_QUEUE/DIGEST_QUEUE,
quiet hours, feedback-кнопки.

Содержимое карточки: RU-саммари от LLM + тег hiring_intent/pain_point
+ прямая ссылка на исходную запись.
"""
import asyncio
import functools
import html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import require, settings
from app.db import SessionLocal
from app.filters import run_hard_filters
from app.logging_setup import configure_logging
from app.llm import quota_state
from app.models import (
    Author,
    Feedback,
    Keyword,
    LlmUsageLog,
    Match,
    RawItem,
    RuleWeight,
    Source,
)
from app.normalize import apply_aliases, compute_struct_features, normalize_text
from app.redis_client import (
    CHANNEL_ALERTS,
    DIGEST_QUEUE,
    DUP_NOTIFY_QUEUE,
    KEY_PAUSED,
    KEY_RELOAD_SIGNAL,
    NOTIFY_QUEUE,
    get_redis,
    get_sync_redis,
)
from app.rules_loader import load_rules_cache
from app.scoring import (
    _matched_terms,
    detect_help_request,
    needs_llm_classification,
    score_item,
)

log = structlog.get_logger("bot")

router = Router()

RATE_LIMIT_PER_MIN = 20
VALID_DICT_TYPES = {
    "stack_core", "stack_periph", "hiring_intent", "pain_point",
    "money", "anti", "closed", "noise", "alias",
}
VALID_CATEGORIES = {
    "job_board", "webdev_business", "ai_automation", "startup_mvp",
    "leadgen_agency", "business_ops", "browse_niche", "other",
}
INTENT_LABEL = {
    "hiring_intent": "явный найм",
    "pain_point": "боль без запроса",
    "both": "найм + боль",
    "help_request": "вопрос по теме",
    "unknown": "сигнал неясен",
}
FEEDBACK_REASONS = [
    ("not_a_lead", "не лид"),
    ("service_offer", "автор сам предлагает услуги"),
    ("not_my_stack", "не мой стек"),
    ("closed", "уже закрыто"),
    ("no_budget", "бюджета нет / equity"),
    ("discussion", "просто обсуждение"),
    ("self_promo", "самопиар"),
    ("duplicate", "дубль"),
]


def owner_only(handler):
    @functools.wraps(handler)  # сохраняем сигнатуру для DI aiogram
    async def wrapper(event, *args, **kwargs):
        user = event.from_user
        if user is None or user.id != settings.telegram_owner_id:
            return
        return await handler(event, *args, **kwargs)

    return wrapper


def _esc(value: str | None) -> str:
    return html.escape(value or "")


def _card_keyboard(match_id: int, permalink: str | None) -> InlineKeyboardMarkup:
    rows = []
    if permalink:
        rows.append([InlineKeyboardButton(text="Открыть запись", url=permalink)])
    rows.append([
        InlineKeyboardButton(text="В избранное", callback_data=f"fav:{match_id}"),
        InlineKeyboardButton(text="Не то", callback_data=f"bad:{match_id}"),
    ])
    rows.append([
        InlineKeyboardButton(text="Почему прислал", callback_data=f"why:{match_id}"),
        InlineKeyboardButton(text="Оригинал текста", callback_data=f"orig:{match_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Лимит сообщения Telegram — 4096 символов. Держим запас под HTML-разметку.
TG_LIMIT = 3900


def _format_card(match: Match, raw: RawItem, dup_note: str) -> str:
    verdict = match.llm_verdict or {}
    tag = INTENT_LABEL.get(match.intent_tag, match.intent_tag)
    posted_local = raw.posted_at.astimezone(ZoneInfo(settings.tz)).strftime("%d.%m %H:%M")

    parts = [f"<b>{_esc(match.headline)}</b> · {match.score} б · {tag}"]
    parts.append(f"{_esc(raw.source)} · {_esc(raw.author_handle)} · {posted_local}{dup_note}")
    parts.append("")

    if match.llm_summary_ru:
        parts.append(_esc(match.llm_summary_ru))
    else:
        # LLM не отработал (квота/сбой) — показываем оригинал, лид не теряем
        parts.append("<i>Без RU-пересказа (LLM недоступен). Оригинал:</i>")
        parts.append(_esc(raw.text[:500]))

    need = verdict.get("need_ru")
    if need:
        parts.append(f"\nЧто нужно: {_esc(need)}")
    budget = verdict.get("budget_hint") or match.compensation
    if budget:
        parts.append(f"Бюджет: {_esc(str(budget))}")
    flags = verdict.get("red_flags_ru") or []
    if flags:
        parts.append("Флаги: " + _esc(", ".join(str(f) for f in flags)))
    if match.stack_matched:
        parts.append("Стек: " + _esc(" · ".join(match.stack_matched[:8])))
    if match.decided_by == "rules_only":
        parts.append(f"<i>Решено правилами (LLM: {match.llm_status})</i>")

    # Режем ПО СТРОКАМ, а не по символам. Каждая строка здесь — законченный кусок
    # с парными тегами, поэтому обрезка на границе строки не разорвёт <b> или <i>
    # и не сломает Telegram-парсер. Обрезка по символам такое ломала бы.
    while parts and len("\n".join(parts)) > TG_LIMIT:
        parts.pop()
    return "\n".join(parts)


async def _load_card_data(match_id: int):
    async with SessionLocal() as session:
        match = (await session.execute(select(Match).where(Match.id == match_id))).scalar_one_or_none()
        if match is None:
            return None
        raw = (await session.execute(select(RawItem).where(RawItem.id == match.raw_item_id))).scalar_one()
    dup_note = f" · ещё {match.duplicate_count} похожих" if match.duplicate_count else ""
    return match, raw, dup_note


async def notify_consumer(bot: Bot) -> None:
    redis = get_redis()
    sent: list[float] = []
    while True:
        popped = await redis.blpop(NOTIFY_QUEUE, timeout=5)
        if popped is None:
            continue
        _key, match_id = popped
        match_id = int(match_id)
        if await redis.get(KEY_PAUSED):
            await redis.rpush(NOTIFY_QUEUE, match_id)
            await asyncio.sleep(5)
            continue
        now = asyncio.get_event_loop().time()
        sent[:] = [t for t in sent if now - t < 60]
        if len(sent) >= RATE_LIMIT_PER_MIN:
            await asyncio.sleep(60 - (now - sent[0]))
        data = await _load_card_data(match_id)
        if data is None:
            continue
        match, raw, dup_note = data
        text = _format_card(match, raw, dup_note)
        kb = _card_keyboard(match.id, raw.permalink)
        try:
            msg = await bot.send_message(settings.telegram_owner_id, text, reply_markup=kb)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            msg = await bot.send_message(settings.telegram_owner_id, text, reply_markup=kb)
        except Exception:
            log.exception("notify_send_failed", match_id=match_id)
            continue
        sent.append(now)
        async with SessionLocal() as session:
            await session.execute(
                update(Match)
                .where(Match.id == match_id)
                .values(
                    status="notified",
                    notified_at=datetime.now(timezone.utc),
                    notification_msg_id=msg.message_id,
                )
            )
            await session.commit()


async def dup_notify_consumer(bot: Bot) -> None:
    redis = get_redis()
    while True:
        popped = await redis.blpop(DUP_NOTIFY_QUEUE, timeout=5)
        if popped is None:
            continue
        _key, match_id = popped
        data = await _load_card_data(int(match_id))
        if data is None:
            continue
        match, raw, dup_note = data
        if not match.notification_msg_id:
            continue
        try:
            await bot.edit_message_text(
                _format_card(match, raw, dup_note),
                chat_id=settings.telegram_owner_id,
                message_id=match.notification_msg_id,
                reply_markup=_card_keyboard(match.id, raw.permalink),
            )
        except Exception:
            pass


async def alerts_subscriber(bot: Bot) -> None:
    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL_ALERTS)
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            await bot.send_message(settings.telegram_owner_id, f"[alert] {message['data']}")
        except Exception:
            log.exception("alert_send_failed")


async def digest_job(bot: Bot) -> None:
    redis = get_redis()
    ids = []
    while True:
        val = await redis.lpop(DIGEST_QUEUE)
        if val is None:
            break
        ids.append(int(val))
    if not ids:
        return
    lines = [f"Утренний дайджест — {len(ids)} тихих совпадений за ночь:\n"]
    for mid in ids[:30]:
        data = await _load_card_data(mid)
        if data is None:
            continue
        match, raw, _dup = data
        summary = match.llm_summary_ru or match.headline
        lines.append(f"• {match.score} б · {raw.source} — {_esc(summary)[:200]}")
        if raw.permalink:
            lines.append(f"  {raw.permalink}")
        async with SessionLocal() as session:
            await session.execute(
                update(Match)
                .where(Match.id == mid)
                .values(status="notified", notified_at=datetime.now(timezone.utc))
            )
            await session.commit()
    await bot.send_message(settings.telegram_owner_id, "\n".join(lines)[:4000])


# ------------------------------------------------------------------- команды


@router.message(Command("start"))
@owner_only
async def cmd_start(message: Message) -> None:
    await message.answer("feedsift запущен. /help — список команд.")


@router.message(Command("help"))
@owner_only
async def cmd_help(message: Message) -> None:
    rules = await load_rules_cache(0)
    await message.answer(
        "/stats [день|неделя] — статистика\n"
        "/sources — список источников\n"
        "/mute &lt;source&gt; [часов] — заглушить источник\n"
        "/unmute &lt;source&gt; — снять заглушку\n"
        "/category &lt;source&gt; &lt;категория&gt; — сменить категорию\n"
        "/keywords &lt;dict_type&gt; — показать словарь\n"
        "/keywords add &lt;dict_type&gt; &lt;термин&gt; [вес]\n"
        "/keywords del &lt;dict_type&gt; &lt;термин&gt;\n"
        "/weights [set &lt;key&gt; &lt;N&gt;] — веса правил\n"
        f"/threshold [N] — порог уведомления (сейчас {rules.notify_threshold})\n"
        "/quota — расход квоты LLM\n"
        "/pause /resume — пауза уведомлений\n"
        "/favorites — избранные\n"
        "/digest — прислать дайджест сейчас\n"
        "/dryrun N — прогнать последние N записей через текущие правила\n"
        "/why &lt;match_id&gt; — разбор баллов"
    )


@router.message(Command("quota"))
@owner_only
async def cmd_quota(message: Message) -> None:
    state = quota_state(get_sync_redis())
    async with SessionLocal() as session:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        rows = (
            await session.execute(
                select(LlmUsageLog.outcome, func.count())
                .where(LlmUsageLog.created_at >= since)
                .group_by(LlmUsageLog.outcome)
            )
        ).all()
    await message.answer(
        f"LLM ({settings.groq_model}):\n"
        f"за день: {state['day_used']}/{state['day_limit']}\n"
        f"за минуту: {state['min_used']}/{state['min_limit']}\n"
        "вызовы за 24ч: " + (", ".join(f"{o}={c}" for o, c in rows) or "не было")
    )


@router.message(Command("stats"))
@owner_only
async def cmd_stats(message: Message, command: CommandObject) -> None:
    period = (command.args or "день").strip()
    days = 7 if period.startswith("нед") else 1
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with SessionLocal() as session:
        seen = (
            await session.execute(select(func.count()).select_from(RawItem).where(RawItem.created_at >= since))
        ).scalar_one()
        matched = (
            await session.execute(select(func.count()).select_from(Match).where(Match.created_at >= since))
        ).scalar_one()
        notified = (
            await session.execute(
                select(func.count()).select_from(Match).where(Match.created_at >= since, Match.status == "notified")
            )
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
    precision = f"{good / (good + bad):.2f}" if (good + bad) else "нет фидбека"
    await message.answer(
        f"За {period}:\nзаписей прочитано: {seen}\nпрошло фильтры: {matched}\n"
        f"отправлено: {notified}\nфидбек: +{good} / -{bad}\nprecision: {precision}"
    )


@router.message(Command("sources"))
@owner_only
async def cmd_sources(message: Message) -> None:
    async with SessionLocal() as session:
        rows = (await session.execute(select(Source).order_by(Source.category, Source.name))).scalars().all()
    if not rows:
        await message.answer("Источников нет — миграции не прогнаны?")
        return
    lines = []
    for r in rows:
        flags = []
        if not r.enabled:
            flags.append("выкл")
        if r.muted_until and r.muted_until > datetime.now(timezone.utc):
            flags.append("muted")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"{r.name} · {r.category} · p{r.priority}{suffix}")
    await message.answer("\n".join(lines)[:4000])


@router.message(Command("mute"))
@owner_only
async def cmd_mute(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if not parts:
        await message.answer("Использование: /mute &lt;source&gt; [часов]")
        return
    name = parts[0]
    hours = int(parts[1]) if len(parts) > 1 else 24 * 365
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    async with SessionLocal() as session:
        await session.execute(update(Source).where(Source.name == name).values(muted_until=until))
        await session.commit()
    await message.answer(f"{name} заглушён на {hours}ч.")


@router.message(Command("unmute"))
@owner_only
async def cmd_unmute(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    async with SessionLocal() as session:
        await session.execute(update(Source).where(Source.name == name).values(muted_until=None))
        await session.commit()
    await message.answer(f"{name} размучен.")


@router.message(Command("category"))
@owner_only
async def cmd_category(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or parts[1] not in VALID_CATEGORIES:
        await message.answer("Использование: /category &lt;source&gt; &lt;" + "|".join(sorted(VALID_CATEGORIES)) + "&gt;")
        return
    name = parts[0]
    async with SessionLocal() as session:
        await session.execute(update(Source).where(Source.name == name).values(category=parts[1]))
        await session.commit()
    await message.answer(f"{name} -> {parts[1]}")


async def _bump_reload_signal() -> None:
    redis = get_redis()
    await redis.incr(KEY_RELOAD_SIGNAL)


@router.message(Command("keywords"))
@owner_only
async def cmd_keywords(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split(maxsplit=3)
    if not args:
        await message.answer("Типы словарей: " + ", ".join(sorted(VALID_DICT_TYPES)))
        return
    if args[0] == "add" and len(args) >= 3:
        dict_type, term = args[1], args[2].lower()
        weight = int(args[3]) if len(args) > 3 else 1
        if dict_type not in VALID_DICT_TYPES:
            await message.answer("Неизвестный dict_type.")
            return
        async with SessionLocal() as session:
            stmt = pg_insert(Keyword).values(dict_type=dict_type, term=term, weight=weight, enabled=True)
            stmt = stmt.on_conflict_do_update(
                index_elements=["dict_type", "term"], set_={"weight": weight, "enabled": True}
            )
            await session.execute(stmt)
            await session.commit()
        await _bump_reload_signal()
        await message.answer(f"Добавлено: {dict_type}/{term} (вес {weight})")
        return
    if args[0] == "del" and len(args) >= 3:
        dict_type, term = args[1], args[2].lower()
        async with SessionLocal() as session:
            await session.execute(
                update(Keyword).where(Keyword.dict_type == dict_type, Keyword.term == term).values(enabled=False)
            )
            await session.commit()
        await _bump_reload_signal()
        await message.answer(f"Отключено: {dict_type}/{term}")
        return
    dict_type = args[0]
    if dict_type not in VALID_DICT_TYPES:
        await message.answer("Типы: " + ", ".join(sorted(VALID_DICT_TYPES)))
        return
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Keyword).where(Keyword.dict_type == dict_type, Keyword.enabled.is_(True)).order_by(Keyword.term)
            )
        ).scalars().all()
    await message.answer(f"{dict_type} [{len(rows)}]:\n" + ", ".join(f"{r.term}({r.weight})" for r in rows)[:3800])


@router.message(Command("weights"))
@owner_only
async def cmd_weights(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if args and args[0] == "set" and len(args) == 3:
        key, value = args[1], int(args[2])
        async with SessionLocal() as session:
            stmt = pg_insert(RuleWeight).values(key=key, weight=value)
            stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"weight": value})
            await session.execute(stmt)
            await session.commit()
        await _bump_reload_signal()
        await message.answer(f"{key} = {value}")
        return
    async with SessionLocal() as session:
        rows = (await session.execute(select(RuleWeight).order_by(RuleWeight.key))).scalars().all()
    await message.answer("\n".join(f"{r.key} = {r.weight}" for r in rows) or "пусто")


@router.message(Command("threshold"))
@owner_only
async def cmd_threshold(message: Message, command: CommandObject) -> None:
    if not command.args:
        rules = await load_rules_cache(0)
        await message.answer(f"Текущий порог: {rules.notify_threshold}")
        return
    n = int(command.args.strip())
    async with SessionLocal() as session:
        stmt = pg_insert(RuleWeight).values(key="__notify_threshold__", weight=n)
        stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"weight": n})
        await session.execute(stmt)
        await session.commit()
    await _bump_reload_signal()
    await message.answer(f"Порог теперь {n}. Прогони /dryrun перед тем как на него полагаться.")


@router.message(Command("pause"))
@owner_only
async def cmd_pause(message: Message) -> None:
    await get_redis().set(KEY_PAUSED, "1")
    await message.answer("Уведомления на паузе. /resume — вернуть.")


@router.message(Command("resume"))
@owner_only
async def cmd_resume(message: Message) -> None:
    await get_redis().delete(KEY_PAUSED)
    await message.answer("Уведомления возобновлены.")


@router.message(Command("favorites"))
@owner_only
async def cmd_favorites(message: Message) -> None:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Match).where(Match.is_favorite.is_(True)).order_by(Match.created_at.desc()).limit(20)
            )
        ).scalars().all()
    if not rows:
        await message.answer("Избранное пусто.")
        return
    await message.answer("\n".join(f"{m.id} · {m.score}б · {_esc(m.headline)}" for m in rows))


@router.message(Command("digest"))
@owner_only
async def cmd_digest(message: Message, bot: Bot) -> None:
    await digest_job(bot)
    await message.answer("Дайджест отправлен (если было что слать).")


@router.message(Command("dryrun"))
@owner_only
async def cmd_dryrun(message: Message, command: CommandObject) -> None:
    n = min(int((command.args or "100").strip()), 500)
    rules = await load_rules_cache(0)
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(RawItem).order_by(RawItem.created_at.desc()).limit(n))
        ).scalars().all()
        srcs = {s.name.lower(): s for s in (await session.execute(select(Source))).scalars().all()}

    would_notify = 0
    would_call_llm = 0
    reject_counts: dict[str, int] = {}
    for raw in rows:
        text_norm = apply_aliases(normalize_text(raw.text), rules.alias_re, rules.aliases)
        struct = compute_struct_features(raw.text, raw.has_body)
        hiring = _matched_terms(rules.hiring_re, text_norm)
        pain = _matched_terms(rules.pain_re, text_norm)
        stack_probe = _matched_terms(rules.stack_re, text_norm)
        noise_probe = _matched_terms(rules.noise_re, text_norm)
        help_request = detect_help_request(text_norm, struct, stack_probe, rules, noise_probe)
        reject = run_hard_filters(
            text_norm, hiring, pain, raw.tag, raw.has_body,
            rules.closed_re, rules.anti_re, rules.hiring_re, help_request,
        )
        if reject:
            reject_counts[reject] = reject_counts.get(reject, 0) + 1
            continue
        src = srcs.get(raw.source.lower())
        result = score_item(
            text_norm, struct, raw.tag,
            src.category if src else "other", src.priority if src else 0, 0, rules,
        )
        if needs_llm_classification(result.score, result.intent_tag):
            would_call_llm += 1
        if result.score >= rules.notify_threshold:
            would_notify += 1
    lines = [
        f"Dry-run на последних {len(rows)} записях:",
        f"было бы отправлено: {would_notify}",
        f"ушло бы в LLM: {would_call_llm}",
    ]
    lines += [f"  reject {r}: {c}" for r, c in sorted(reject_counts.items(), key=lambda x: -x[1])]
    await message.answer("\n".join(lines))


async def _send_why(chat_id: int, match_id: int, bot: Bot) -> None:
    async with SessionLocal() as session:
        match = (await session.execute(select(Match).where(Match.id == match_id))).scalar_one_or_none()
    if match is None:
        await bot.send_message(chat_id, "Не найдено.")
        return
    lines = [f"Разбор баллов match #{match.id} (правила {match.rules_version}):"]
    for rule, points in (match.signals or {}).items():
        lines.append(f"  {rule}: {'+' if points >= 0 else ''}{points}")
    lines.append(f"Итого: {match.score} · тег: {INTENT_LABEL.get(match.intent_tag, match.intent_tag)}")
    lines.append(f"Решение: {match.decided_by} (LLM: {match.llm_status})")
    if match.llm_prob is not None:
        lines.append(f"Уверенность LLM: {match.llm_prob:.2f} ({match.model_version})")
    await bot.send_message(chat_id, "\n".join(lines))


@router.message(Command("why"))
@owner_only
async def cmd_why(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /why &lt;match_id&gt;")
        return
    await _send_why(message.chat.id, int(command.args.strip()), message.bot)


@router.callback_query(F.data.startswith("why:"))
@owner_only
async def cb_why(call: CallbackQuery) -> None:
    await _send_why(call.message.chat.id, int(call.data.split(":")[1]), call.bot)
    await call.answer()


@router.callback_query(F.data.startswith("orig:"))
@owner_only
async def cb_orig(call: CallbackQuery) -> None:
    data = await _load_card_data(int(call.data.split(":")[1]))
    if data is None:
        await call.answer("Не найдено")
        return
    _match, raw, _dup = data
    await call.message.answer(f"<b>{_esc(raw.title)}</b>\n\n{_esc(raw.text[:3500])}")
    await call.answer()


async def _bump_author(session, match_id: int, field: str) -> None:
    match = (await session.execute(select(Match).where(Match.id == match_id))).scalar_one_or_none()
    if match is None:
        return
    raw = (await session.execute(select(RawItem).where(RawItem.id == match.raw_item_id))).scalar_one_or_none()
    if raw is None or not raw.author_handle:
        return
    author = (
        await session.execute(select(Author).where(Author.handle == raw.author_handle))
    ).scalar_one_or_none()
    if author is None:
        session.add(Author(handle=raw.author_handle, **{field: 1}))
    else:
        setattr(author, field, getattr(author, field) + 1)


@router.callback_query(F.data.startswith("fav:"))
@owner_only
async def cb_fav(call: CallbackQuery) -> None:
    """Избранное = сильный позитивный фидбек для репутации автора."""
    match_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        await session.execute(update(Match).where(Match.id == match_id).values(is_favorite=True))
        session.add(Feedback(match_id=match_id, verdict="good", reason=None))
        await _bump_author(session, match_id, "good_count")
        await session.commit()
    await call.answer("Добавлено в избранное")


@router.callback_query(F.data.startswith("bad:"))
@owner_only
async def cb_bad(call: CallbackQuery) -> None:
    match_id = int(call.data.split(":")[1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"reason:{match_id}:{code}")]
            for code, label in FEEDBACK_REASONS
        ]
    )
    await call.message.answer("Почему не то?", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("reason:"))
@owner_only
async def cb_reason(call: CallbackQuery) -> None:
    _, match_id_s, reason = call.data.split(":")
    match_id = int(match_id_s)
    async with SessionLocal() as session:
        session.add(Feedback(match_id=match_id, verdict="bad", reason=reason))
        await _bump_author(session, match_id, "bad_count")
        await session.commit()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Учтено")


async def main() -> None:
    configure_logging()
    require("database_url", "redis_url", "telegram_bot_token", "telegram_owner_id")

    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=settings.tz)
    end_h, end_m = settings.quiet_hours_end.split(":")
    scheduler.add_job(digest_job, "cron", hour=int(end_h), minute=int(end_m), args=[bot])
    scheduler.start()

    log.info("bot_started")
    await asyncio.gather(
        dp.start_polling(bot),
        notify_consumer(bot),
        dup_notify_consumer(bot),
        alerts_subscriber(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
