"""Кнопки бота, ведущие в веб-дашборд.

Проверяется только сборка клавиатур — ни Telegram, ни БД для этого не нужны.
Главное здесь: при пустом DASHBOARD_ORIGIN кнопок быть не должно вовсе.
Инстанс без дашборда — штатная ситуация, а Telegram отказывается отправлять
сообщение с некорректным url, то есть лишняя кнопка стоила бы недоставленного
лида, а не просто нерабочей ссылки.
"""
import pytest

from app.bot import _card_keyboard, _dashboard_url
from app.config import settings

ORIGIN = "https://dash.example.com"


@pytest.fixture
def with_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dashboard_origin", ORIGIN)


@pytest.fixture
def without_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dashboard_origin", "")


def _urls(keyboard) -> list[str]:
    return [b.url for row in keyboard.inline_keyboard for b in row if b.url]


def test_dashboard_url_with_and_without_lead(with_origin) -> None:
    assert _dashboard_url() == ORIGIN
    assert _dashboard_url(42) == f"{ORIGIN}/?lead=42"


def test_trailing_slash_does_not_double_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dashboard_origin", ORIGIN + "/")
    assert _dashboard_url(42) == f"{ORIGIN}/?lead=42"


def test_no_origin_means_no_url(without_origin) -> None:
    assert _dashboard_url() is None
    assert _dashboard_url(42) is None


def test_card_keyboard_has_dashboard_button(with_origin) -> None:
    keyboard = _card_keyboard(7, "https://example.test/post/7")
    assert f"{ORIGIN}/?lead=7" in _urls(keyboard)
    # Отдельным рядом, последним — не втискивается к существующим кнопкам.
    assert [b.text for b in keyboard.inline_keyboard[-1]] == ["Открыть в дашборде"]


def test_card_keyboard_without_dashboard(without_origin) -> None:
    keyboard = _card_keyboard(7, "https://example.test/post/7")
    assert _urls(keyboard) == ["https://example.test/post/7"]
    assert not any(
        b.text == "Открыть в дашборде" for row in keyboard.inline_keyboard for b in row
    )


def test_existing_card_buttons_untouched(with_origin) -> None:
    """Кнопка дашборда добавляется, а не заменяет собой что-то из старых."""
    keyboard = _card_keyboard(7, "https://example.test/post/7")
    callbacks = [
        b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data
    ]
    assert callbacks == ["fav:7", "bad:7", "why:7", "orig:7"]


def test_card_without_permalink_still_gets_dashboard(with_origin) -> None:
    keyboard = _card_keyboard(7, None)
    assert _urls(keyboard) == [f"{ORIGIN}/?lead=7"]
