"""Collector: синхронный поллер Atom/RSS-фида.

Адаптер над публичным фидом источника: один HTTP-запрос на цикл на ВСЕ источники
контура сразу через "+"-склейку в шаблоне URL (settings.source_feed_url_template).
Учётные данные фида едут параметрами запроса user=/feed=.

Интервал 90-120 с — буфер под лимит провайдера "один запрос в минуту".
ETag/Last-Modified кэшируются в Redis, чтобы не тянуть неизменившийся фид целиком.

Ничего не публикует и не изменяет на стороне источника. Только GET.
"""
import html as html_mod
import json
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from sqlalchemy import create_engine, text as sql_text

from app.config import require, settings
from app.logging_setup import configure_logging
from app.normalize import extract_tag
from app.redis_client import (
    CHANNEL_ALERTS,
    KEY_FEED_ETAG,
    KEY_FEED_LAST_MODIFIED,
    KEY_HEARTBEAT_COLLECTOR,
    KEY_SEEN_ITEM,
    STREAM_RAW,
    get_sync_redis,
)
from app.sources import SOURCE_NAMES, SOURCE_NAMES_BROWSE

log = structlog.get_logger("collector")

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

SEEN_TTL_SEC = 7 * 24 * 3600
HEARTBEAT_TTL_SEC = 900
SOURCE_LIST_REFRESH_SEC = 600
BACKOFF_BASE_SEC = 60
BACKOFF_MAX_SEC = 900

# Провайдер оборачивает тело записи парой HTML-комментариев-сентинелов внутри
# <content type="html">. Всё, что вне этой пары — служебный подвал фида
# (ссылки "автор"/"комментарии"), в тело записи он не входит.
BODY_MARKER_RE = re.compile(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")


def _sync_db_url() -> str:
    return settings.database_url.replace("+asyncpg", "+psycopg2")


def extract_body_text(content_html: str | None) -> tuple[str, bool]:
    """Достаёт тело записи из HTML-содержимого фида, без служебного подвала.

    У записи-ссылки (без собственного текста) блок сентинелов отсутствует вовсе.
    Возвращает (текст, has_body).
    """
    if not content_html:
        return "", False
    m = BODY_MARKER_RE.search(content_html)
    if not m:
        return "", False
    body = TAG_RE.sub(" ", m.group(1))
    body = html_mod.unescape(html_mod.unescape(body))
    body = WS_RE.sub(" ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip(), True


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items: list[dict] = []
    for entry in root.findall("a:entry", ATOM_NS):
        eid = entry.find("a:id", ATOM_NS)
        title_el = entry.find("a:title", ATOM_NS)
        cat = entry.find("a:category", ATOM_NS)
        link = entry.find("a:link", ATOM_NS)
        author = entry.find("a:author/a:name", ATOM_NS)
        published = entry.find("a:published", ATOM_NS)
        content = entry.find("a:content", ATOM_NS)

        if eid is None or title_el is None or title_el.text is None:
            continue
        title = html_mod.unescape(title_el.text)
        body, has_body = extract_body_text(content.text if content is not None else None)
        author_name = (author.text or "").rsplit("/", 1)[-1] if author is not None else None
        posted_at = published.text if published is not None else None

        items.append(
            {
                "item_id": eid.text,
                "source": cat.get("term") if cat is not None else None,
                "title": title,
                "body": body,
                "has_body": has_body,
                "tag": extract_tag(title),
                "author_handle": author_name or None,
                "permalink": link.get("href") if link is not None else None,
                "posted_at": posted_at,
            }
        )
    return items


# Контур -> статический seed-фолбэк, если Postgres моргнул.
_STATIC_SEED_BY_CIRCUIT = {
    "main": SOURCE_NAMES,
    "browse": SOURCE_NAMES_BROWSE,
}


class SourceList:
    """Список источников СВОЕГО контура из БД с фолбэком на статический seed, чтобы
    коллектор не вставал колом, если Postgres моргнул.

    circuit фильтрует список на уровне SQL (WHERE circuit = :circuit), а не через
    отдельную таблицу — второй коллектор (browse) не должен видеть/опрашивать
    источники основного контура и наоборот: у контуров разные учётные данные фида,
    каждая со своим лимитом запросов.
    """

    def __init__(self, circuit: str) -> None:
        self._circuit = circuit
        self._engine = create_engine(_sync_db_url(), pool_pre_ping=True)
        self._names: list[str] = list(_STATIC_SEED_BY_CIRCUIT.get(circuit, SOURCE_NAMES))
        self._loaded_at = 0.0

    def get(self) -> list[str]:
        if time.time() - self._loaded_at < SOURCE_LIST_REFRESH_SEC:
            return self._names
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sql_text(
                        "SELECT name FROM sources WHERE enabled IS TRUE AND circuit = :circuit "
                        "ORDER BY name"
                    ),
                    {"circuit": self._circuit},
                ).all()
            names = [r[0] for r in rows]
            if names:
                self._names = names
        except Exception:
            log.exception("source_list_refresh_failed")
        self._loaded_at = time.time()
        return self._names


def build_url(sources: list[str]) -> str:
    return settings.source_feed_url_template.format(sources="+".join(sources))


def fetch_feed(
    client: httpx.Client, sources: list[str], etag: str | None, last_modified: str | None
):
    params = {
        "user": settings.source_feed_user,
        "feed": settings.source_feed_token,
        "limit": str(settings.feed_limit),
    }
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return client.get(build_url(sources), params=params, headers=headers)


def publish(redis, item: dict) -> bool:
    """Публикует запись в Redis Stream. False — если этот item_id уже видели."""
    key = KEY_SEEN_ITEM.format(item_id=item["item_id"])
    if not redis.set(key, "1", ex=SEEN_TTL_SEC, nx=True):
        return False
    redis.xadd(STREAM_RAW, {"data": json.dumps(item, ensure_ascii=False)})
    return True


def _too_old(posted_at: str | None) -> bool:
    if not posted_at:
        return False
    try:
        dt = datetime.fromisoformat(posted_at)
    except ValueError:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.max_item_age_min)
    return dt < cutoff


