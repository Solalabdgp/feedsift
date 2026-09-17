"""require_session — зависимость всех защищённых роутов дашборда.

Навешивается не на каждый роут по отдельности, а на роутер целиком
(app/dashboard.py: APIRouter(dependencies=[Depends(require_session)])). Разница
существенная: при поштучной навеске новый эндпоинт открыт по умолчанию, и его
забывают закрыть. Здесь по умолчанию закрыт, а исключения (/health, /auth/login)
объявлены явно и их видно списком.
"""
from fastapi import HTTPException, Request, status

from app.auth.session import touch_session

COOKIE_NAME = "dash_session"


async def require_session(request: Request) -> str:
    """Токен живой сессии. 401, если куки нет или сессия недействительна.

    Побочный эффект намеренный: touch_session продлевает скользящее окно,
    то есть активная работа в дашборде сама держит сессию живой.
    """
    token = request.cookies.get(COOKIE_NAME, "")
    if not await touch_session(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Сессия не найдена или истекла",
        )
    # Чтобы /auth/logout мог погасить именно эту сессию, не разбирая куку заново.
    request.state.session_token = token
    return token
