# Changelog

## [2.1.0] - 2026-11-07

### 🔒 Security, Multi-Feed API Support, & Unified Service Refactor

Refactored all three services (`tg-comment`, `anisearch`, `mongosync`) to decouple application secrets and state from image builds and git tracking, add rate-limit protection, and support both RSS and JSON API feeds.

### Added
- **API Feed Support in `tg-comment`** — Supports both RSS/Atom and JSON API feeds (`feed_type: "api"`) with customizable `base_url` for resolving relative paths to full URLs.
- **Comment Timestamp Footer in Telegram Messages** — Parses ISO 8601 (`createdAt`) and RFC/RSS dates to display a formatted timestamp footer (e.g. `🕒 Nov 04, 2026 • 20:47 UTC`) in each Telegram notification.
- **Rate Limit & Retry Handling** — Automatically detects Telegram HTTP 429 errors in `tg-comment`, respects `retry_after` parameter, and paces message dispatches with a configurable delay.
- **Rotating File & Stream Logging** — Standardized Python logging with `RotatingFileHandler` (5 MB, 3 backups) and `StreamHandler` across all three services:
  - `tg-comment/logs/tg-comment.log`
  - `anisearch/logs/anisearch.log`
  - `mongosync/logs/mongosync.log`
- **Config Templates (`example.config.json`)**:
  - `tg-comment/example.config.json` — multi-feed RSS and API template
  - `anisearch/example.config.json` — bot credentials, channel settings, and forum thread filters
  - `mongosync/example.config.json` — cluster URI, database names, and sync interval
- **Root `.dockerignore`** — Prevents active configs, database files, session data, virtual environments, and logs from being baked into container layers.
- **Pyrogram Dedicated Session Folder** — Configured `anisearch` to store `.session` files in `session/` to enable persistent host mounting without file-vs-directory collision.
- **`uvloop` Integration in `anisearch`** — Set `uvloop.EventLoopPolicy` for high-throughput asynchronous networking.
- **Batched & Flood-Safe Broadcast in `anisearch`** — Replaced instant broadcast loop with batched execution (25 users/batch), automatic `FloodWait` sleep-and-retry, inactive/blocked user pruning, live progress updates, and async task execution.
- **Configurable Branding & Start Buttons in `anisearch`** — Allows customizing bot title, website link, updates channel, community chat, or custom button layouts in `config.json` without editing Python code.
- **Group Mention & Reply Search in `anisearch`** — Enables searching in Telegram groups by mentioning the bot (`@bot keyword`) or replying to bot messages, alongside dedicated forum topic threads and optional global group text search.

### Changed
- **Redesigned `/start` and `/help` Messages** — Upgraded layout with clean typography, emoji badges, command guides, and dynamic community buttons.
- **`docker-compose.yml` Volume Mounts**:
  - Removed full directory bind mounts (`./tg-comment:/app/tg-comment`, `./anisearch:/app/anisearch`, `./mongosync:/app/mongosync`) since application code is already baked into the image.
  - Mounted `config.json` as read-only (`:ro`) from host for each service.
  - Mounted persistent state (`tg-feed-bot.db`, `session/`, and `logs/`) for each service.
  - Added Docker `json-file` logging driver (10 MB max, 3 files, compressed) to `anisearch` and `mongosync`.
- **Dynamic Multi-Source Database Reloading in `mongosync`** — Re-evaluates `source_databases` on every cycle so adding new source databases in `config.json` takes effect immediately without container restart.
- **`mongosync` Structure** — Encapsulated sync logic in modular functions and `main()`, adding graceful fallback when MongoDB credentials are missing.
- **`anisearch` DB Resilience** — Added safe handling for absent MongoDB connections in local development mode without unhandled exceptions.
- **Migrated to `wzgram[fast]`** — Replaced `pyroblack` and `TgCrypto-pyroblack` with `wzgram[fast]` in `requirements.txt`, providing Rust-based `WarpCrypto` acceleration and active MTProto support.

