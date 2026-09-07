"""Жёсткие фильтры HF-1..HF-7: первое срабатывание останавливает обработку записи.

Принципиальные решения по наполнению:
- HF-2 режет отсутствие СИГНАЛА НАМЕРЕНИЯ, а не отсутствие стека. Стек — мягкий слой
  скоринга: заказчик редко пишет "python", он пишет "I need my orders pulled into
  a spreadsheet".
- HF-4 не режет вопросительную форму: это убило бы ровно тот класс записей, ради
  которого проект и делается (pain-point почти всегда вопрос). Режется только
  короткая запись БЕЗ явного найма и БЕЗ боли.
- HF-5 ловит не резюме соискателя, а встречный оффер: "я фрилансер, наймите меня".
- HF-6 — запись без тела (только ссылка), в Atom это пустой <content>.
"""
import re

from app.scoring import TAG_HIRING, TAG_SERVICE_OFFER

MIN_LENGTH = 40
MAX_LENGTH = 20000
ANTI_HEAD_WINDOW = 300


class TokenIndex:
    """Индекс токенов для проверки близости совпадений (HF-3/HF-5)."""

    def __init__(self, text: str):
        self.text = text
        self._tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]

    def token_index_at(self, char_pos: int) -> int:
        for i, (start, _end) in enumerate(self._tokens):
            if start > char_pos:
                return max(0, i - 1)
        return len(self._tokens) - 1 if self._tokens else 0

    def near(self, pos_a: int, pos_b: int, window: int = 5) -> bool:
        return abs(self.token_index_at(pos_a) - self.token_index_at(pos_b)) <= window


def hf1_length(text_norm: str) -> str | None:
    n = len(text_norm)
    if n < MIN_LENGTH or n > MAX_LENGTH:
        return "HF-1_length"
    return None


def hf2_no_signal(
    hiring_matched: list[str],
    pain_matched: list[str],
    tag: str | None,
    help_request: bool = False,
    skip: bool = False,
) -> str | None:
    """Три пути пройти фильтр, от жёсткого к мягкому:
    точная фраза найма, точная фраза боли, или (мягко) вопрос по теме стека.

    Третий путь добавлен после первого живого прогона: HF-2 срезал подавляющее
    большинство записей, и основная часть отсева пришлась на людей, которые
    формулируют запрос своими словами, а не фразами из словаря.

    skip=True (browse-контур) отключает фильтр целиком: словари hiring_re/pain_re
    и ядерный стек в detect_help_request откалиброваны под один домен и дают
    дырявое частичное покрытие на других нишах — там, где контур B должен решать
    сам через более мягкий LLM-промпт, а не тихо отсеиваться здесь до LLM.
    """
    if skip:
        return None
    if hiring_matched or pain_matched or help_request:
        return None
    if tag and tag in TAG_HIRING:
        return None
    return "HF-2_no_signal"


def hf3_closed(text_norm: str, closed_re: re.Pattern | None, hiring_re: re.Pattern | None) -> str | None:
    if closed_re is None:
        return None
    idx = TokenIndex(text_norm)
    for m in closed_re.finditer(text_norm):
        # маркер в заголовке/первых строках — типичное "EDIT: found someone"
        if m.start() < 200:
            return "HF-3_closed"
        if hiring_re is not None:
            hire_m = hiring_re.search(text_norm)
            if hire_m and idx.near(m.start(), hire_m.start(), window=6):
                return "HF-3_closed"
    return None


def hf4_low_info(
    text_norm: str,
    hiring_matched: list[str],
    pain_matched: list[str],
    help_request: bool = False,
    skip: bool = False,
) -> str | None:
    """Короткая запись, где не сработало вообще ничего осмысленного.
    Pain-point и вопрос по теме в вопросительной форме НЕ режутся: это целевой класс.

    skip=True — см. hf2_no_signal выше, тот же аргумент: словарный сигнал ненадёжен
    вне основного домена, browse-контур пропускает эту проверку.
    """
    if skip:
        return None
    if len(text_norm) >= 150:
        return None
    if hiring_matched or pain_matched or help_request:
        return None
    return "HF-4_low_info"


def hf5_service_offer(text_norm: str, tag: str | None, anti_re: re.Pattern | None) -> str | None:
    """Автор сам продаёт услуги — конкурент, а не лид."""
    if tag and tag in TAG_SERVICE_OFFER:
        return "HF-5_service_offer"
    if anti_re is None:
        return None
    m = anti_re.search(text_norm)
    if m and m.start() < ANTI_HEAD_WINDOW:
        return "HF-5_service_offer"
    return None


def hf6_no_body(has_body: bool, hiring_matched: list[str], tag: str | None) -> str | None:
    """Запись без текста (только ссылка): оставляем, только если найм виден в заголовке."""
    if has_body:
        return None
    if hiring_matched or (tag and tag in TAG_HIRING):
        return None
    return "HF-6_no_body"


def hf7_duplicate() -> str | None:
    # Дубли обрабатываются в dedup.py до вызова остальных HF — заглушка для нумерации.
    return None


def run_hard_filters(
    text_norm: str,
    hiring_matched: list[str],
    pain_matched: list[str],
    tag: str | None,
    has_body: bool,
    closed_re: re.Pattern | None,
    anti_re: re.Pattern | None,
    hiring_re: re.Pattern | None,
    help_request: bool = False,
    skip_soft_gate: bool = False,
) -> str | None:
    """HF-1..HF-6 по порядку, первое срабатывание останавливает обработку.

    skip_soft_gate (browse-контур) отключает только HF-2/HF-4 (сигнал по словарям
    hiring/pain, ненадёжный вне основного домена) — HF-1 (длина), HF-3 (уже закрыто),
    HF-5 (встречный оффер) и HF-6 (нет тела записи) доменно-нейтральны и остаются
    в силе для обоих контуров.
    """
    for check in (
        lambda: hf1_length(text_norm),
        lambda: hf2_no_signal(hiring_matched, pain_matched, tag, help_request, skip=skip_soft_gate),
        lambda: hf3_closed(text_norm, closed_re, hiring_re),
        lambda: hf4_low_info(text_norm, hiring_matched, pain_matched, help_request, skip=skip_soft_gate),
        lambda: hf5_service_offer(text_norm, tag, anti_re),
        lambda: hf6_no_body(has_body, hiring_matched, tag),
    ):
        result = check()
        if result:
            return result
    return None
