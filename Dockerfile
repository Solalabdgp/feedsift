# --- Стадия сборки: тут живут компиляторы, в финальный образ они не попадают ---
FROM python:3.12-slim AS builder

# gcc/libpq-dev нужны только на случай сборки из исходников. psycopg2-binary и
# остальные зависимости ставятся из бинарных wheels, но оставлять компилятор
# в рабочем образе смысла нет — он там только расширяет поверхность атаки.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Финальный образ: только рантайм ---
FROM python:3.12-slim

# libpq5 — рантайм-часть libpq для psycopg2 (без dev-заголовков и компилятора)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin feedsift

COPY --from=builder /install /usr/local

# Единственный каталог, куда контейнеру можно писать. Каталог с кодом (/srv)
# намеренно остаётся принадлежащим root и доступным только на чтение.
# Сюда celery beat кладёт файл расписания.
RUN mkdir -p /data && chown feedsift:feedsift /data

WORKDIR /srv
COPY --chown=feedsift:feedsift app ./app
COPY --chown=feedsift:feedsift scripts ./scripts
COPY --chown=feedsift:feedsift alembic ./alembic
COPY --chown=feedsift:feedsift alembic.ini .

# Непривилегированный пользователь. Ни один сервис не пишет в файловую систему
# контейнера и не слушает порты — root не нужен ни для чего.
USER feedsift

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "app.worker"]
