"""Аутентификация веб-дашборда (app/dashboard.py).

Пользователь ровно один — владелец. Отсюда всё остальное: нет таблицы users,
нет регистрации, нет восстановления пароля, нет ролей. Пароль хранится как
Argon2id-хэш в переменной окружения, состояние сессии — в Redis.

Модули:
    security.py        — Argon2id + пеппер, проверка пароля
    session.py         — токен сессии в Redis (create/touch/destroy)
    rate_limit.py      — счётчик неудачных входов по IP + эскалация блокировки
    dependency.py      — require_session, зависимость защищённых роутов
    router.py          — POST /auth/login, POST /auth/logout
    origin_guard.py    — CSRF-проверка Origin на небезопасных методах
    security_headers.py — заголовки безопасности на каждый ответ
"""
