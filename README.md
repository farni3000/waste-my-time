# waste-my-time

Local, single-user game recommender. It syncs your Steam library, adds games you
played elsewhere, collects your interests, ingests editorial sources (RSS and
best-of lists), and asks DeepSeek to pick from a pre-filtered candidate pool.
Everything is stored locally in SQLite; API keys stay in `.env`.

## Features

- **Steam sync** via the Steam Web API: owned games, total playtime, last played,
  plus a `playtime_snapshot` every time the playtime changes. Snapshots are the
  basis for later trend analysis.
- **Manual library** for console, Epic, GOG, physical games, etc. with hours,
  1–5 rating, status, notes and free-form tags.
- **Interests** with weights (−3…+3) and hard filters, plus free-text notes.
- **Editorial sources**: RSS feeds and configurable HTML best-of lists. Mentions
  are resolved against the game catalog; ambiguous titles go into a manual
  review queue.
- **Recommendations** in two modes: `backlog` (games you own but barely played)
  and `new` (new purchases). Candidates are scored locally (tag affinity,
  similarity to favourites, editorial signal, novelty), then DeepSeek reranks and
  explains the top ~30. The model can only choose IDs from the candidate list.
- **Feedback** (like / dismiss / played) feeds into the next taste profile.
  "Played" creates a manual library entry.
- **Stats**: hours per genre, monthly tracked playtime, top games, feedback rate.
- Local-only: binds to `127.0.0.1`, no login, no telemetry.

## Stack

- Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, SQLite (WAL)
- Jinja2 + HTMX + hand-written CSS (no Node, no build step)
- Worker process with a SQLite-backed job queue
- `httpx`, `feedparser`, BeautifulSoup, rapidfuzz
- DeepSeek via its OpenAI-compatible API

## Setup

```bash
python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env: STEAM_API_KEY, STEAM_ID, DEEPSEEK_API_KEY
./.venv/bin/alembic upgrade head
./start.sh
```

Open <http://127.0.0.1:8765>. `start.sh` runs migrations, seeds the default RSS
feeds, starts the worker and the web app. Stop with Ctrl+C.

For development you can run the two processes separately:

```bash
make serve   # auto-reload
make worker  # job queue
```

## Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `STEAM_API_KEY` | Steam Web API key |
| `STEAM_ID` | SteamID64 or vanity name (can also be set in the UI) |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `DEEPSEEK_BASE_URL` | default `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | default `deepseek-chat` |
| `APP_HOST` / `APP_PORT` | default `127.0.0.1:8765` |
| `DB_PATH` | default `data/app.db` |
| `TIMEZONE` | default `Europe/Berlin` |

`.env` is gitignored. `.env.example` documents the variables without values.

Your Steam profile must be public and **game details** must be visible; otherwise
`GetOwnedGames` returns an empty response. The app reports that explicitly.

## Usage

1. **Sync Steam** on the dashboard (or `python -m app.cli sync-steam`).
2. Add missing games and interests in the UI.
3. Optionally add best-of lists under **Quellen** and resolve pending mentions.
4. Start a recommendation round. The worker builds the profile, collects and
   scores candidates, calls DeepSeek, and stores the run with reasons.
5. Give feedback. It influences the next profile version.

CLI equivalents:

```bash
python -m app.cli sync-steam
python -m app.cli ingest
python -m app.cli profile
python -m app.cli recommend --mode backlog
python -m app.cli recommend --mode new --max-price 25
python -m app.cli resolve
python -m app.cli stats
python -m app.cli backup
```

## How recommendations work

1. Hard filters: platform, excluded tags, maximum price, already owned/dismissed.
2. Local scoring:
   - tag affinity from playtime- and rating-weighted history,
   - Jaccard similarity to your top games,
   - editorial signal (distinct sources, rank, recency decay),
   - novelty (penalty for the same series recently played),
   - popularity/review score as a weak tie-breaker.
3. Diversity caps: at most 3 per primary genre and 2 per series.
4. The top ~30 go to DeepSeek with a compact profile. The response is validated
   with Pydantic; unknown `candidate_id`s are discarded. On invalid JSON the call
   is retried once.
5. If DeepSeek is unavailable, the local ranking is stored as fallback with
   generated reasons. The run is marked `heuristic-fallback`.

The prompt is versioned (`prompt_version`), and every run stores model, token
usage, filters and the raw response for later comparison.

## Data sources and etiquette

- **Steam Web API**: owned games, player summary.
- **Steam Store** (`appdetails`, `storesearch`) and **SteamSpy**: metadata, tags,
  review summary. Responses are cached in `api_cache` (30 days).
- **RSS/best-of**: fetched with a descriptive User-Agent, `robots.txt` check and
  1 request/second per host. Only short excerpts and links are stored, not full
  articles.
- **DeepSeek**: receives the compact taste profile and candidate metadata (no
  SteamID, no persona name).

Respect the terms of service of the sources you add. This is a personal,
local-first tool.

## Project layout

```
app/
  main.py            FastAPI app + lifespan
  config.py          Settings from .env
  db.py              Engine, sessions, migrations
  models.py          SQLAlchemy models
  cli.py             CLI commands
  worker.py          Job loop
  jobs.py            Job handlers
  domain/            profile, candidates, recommendation orchestration
  sources/           steam, store, http, rss, editorial, resolve
  llm/               DeepSeek client, prompts, response schema
  web/               routes, Jinja templates, HTMX, CSS
migrations/          Alembic
tests/               pytest + respx
data/                SQLite DB, backups, caches (gitignored)
```

## Tests

```bash
./.venv/bin/pytest -q
./.venv/bin/ruff check .
```

HTTP calls are mocked with `respx`; tests never hit Steam or DeepSeek.

## Notes

- Snapshots only capture playtime **from the first sync onward**. Historical
  trends before that cannot be reconstructed from the Steam API.
- Entity resolution is heuristic. The review queue is intentional: confirm
  ambiguous titles instead of silently polluting the editorial signal.
- The app is not exposed to the network. Do not bind it to `0.0.0.0` without
  adding authentication.
