"""CSRF: небезопасный метод обязан прийти с известного Origin.

Кука сессии уже помечена SameSite=Strict, и одного этого почти достаточно —
браузер не приложит её к запросу, начатому с чужого сайта. «Почти» здесь
и закрывается: SameSite реализуется браузером, то есть защита целиком лежит
на стороне, которую мы не контролируем (старый мобильный вебвью, флаг в
about:config, будущая дырка в трактовке «site»). Проверка Origin на сервере
не зависит ни от чего из этого.

Отдельного CSRF-токена нет намеренно: он даёт то же самое, но требует выдачи,
хранения и ротации, а это лишний механизм на дашборд с одним пользователем.

Отсутствие И Origin, И Referer на POST/PUT/PATCH/DELETE трактуется как отказ.
Так делает Django, и причина та же: заголовок мог не проставить кто угодно,
а гадать по остаткам — значит оставить обходной путь. Побочный эффект: запрос
curl'ом нужно слать с явным `-H "Origin: $DASHBOARD_ORIGIN"`.

Чистый ASGI, не BaseHTTPMiddleware: здесь нужно только посмотреть на scope и
либо пропустить, либо ответить 403 — оборачивать ради этого тело ответа
в очередь и отдельную задачу незачем.
"""
from urllib.parse import urlsplit

import structlog
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = structlog.get_logger(__name__)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_of(url: str) -> str | None:
    """scheme://host[:port] из абсолютного URL. None, если URL не абсолютный."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


class OriginGuardMiddleware:
    def __init__(self, app: ASGIApp, allowed_origin: str) -> None:
        self.app = app
        self.allowed_origin = allowed_origin.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None:
            referer = headers.get("referer")
            origin = _origin_of(referer) if referer else None

        if origin is None or origin.rstrip("/") != self.allowed_origin:
            log.warning(
                "dashboard.csrf.rejected",
                method=scope["method"],
                path=scope.get("path"),
                origin=origin,
            )
            response = JSONResponse(
                {"detail": "Запрос отклонён: недопустимый Origin"},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
