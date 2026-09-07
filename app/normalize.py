"""Нормализация текста и структурные признаки англоязычных записей фида."""
import re

MD_CHARS_RE = re.compile(r"[*_`~]")
URL_RE = re.compile(r"https?://\S+")
BULLET_LINE_RE = re.compile(r"^\s*([-–—•*●▪]|\d+[.)])\s+", re.MULTILINE)
WHITESPACE_RE = re.compile(r"[ \t]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.\w+")
TAG_RE = re.compile(r"^\s*[\[(]\s*([A-Za-z /]{2,20})\s*[\])]")


def extract_tag(title: str) -> str | None:
    """Фид не отдаёт пользовательскую метку записи отдельным полем — вытаскиваем её
    из начала заголовка: "[Hiring] ...", "[For Hire] ...", "[Task] ...".
    """
    m = TAG_RE.match(title)
    return m.group(1).strip().lower() if m else None


def compute_struct_features(raw_text: str, has_body: bool) -> dict:
    lines = raw_text.split("\n")
    non_empty_lines = [ln for ln in lines if ln.strip()]
    bullet_lines = BULLET_LINE_RE.findall(raw_text)
    # Первая строка raw_text — всегда заголовок записи (worker склеивает title + body).
    first_line = non_empty_lines[0].strip() if non_empty_lines else ""
    return {
        "length": len(raw_text),
        "line_count": len(non_empty_lines),
        "bullet_line_count": len(bullet_lines),
        "has_links": bool(URL_RE.search(raw_text)),
        "has_email": bool(EMAIL_RE.search(raw_text)),
        "has_body": has_body,
        "title_is_question": first_line.endswith("?"),
    }


def normalize_text(raw_text: str) -> str:
    text = MD_CHARS_RE.sub("", raw_text)
    text = text.lower()
    text = WHITESPACE_RE.sub(" ", text)
    text = BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def apply_aliases(text_norm: str, alias_pattern: re.Pattern | None, aliases: dict[str, str]) -> str:
    if alias_pattern is None or not aliases:
        return text_norm

    def _sub(m: re.Match) -> str:
        return aliases.get(m.group(0), m.group(0))

    return alias_pattern.sub(_sub, text_norm)


def extract_headline(title: str, max_len: int = 90) -> str:
    return title.strip()[:max_len]