### Fixed
- **PyMongo Collection Truth Value Testing in `anisearch`** — Fixed `NotImplementedError: Collection objects do not implement truth value testing` by replacing `if not users_collection:` with `if users_collection is None:`.
- **Deprecated `datetime.utcnow()` Warning** — Replaced `datetime.utcnow()` with timezone-aware `datetime.now(timezone.utc)` in `anisearch/anidlsearch.py` for Python 3.12+ compatibility.
- **Force Subscription Prompt when Disabled** — Added `enable_force_sub` toggle (default `false`) and unified middleware check in `start_command`, preventing unwanted channel subscription prompts when force subscription is turned off.
- **Restricted Admin Commands Visibility** — `/stats` and `/broadcast` are now only displayed to authorized IDs in `ADMIN_IDS` in `/start` and `/help`, keeping the interface clean for regular users.
- **Exclude Admins from Broadcast Dispatches** — Updated `get_all_user_ids()` and `broadcast_command` in `anisearch/anidlsearch.py` to filter out `ADMIN_IDS` (using MongoDB `$nin` and type-safe Python set exclusion), preventing bot administrators from receiving redundant broadcast messages and ensuring confirmation prompts reflect the exact non-admin recipient count.

### Removed
- **Untracked Sensitive Configs** — Removed live `tg-comment/config.json` from git tracking and ignored all `**/config.json` in `.gitignore`.
- **Hardcoded Branding & Channel Defaults** — Replaced all real Telegram IDs, channel URLs, and domain names in `anidlsearch.py` and `example.config.json` with generic dummy fallbacks (`0`, `""`, `"Anime Search Bot"`).

### Estimated Impact
| Metric | Before | After |
|--------|--------|-------|
| Host code mounts | 3 full directories mounted over image | Read-only configs + state/logs only |
| Telegram rate-limit crashes | Unhandled Exception on 429 | Auto-retry with backoff + rate limiting |
| Supported Feed Types | RSS/Atom only | RSS/Atom + REST JSON API with link formatting |
| Secret leakage risk | High (tracked in git & baked in image) | Zero (`.gitignore` + `.dockerignore` + `:ro` mount) |

## [2.0.0] - 2026-11-07

### 🏗️ Docker Consolidation — Single Image, 3 Containers

Merged 3 separate Docker setups into one unified configuration to reduce disk usage and simplify deployment.

### Added
- **Root `Dockerfile`** — Multi-stage build using `python:3.12-slim-bookworm`
  - Stage 1 (builder): compiles C extensions (TgCrypto) with gcc
  - Stage 2 (final): slim runtime image with no compiler bloat
- **Root `docker-compose.yml`** — Single compose file managing all 3 services
  - `tg-comment` — RSS feed → Telegram comment notifier
  - `anisearch` — Telegram anime search bot
  - `mongosync` — MongoDB cross-database sync
- **Root `requirements.txt`** — Merged dependencies from all 3 apps
- **`.gitignore`** — Added coverage for `venv/`, `__pycache__/`, `*.session`, `*.pyc`, `tg-feed-bot.db`, and `logs/`

### Removed
- `tg-comment/Dockerfile` — replaced by root Dockerfile
- `tg-comment/docker-compose.yml` — replaced by root docker-compose.yml
- `tg-comment/requirements.txt` — merged into root requirements.txt
- `anisearch/Dockerfile` — replaced by root Dockerfile
- `anisearch/docker-compose.yml` — replaced by root docker-compose.yml
- `anisearch/requirements.txt` — merged into root requirements.txt
- `mongosync/Dockerfile` — replaced by root Dockerfile
- `mongosync/docker-compose.yml` — replaced by root docker-compose.yml
- `mongosync/requirements.txt` — merged into root requirements.txt
- `dataclasses` dependency — built-in since Python 3.7, unnecessary to install

### Changed
- **Base image standardized** to `python:3.12-slim-bookworm` (~150 MB) for all services
  - Was: `python:3.12.1-alpine3.19` (tg-comment), `python:3.12-bookworm` ~1 GB (anisearch), `python:3.11-slim-bookworm` (mongosync)
- **Python version unified** to 3.12 across all services (was mixed 3.11/3.12)
- **`PYTHONUNBUFFERED=1`** set globally — real-time log output for all containers
- **All services on `tg_net`** network — consistent networking
- **Logging config** added to tg-comment service (json-file driver, 10m max, 3 files, compressed)

### Estimated Impact
| Metric | Before | After |
|--------|--------|-------|
| Base image layers | ~1.8 GB (3 separate) | ~150 MB (1 shared) |
| Total disk | ~2.2 GB | ~350–400 MB |
| Compose files to manage | 3 | 1 |
| Build commands needed | 3 | 1 |

