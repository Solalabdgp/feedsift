"""Слоистый keyword-скоринг: слои с потолками + порог + серая зона.

Слой B "сигнал намерения" разбит на hiring_intent и pain_point — от этого зависит,
нужен ли вызов LLM.
"""
import re
from dataclasses import dataclass, field

from app.config import settings

CONTACT_RE = re.compile(
    r"\bdm me\b|\bpm me\b|\bmessage me\b|\bdms? open\b|\breach out\b|"
    r"[\w.+-]+@[\w-]+\.\w+|\bdiscord\.gg/\S+|\bt\.me/\S+"
)
MONEY_AMOUNT_RE = re.compile(
    r"\$\s?\d{2,}|\d{2,}\s?(usd|usdt|eur|gbp)\b|\d{2,}\s?(\$|usd)?\s?(/|per\s)\s?(hr|hour)\b|"
    r"\bbudget (of|is|around|up to)\b|\b\d{1,3}k\b",
    re.IGNORECASE,
)
# Мягкий сигнал "человек просит помощи / задаёт вопрос по делу".
#
# Зачем: фразовые словари ловят только точные формулировки ("need someone to build"),
# а живые люди пишут "how do I keep my sheet in sync with my shop?". Подавляющая часть
# отсева на HF-2 приходилась не на мусор, а на записи, сформулированные своими словами.
# Этот регекс + попадание хотя бы в один термин стека открывают такой записи проход
# в серую зону, где решает LLM.
#
# Сам по себе сигнал НЕ шлёт уведомление: вес +2 при пороге 10.
HELP_REQUEST_RE = re.compile(
    r"\bhow (do|can|would|should|did) (i|we|you)\b|\bhow to\b|"
    r"\bany(one|body) know how\b|\bany(one|body) else\b|"
    r"\bis there a (way|tool|service|plugin|script|bot|app|option)\b|"
    r"\bwhats the best way\b|\bbest way to\b|\bwhat would you use\b|"
    r"\bany (tips|advice|recommendations|suggestions|ideas)\b|"
    r"\brecommendations for\b|\bcan (someone|anyone) help\b|"
    r"\bneed help\b|\bhelp me\b|\blooking for advice\b|\blooking for a way\b|"
    r"\bstruggling with\b|\bhaving trouble\b|\btrouble with\b|\bstuck (on|with)\b|"
    r"\bdoes anyone (use|know|have)\b|\bwhat (should|would) i (do|use)\b|"
    r"\bhow would you\b|\bnot working\b|\bwont work\b|\bkeeps failing\b|"
    r"\bkeeps breaking\b|\bany idea why\b|\bwhat am i doing wrong\b|"
    # Расширение по разбору отказов HF-2 на живом трафике. Класс промахов: человек
    # описал свой магазин/процесс и свою нужду, но ни одна из старых формулировок
    # не подошла, и запись умерла, не дойдя до классификатора.
    r"\blooking for (a|an|any|some)\b|\bwhat are my options\b|\bany options\b|"
    r"\bwhat options\b|\bi want to\b|\bi need to\b|\bi am trying to\b|"
    r"\bim trying to\b|\btrying to (find|figure|set up|automate|build)\b|"
    r"\bhas anyone\b|\banyone here\b|\bis it possible\b|\bany way to\b|"
    r"\bwhats the best\b|\bwhich (one|tool|service|option)\b|"
    r"\bhelp with\b|\bsuggestions for\b|\bpointers\b|\bwhere do i start\b|"
    r"\bcant figure out\b|\bcant get it to\b|\bno idea how\b"
)

SECTION_HEADER_RE = re.compile(
    r"(requirements|responsibilities|deliverables|scope|budget|timeline|"
    r"tech stack|what i need|what we need|about the project|about the role|"
    r"nice to have|must have)\s*:",
)