def main() -> None:
    configure_logging()
    require(
        "database_url",
        "redis_url",
        "source_feed_user",
        "source_feed_token",
        "source_feed_url_template",
    )

    circuit = settings.circuit
    redis = get_sync_redis()
    source_list = SourceList(circuit)
    key_etag = KEY_FEED_ETAG.format(circuit=circuit)
    key_last_modified = KEY_FEED_LAST_MODIFIED.format(circuit=circuit)
    key_heartbeat = KEY_HEARTBEAT_COLLECTOR.format(circuit=circuit)
    backoff = 0
    client = httpx.Client(
        timeout=30.0,
        # follow_redirects=False намеренно: учётные данные фида едут в query-строке,
        # а безусловный редирект отправил бы их на любой хост, который провайдер
        # укажет в Location. Если начнёт редиректить — увидим это как
        # feed_unexpected_status, а не как утечку кредов.
        follow_redirects=False,
        headers={"User-Agent": settings.source_user_agent},
    )

    log.info(
        "collector_started",
        circuit=circuit,
        sources=len(source_list.get()),
        limit=settings.feed_limit,
    )

    while True:
        try:
            sources = source_list.get()
            etag = redis.get(key_etag)
            last_modified = redis.get(key_last_modified)
            resp = fetch_feed(client, sources, etag, last_modified)

            if resp.status_code == 429:
                backoff = min(max(BACKOFF_BASE_SEC, backoff * 2), BACKOFF_MAX_SEC)
                log.warning("feed_rate_limited", circuit=circuit, backoff=backoff)
                redis.publish(
                    CHANNEL_ALERTS,
                    f"[{circuit}] Фид вернул 429, пауза {backoff}с. "
                    "Если повторяется — интервал поллинга мал.",
                )
                time.sleep(backoff)
                continue

            if resp.status_code in (401, 403):
                # Наиболее вероятная причина — протухли или отозваны учётные данные фида
                log.error("feed_forbidden", circuit=circuit, status=resp.status_code)
                redis.publish(
                    CHANNEL_ALERTS,
                    f"[{circuit}] Фид отдал {resp.status_code}. Проверь учётные данные "
                    "SOURCE_FEED_USER/SOURCE_FEED_TOKEN.",
                )
                time.sleep(BACKOFF_MAX_SEC)
                continue

            if resp.status_code == 304:
                log.info("feed_not_modified", circuit=circuit)
            elif resp.status_code != 200:
                log.warning("feed_unexpected_status", circuit=circuit, status=resp.status_code)
                time.sleep(BACKOFF_BASE_SEC)
                continue
            else:
                backoff = 0
                if resp.headers.get("ETag"):
                    redis.set(key_etag, resp.headers["ETag"], ex=3600)
                if resp.headers.get("Last-Modified"):
                    redis.set(key_last_modified, resp.headers["Last-Modified"], ex=3600)

                items = parse_feed(resp.text)
                published_count = 0
                skipped_old = 0
                for item in items:
                    if _too_old(item["posted_at"]):
                        skipped_old += 1
                        continue
                    if publish(redis, item):
                        published_count += 1
                log.info(
                    "feed_cycle",
                    circuit=circuit,
                    entries=len(items),
                    published=published_count,
                    skipped_old=skipped_old,
                )

            redis.set(key_heartbeat, str(int(time.time())), ex=HEARTBEAT_TTL_SEC)

        except Exception:
            log.exception("feed_cycle_failed", circuit=circuit)
            time.sleep(BACKOFF_BASE_SEC)
            continue

        time.sleep(random.uniform(settings.poll_interval_min_sec, settings.poll_interval_max_sec))


if __name__ == "__main__":
    main()
