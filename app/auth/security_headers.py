"""Заголовки безопасности на каждый ответ дашборда.

CSP здесь строгая до жёсткости: ни 'unsafe-inline', ни CDN. Дашборд отдаёт JSON,
фронтенд к нему приедет своими файлами с того же origin, так что послаблять
нечего. Ровно по этой же причине выключена встроенная Swagger UI (её HTML тянет
скрипт и стили со стороннего CDN и инлайнит инициализацию — под такую CSP она
не живёт, а ослаблять CSP ради страницы с документацией — плохой размен).
Схема OpenAPI при этом никуда не делась, она отдаётся по /api/openapi.json
и закрыта сессией.

HSTS ставится только при dashboard_cookie_secure. Флаг один и тот же по смыслу:
«сервис работает по https». Слать HSTS с http-стенда — значит записать браузеру
разработчика редирект на https для localhost, который потом руками не убрать.

Cache-Control: no-store — на всё. Ответы дашборда это чужие лиды, контакты и
суммы сделок; общий прокси или дисковый кэш браузера им не место.
"""
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings

CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        # Формы никуда, кроме себя, не отправляются, базового URL нет,
        # встраивание в чужой фрейм запрещено (frame-ancestors — актуальная
        # замена X-Frame-Options, он ниже оставлен для старых браузеров).
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    )
)

_HEADERS: dict[str, str] = {
    "content-security-policy": CSP,
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    # Никаких URL дашборда во внешние логи: в пути лежат id лидов.
    "referrer-policy": "no-referrer",
    "permissions-policy": "geolocation=(), camera=(), microphone=(), payment=(), usb=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "cross-origin-embedder-policy": "require-corp",
    "cache-control": "no-store",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.headers = dict(_HEADERS)
        if settings.dashboard_cookie_secure:
            self.headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    # setdefault, а не __setitem__: отдельный ответ вправе
                    # переопределить заголовок под себя.
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