# [Hiring]/[Task] — кто-то ищет исполнителя. [For Hire]/[Offer] — кто-то СЕБЯ продаёт.
# Метки противоположного смысла держим строго врозь: "offer" в наборе найма
# затаскивал в топ рекламу чужих услуг.
TAG_HIRING = {"hiring", "hiring us", "task", "paid", "job"}
TAG_SERVICE_OFFER = {"for hire", "forhire", "hire me", "offer", "offering", "selling", "service"}


def _build_alternation(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    escaped = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b")


@dataclass
class RulesCache:
    version: int = 0
    stack_weights: dict[str, int] = field(default_factory=dict)
    hiring_weights: dict[str, int] = field(default_factory=dict)
    pain_weights: dict[str, int] = field(default_factory=dict)
    money_weights: dict[str, int] = field(default_factory=dict)
    anti_terms: list[str] = field(default_factory=list)
    closed_terms: list[str] = field(default_factory=list)
    noise_weights: dict[str, int] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    rule_weights: dict[str, int] = field(default_factory=dict)
    stack_cap: int = 9
    intent_cap: int = 8
    noise_cap: int = -8
    notify_threshold: int = 10
    rules_version_tag: str = "v1"

    stack_re: re.Pattern | None = field(default=None, repr=False)
    hiring_re: re.Pattern | None = field(default=None, repr=False)
    pain_re: re.Pattern | None = field(default=None, repr=False)
    money_re: re.Pattern | None = field(default=None, repr=False)
    anti_re: re.Pattern | None = field(default=None, repr=False)
    closed_re: re.Pattern | None = field(default=None, repr=False)
    noise_re: re.Pattern | None = field(default=None, repr=False)
    alias_re: re.Pattern | None = field(default=None, repr=False)

    def compile(self) -> None:
        self.stack_re = _build_alternation(list(self.stack_weights.keys()))
        self.hiring_re = _build_alternation(list(self.hiring_weights.keys()))
        self.pain_re = _build_alternation(list(self.pain_weights.keys()))
        self.money_re = _build_alternation(list(self.money_weights.keys()))
        self.anti_re = _build_alternation(self.anti_terms)
        self.closed_re = _build_alternation(self.closed_terms)
        self.noise_re = _build_alternation(list(self.noise_weights.keys()))
        self.alias_re = _build_alternation(list(self.aliases.keys()))


def _matched_terms(pattern: re.Pattern | None, text: str) -> list[str]:
    if pattern is None:
        return []
    seen: list[str] = []
    seen_set = set()
    for m in pattern.finditer(text):
        term = m.group(0)
        if term not in seen_set:
            seen_set.add(term)
            seen.append(term)
    return seen


@dataclass
class ScoreResult:
    score: int
    signals: dict[str, int]
    stack_matched: list[str]
    hiring_matched: list[str]
    pain_matched: list[str]
    help_request: bool
    intent_tag: str  # hiring_intent|pain_point|both|help_request|unknown
    compensation: str | None
    contact: str | None


CORE_STACK_MIN_WEIGHT = 3


def detect_help_request(
    text_norm: str,
    struct_features: dict,
    stack_matched: list[str],
    rules: "RulesCache | None" = None,
    noise_matched: list[str] | None = None,
) -> bool:
    """Запись выглядит как просьба о помощи ПО НАШЕЙ ТЕМЕ.

    Три ограничителя, без которых сигнал превращается в дыру, через которую
    в воронку льётся весь входящий поток (замерено на живых данных):

    1. Нужен термин стека, причём ЯДЕРНЫЙ (вес >= 3). Периферия — "react", "slack",
       "telegram", "notion" — слишком фоновая: "What IDE are you using?" ловилась на ней.
    2. Совпадение со словарём шума отменяет сигнал. Иначе "I built X", "just launched",
       "got my first paying user" проходят как "вопрос по теме" — это самопиар, не лид.
    3. Одного вопросительного знака в заголовке мало, если нет фразы-просьбы —
       он засчитывается только вместе с ядерным термином (условие 1).

    Строгие пути (точная фраза найма / боли) этими ограничителями не затронуты.
    """
    if not stack_matched:
        return False
    if noise_matched:
        return False
    if rules is not None:
        has_core = any(
            rules.stack_weights.get(t, 0) >= CORE_STACK_MIN_WEIGHT for t in stack_matched
        )
        if not has_core:
            return False
    if HELP_REQUEST_RE.search(text_norm):
        return True
    if struct_features.get("title_is_question"):
        return True
    # Послабление: запись может быть плотно про наши инструменты и про СВОЙ сетап,
    # но не содержать ни одной узнаваемой формы вопроса. Два и более ядерных термина —
    # достаточный повод отдать её классификатору. Это безопасно ровно потому, что
    # решает не этот сигнал, а LLM: на живом трафике она отклоняет подавляющее
    # большинство того, что сюда доходит. Цена послабления — квота, её запас кратный.
    if rules is not None:
        core_hits = sum(
            1 for t in stack_matched if rules.stack_weights.get(t, 0) >= CORE_STACK_MIN_WEIGHT
        )
        if core_hits >= 2:
            return True
    return False


def classify_intent(
    hiring_matched: list[str],
    pain_matched: list[str],
    tag: str | None,
    help_request: bool = False,
) -> str:
    hiring = bool(hiring_matched) or (tag in TAG_HIRING if tag else False)
    pain = bool(pain_matched)
    if hiring and pain:
        return "both"
    if hiring:
        return "hiring_intent"
    if pain:
        return "pain_point"
    if help_request:
        return "help_request"
    return "unknown"


def needs_llm_classification(score: int, intent_tag: str) -> bool:
    """Отдать ли запись LLM на классификацию.

    Всё, что пережило жёсткие фильтры, судит LLM. Балл решает только ОБЪЁМ
    (сколько кандидатов вообще доходит), но не вердикт "лид или нет".

    Раньше здесь стоял обход: при score >= grey_high классификация пропускалась,
    is_lead проставлялся true насильно, вызывался только перевод. Обход снят после
    разбора реальных уведомлений — доля рабочих карточек оказалась очень низкой.
    Причина: высокий балл набирается ПЛОТНОСТЬЮ нишевых слов, а в тематических
    источниках общая дискуссия густо усеяна теми же терминами. Запись с чужим
    советом, а не запросом на работу, набирала максимальный балл и уходила
    в уведомление без единой проверки. То есть балл коррелирует с насыщенностью
    терминами, а не с наличием заказа — доверять ему как замене классификации нельзя.

    Цена снятия обхода — несколько лишних вызовов в сутки при кратном запасе квоты.
    Точность здесь важнее экономии.
    """
    if intent_tag == "unknown":
        return False
    if intent_tag == "help_request":
        # Мягкий сигнал сам по себе права на вызов не даёт — нужен минимальный балл
        return score >= settings.help_request_min_score
    return True


def score_item(
    text_norm: str,
    struct_features: dict,
    tag: str | None,
    category: str,
    source_priority: int,
    author_reputation_bonus: int,
    rules: RulesCache,
    soft_help: bool = True,
) -> ScoreResult:
    """soft_help=False отключает мягкий сигнал "вопрос по теме" — рычаг для A/B-замера
    его вклада в scripts/offline_dryrun.py, в проде всегда True."""
    signals: dict[str, int] = {}

    # A. Стек — попадает ли задача в профиль исполнителя
    stack_matched = _matched_terms(rules.stack_re, text_norm)
    a_score = min(sum(rules.stack_weights.get(t, 0) for t in stack_matched), rules.stack_cap)
    if a_score:
        signals["stack"] = a_score

    # Шум считаем здесь, а не в конце: он нужен как вето для мягкого help-сигнала ниже
    noise_matched = _matched_terms(rules.noise_re, text_norm)

    # B. Сигнал намерения: явный найм + неявная боль + денежные ключевики
    hiring_matched = _matched_terms(rules.hiring_re, text_norm)
    pain_matched = _matched_terms(rules.pain_re, text_norm)
    money_matched = _matched_terms(rules.money_re, text_norm)
    b_raw = (
        sum(rules.hiring_weights.get(t, 0) for t in hiring_matched)
        + sum(rules.pain_weights.get(t, 0) for t in pain_matched)
        + sum(rules.money_weights.get(t, 0) for t in money_matched)
    )
    b_score = min(b_raw, rules.intent_cap)
    if b_score:
        signals["intent"] = b_score

    # C. Структурные признаки записи
    c_score = 0
    if SECTION_HEADER_RE.search(text_norm):
        w = rules.rule_weights.get("has_sections", 3)
        c_score += w
        signals["has_sections"] = w
    contact_m = CONTACT_RE.search(text_norm)
    if contact_m:
        w = rules.rule_weights.get("has_contact", 2)
        c_score += w
        signals["has_contact"] = w
    money_match = MONEY_AMOUNT_RE.search(text_norm)
    if money_match:
        w = rules.rule_weights.get("has_money_pattern", 3)
        c_score += w
        signals["has_money_pattern"] = w
    if struct_features.get("bullet_line_count", 0) >= 3:
        w = rules.rule_weights.get("has_bullets", 2)
        c_score += w
        signals["has_bullets"] = w
    length = struct_features.get("length", 0)
    if length > 500:
        w = rules.rule_weights.get("long_len", 1)
        c_score += w
        signals["long_len"] = w
    if length < 180:
        w = rules.rule_weights.get("short_len", -3)
        c_score += w
        signals["short_len"] = w
    if not struct_features.get("has_body", False):
        w = rules.rule_weights.get("no_body", -2)
        c_score += w
        signals["no_body"] = w
    if not stack_matched:
        # Заказ есть, но ни одного слова из профиля исполнителя. Не жёсткий фильтр:
        # заказчик часто описывает задачу бытовым языком ("pull my orders into a sheet").
        w = rules.rule_weights.get("no_stack", -3)
        c_score += w
        signals["no_stack"] = w

    help_request = soft_help and detect_help_request(
        text_norm, struct_features, stack_matched, rules, noise_matched
    )
    if help_request:
        w = rules.rule_weights.get("help_question", 2)
        c_score += w
        signals["help_question"] = w

    # D. Контекст источника: метка записи + категория + ручной приоритет
    d_score = 0
    if tag:
        if tag in TAG_HIRING:
            key = "tag_task" if tag == "task" else "tag_hiring"
            w = rules.rule_weights.get(key, 6)
            d_score += w
            signals[key] = w
    if category == "job_board":
        w = rules.rule_weights.get("category_job_board", 3)
        d_score += w
        signals["category_job_board"] = w
    elif category == "ai_automation":
        w = rules.rule_weights.get("category_ai_automation", 1)
        d_score += w
        signals["category_ai_automation"] = w
    elif category == "business_ops":
        w = rules.rule_weights.get("category_business_ops", 1)
        d_score += w
        signals["category_business_ops"] = w
    if source_priority:
        d_score += source_priority
        signals["source_priority"] = source_priority
    if author_reputation_bonus:
        d_score += author_reputation_bonus
        signals["author_reputation"] = author_reputation_bonus

    # Мягкие штрафы за шум (совпадения посчитаны выше, до help-сигнала)
    noise_score = max(
        sum(rules.noise_weights.get(t, -4) for t in noise_matched), rules.noise_cap
    )
    if noise_score:
        signals["noise"] = noise_score

    total = a_score + b_score + c_score + d_score + noise_score

    return ScoreResult(
        score=total,
        signals=signals,
        stack_matched=stack_matched,
        hiring_matched=hiring_matched,
        pain_matched=pain_matched,
        help_request=help_request,
        intent_tag=classify_intent(hiring_matched, pain_matched, tag, help_request),
        compensation=money_match.group(0) if money_match else None,
        contact=contact_m.group(0) if contact_m else None,
    )
