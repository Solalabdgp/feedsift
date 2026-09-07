# feedsift

A content monitoring pipeline: it pulls items from an external RSS/Atom style source, queues them,
filters them with rules, sends the uncertain ones to an LLM for classification, and delivers the
result to a Telegram chat.

This repository is an integration example. The source adapter is deliberately thin and
configuration driven, so the same pipeline works against any feed that returns Atom or RSS entries.
Everything downstream of the queue knows nothing about where an item came from.

## Pipeline

```
collector ──> Redis Stream (queue:raw) ──> worker ──> Postgres
(conditional GET,                        (dedup, hard filters,      │
 one request per cycle)                   keyword scoring)          │ uncertain items
                                                                    ▼
     bot <──── notify / digest queues <──── llm-worker (rate limited)
  (aiogram)                                          │
                                              Postgres + usage log
```

Nine containers in `docker-compose.yml`: postgres, redis, migrate, collector, worker, llm-worker,
bot, beat, backup.

## Components

### collector

A synchronous poller. One HTTP request per cycle covers the whole set of configured sources,
because the source endpoint accepts a combined listing in the URL path. Details worth knowing:

- Conditional requests. `ETag` and `Last-Modified` from the previous response are stored in Redis
  and sent back as `If-None-Match` / `If-Modified-Since`, so an unchanged feed costs a 304.
- Randomized interval between cycles (90 to 120 seconds by default) instead of a fixed one.
- Exponential backoff on HTTP 429, starting at 60 seconds and capped at 900, plus a separate
  branch for 401/403 that pauses and raises an alert instead of hammering the source.
- Redirects are not followed. Credentials travel in the request, and an unconditional redirect
  would forward them to whatever host the source names in `Location`.
- Items older than a configurable age are dropped before they reach the queue.
- Deduplication at the edge: `SET key 1 EX 7d NX` on the item id, so a restart or an overlapping
  feed window does not republish anything.
- The source list is read from Postgres and refreshed every 10 minutes, with a static seed as a
  fallback so a database hiccup does not stop collection.

Publishing goes to a Redis Stream, which is the only contract between the collector and the rest
of the system. Replacing the source means rewriting one file.

### worker

An async consumer group reader over the same stream. Per item:

1. Normalization: text cleanup, alias mapping, structural features, headline extraction.
2. Deduplication in two passes: exact hash first, then simhash64 with a Hamming distance check
   against recent candidates, which catches reposts and lightly edited copies.
3. Hard filters, a numbered chain where each rule can reject the item and records which one did.
   A rejected item is still stored, with `reject_rule` set, so filter changes can be measured
   against real traffic later.
4. Keyword scoring in layers with per layer caps, a notify threshold, and a grey band around it.

Items that clear the filters become rows in `matches`. Confident ones go straight to delivery.
Items in the grey band, or ones that matched only on soft signals, are handed to the LLM stage.

### llm-worker

A Celery worker on its own queue. The LLM is not in the hot path: it sees only what the rules
could not decide, which is a small fraction of the stream.

Quota control has three layers, because a free tier provider will cut you off mid day otherwise:

- a Celery `rate_limit` on the queue,
- a per minute counter in Redis,
- a per day counter in Redis.

When quota runs out the task fails closed instead of retrying. The row is marked as decided by
rules only, the notification still goes out, and the daily budget is not burned by a retry loop.
Rate limit headers from the provider are read off every response and logged, so real limits are
observed rather than assumed.

Optional key rotation: if several API keys are configured, a 429 on the current key moves the same
call to the next key in the pool. With one key configured the pool collapses to that key and the
code path is unchanged.

Every call is written to a usage log table with mode, model, outcome, token counts, latency and
error, which is what the reporting and the quota command in the bot read from.

### bot

An aiogram bot, single owner. Everything else is ignored without a reply. It delivers cards,
takes feedback on them, and doubles as the admin console:

| Command | What it does |
|---|---|
| `/stats [day\|week]` | read, passed filters, delivered, precision from feedback |
| `/sources` | configured sources with category and priority |
| `/mute <source> [hours]`, `/unmute <source>` | silence a source |
| `/keywords <type>`, `/keywords add\|del ...` | edit dictionaries live |
| `/weights [set <key> <N>]` | scoring rule weights |
| `/threshold [N]` | delivery threshold |
| `/quota` | LLM spend for the day |
| `/dryrun N` | replay the last N items through the current rules |
| `/why <id>` | score breakdown for one card |
| `/pause`, `/resume`, `/digest`, `/favorites` | delivery control |

Dictionary and weight edits are picked up by the worker within about 15 seconds through a Redis
signal key. No restart, no redeploy.

Quiet hours are configurable. Anything decided during them is queued into a digest instead of
waking the owner up.

### beat

Scheduled maintenance on a separate queue, so reports never take the rate limited LLM slot:

- weekly quality report,
- retention cleanup, default 30 days,
- heartbeat check every 10 minutes, which alerts if a collector stopped writing its heartbeat key.

## Storage

Postgres through SQLAlchemy 2.x async, migrations with Alembic. Main tables: `sources`,
`raw_items`, `matches`, `feedback`, `keywords`, `rule_weights`, `authors`, `llm_usage_log`,
`settings`.

Two things worth pointing out in the schema. Source attributes are denormalized onto items and
matches at ingest time, so the delivery path and the reporting queries need no joins. And every
match carries how it was decided (rules, llm, or rules only) plus the raw verdict, which makes it
possible to audit the classifier against real traffic afterwards.

## Two circuits on one stack

The same code runs two independent monitoring circuits, `main` and `browse`, separated only by
environment variables. Each circuit has its own source list, its own credentials, and its own LLM
budget. The code does not fork: `circuit` picks which source rows the collector reads, which Redis
key prefixes it uses for quota and heartbeat, and which rows the reporting filters on. The worker
and the bot are shared, because an item's circuit comes from its source row rather than from the
process environment.

## Configuration and secrets

`.env.example` lists everything. Copy it, fill it, and note that `.env` is gitignored.

Secrets are handed to services one by one in `docker-compose.yml` rather than through a shared
`env_file`. Redis gets none. The collector gets only the source credentials, the bot only its
Telegram token, the llm-worker only the LLM key. A missing required variable stops the service at
startup twice over: `docker compose` refuses through `${VAR:?}`, and `config.require()` refuses
inside the process with a list of what is missing.

Two more things that came out of a security pass and are worth copying:

- HTTP client loggers are pinned to WARNING. At INFO, `httpx` prints the full request URL, and any
  credential carried as a query parameter lands in container logs in plain text.
- Postgres and Redis publish no ports to the host. The database password has no default value in
  code, so a dropped variable cannot silently start a service with a well known password.

## Running it

```bash
cp .env.example .env    # fill in real values
docker compose up -d --build
docker compose logs -f collector worker llm-worker
```

Manual backup before a deploy or a migration:

```bash
docker compose run --rm --entrypoint sh backup /scripts/backup.sh once
```

The backup script writes with `umask 077` into a `.part` file and publishes it only after a size
check, rotating old copies only after a successful run. `set -eu` plus `pipefail`, so a failed dump
cannot leave a valid looking empty archive behind.

### Rules dry run without Docker

```bash
python -m scripts.offline_dryrun
```

Runs a batch of items through the current dictionaries locally, with no Postgres, no Redis and no
LLM calls. It prints what each filter rejected, how much would have been delivered, and how many
LLM calls that would have cost. This is the tool for tuning dictionaries before touching anything
in production.

## Image and runtime

Multi stage build. The final image carries no compiler and no build headers, runs as a non root
user with uid 10001, and mounts the application directory read only. Celery beat writes its
schedule to a dedicated volume, since the code directory is not writable for that user.

## Stack

Python 3.12, httpx, redis-py, SQLAlchemy 2.x with asyncpg, Alembic, Celery, aiogram 3, structlog,
pydantic-settings, Postgres 16, Redis 7, Docker Compose.

## About this repository

This is an extract of a private project, published as an integration and architecture example. The
source adapter, the dictionaries and the prompts shipped here are neutral placeholders, not the
ones the original runs with. Git history starts at import: the code was developed privately before
this repository existed.
