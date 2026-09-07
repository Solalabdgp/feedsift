"""Offline-прогон пайплайна по живому фиду, без Docker/Postgres/Redis.

Один HTTP-запрос к источнику, дальше локально: парсинг Atom -> нормализация ->
жёсткие фильтры -> keyword-скоринг. Показывает, сколько записей дошло бы до
уведомления и сколько ушло бы в LLM — то есть попадает ли трафик в бюджет
вызовов LLM в сутки.

LLM НЕ вызывается.

    python -m scripts.offline_dryrun                  # свежий запрос к источнику
    python -m scripts.offline_dryrun --save feed.xml  # запрос + сохранить выдачу
    python -m scripts.offline_dryrun --from feed.xml  # прогнать сохранённую выдачу
    python -m scripts.offline_dryrun --from feed.xml --no-soft

--from/--save нужны, чтобы сравнивать варианты правил на ОДНИХ И ТЕХ ЖЕ записях:
у провайдера жёсткий лимит запросов, а два прогона по разным окнам ленты
сравнивать бессмысленно. --no-soft отключает мягкий сигнал "вопрос по теме"
(app/scoring.detect_help_request) — это A/B-рычаг для замера его вклада в recall.
"""
import sys
from pathlib import Path

import httpx

from app.collector import build_url, parse_feed
from app.config import settings
from app.filters import run_hard_filters
from app.normalize import apply_aliases, compute_struct_features, normalize_text
from app.rules_seed import build_seed_rules
from app.scoring import (
    _matched_terms,
    detect_help_request,
    needs_llm_classification,
    score_item,
)
from app.sources import SOURCES

CATEGORY_BY_SOURCE = {name.lower(): cat for name, cat, _p in SOURCES}
PRIORITY_BY_SOURCE = {name.lower(): prio for name, _c, prio in SOURCES}


def _arg(name: str) -> str | None:
    if name in sys.argv:
        idx = sys.argv.index(name)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


def main() -> int:
    from_file = _arg("--from")
    save_to = _arg("--save")
    soft_enabled = "--no-soft" not in sys.argv

    sources = [name for name, _c, _p in SOURCES]

    if from_file:
        xml = Path(from_file).read_text(encoding="utf-8")
        print(f"из файла {from_file}, {len(xml)} байт")
    else:
        if not settings.source_feed_user or not settings.source_feed_token:
            print("SOURCE_FEED_USER / SOURCE_FEED_TOKEN не заданы в .env")
            return 1
        if not settings.source_feed_url_template:
            print("SOURCE_FEED_URL_TEMPLATE не задан в .env")
            return 1
        resp = httpx.get(
            build_url(sources),
            params={
                "user": settings.source_feed_user,
                "feed": settings.source_feed_token,
                "limit": str(settings.feed_limit),
            },
            headers={"User-Agent": settings.source_user_agent},
            timeout=30.0,
            follow_redirects=True,
        )
        print(f"HTTP {resp.status_code}, {len(resp.text)} байт")
        if resp.status_code != 200:
            print(resp.text[:500])
            return 1
        xml = resp.text
        if save_to:
            Path(save_to).write_text(xml, encoding="utf-8")
            print(f"сохранено в {save_to}")

    print("мягкий сигнал 'вопрос по теме': " + ("включён" if soft_enabled else "ВЫКЛЮЧЕН"))
    items = parse_feed(xml)
    with_body = sum(1 for i in items if i["has_body"])
    print(
        f"записей: {len(items)}, с телом: {with_body}, только ссылка: {len(items) - with_body}"
    )
    print(f"источников в выдаче: {len({i['source'] for i in items})} из {len(sources)}")

    rules = build_seed_rules()
    rejects: dict[str, int] = {}
    passed = []

    for item in items:
        raw_text = f"{item['title']}\n\n{item['body']}".strip()
        text_norm = apply_aliases(normalize_text(raw_text), rules.alias_re, rules.aliases)
        struct = compute_struct_features(raw_text, item["has_body"])
        hiring = _matched_terms(rules.hiring_re, text_norm)
        pain = _matched_terms(rules.pain_re, text_norm)
        stack_probe = _matched_terms(rules.stack_re, text_norm)
        noise_probe = _matched_terms(rules.noise_re, text_norm)
        help_request = soft_enabled and detect_help_request(
            text_norm, struct, stack_probe, rules, noise_probe
        )
        reject = run_hard_filters(
            text_norm, hiring, pain, item["tag"], item["has_body"],
            rules.closed_re, rules.anti_re, rules.hiring_re, help_request,
        )
        if reject:
            rejects[reject] = rejects.get(reject, 0) + 1
            continue
        source_key = (item["source"] or "").lower()
        result = score_item(
            text_norm, struct, item["tag"],
            CATEGORY_BY_SOURCE.get(source_key, "other"),
            PRIORITY_BY_SOURCE.get(source_key, 0), 0, rules,
            soft_help=soft_enabled,
        )
        passed.append((result, item))

    notify = [p for p in passed if p[0].score >= rules.notify_threshold]
    classify = [p for p in passed if needs_llm_classification(p[0].score, p[0].intent_tag)]
    # роль B: перевод для тех, кого правила и так шлют и кто не попал в классификацию
    translate = [p for p in notify if p not in classify]

    by_tag: dict[str, int] = {}
    for result, _item in passed:
        by_tag[result.intent_tag] = by_tag.get(result.intent_tag, 0) + 1

    print("\n-- отсев жёсткими фильтрами --")
    for rule, cnt in sorted(rejects.items(), key=lambda x: -x[1]):
        print(f"  {rule}: {cnt}")
    print(f"\nпрошло фильтры: {len(passed)}")
    print("  по тегу намерения: " + (", ".join(f"{t}={c}" for t, c in sorted(by_tag.items())) or "—"))
    print(f"дошло бы до уведомления (порог {rules.notify_threshold}): {len(notify)}")
    print(
        f"вызовов LLM: {len(classify) + len(translate)} "
        f"(классификация {len(classify)} + перевод {len(translate)})"
    )

    print("\n-- топ кандидатов --")
    for result, item in sorted(passed, key=lambda p: -p[0].score)[:10]:
        print(f"\n[{result.score:>3}] {result.intent_tag:<13} {item['source']}")
        print(f"      {item['title'][:100]}")
        print(f"      сигналы: {result.signals}")
        if result.hiring_matched:
            print(f"      найм: {result.hiring_matched[:5]}")
        if result.pain_matched:
            print(f"      боль: {result.pain_matched[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
