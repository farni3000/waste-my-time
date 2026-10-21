# AGENTS.md

Local Python 3.12 app. Read `README.md` for features and setup.

## Commands

```bash
./.venv/bin/pytest -q          # tests (mocked HTTP)
./.venv/bin/ruff check .       # lint
./.venv/bin/alembic upgrade head
./start.sh                     # worker + web on 127.0.0.1:8765
./.venv/bin/python -m app.cli sync-steam
./.venv/bin/python -m app.cli ingest
./.venv/bin/python -m app.cli recommend --mode backlog
```

## Structure

- `app/models.py` – single source of truth for schema; after changes run
  `alembic revision --autogenerate -m "..."` and `alembic upgrade head`.
- `app/domain/` – profile aggregation, candidate generation/scoring,
  recommendation orchestration. Keep I/O out of pure scoring helpers.
- `app/sources/` – adapters. `store.py` owns caching for Steam Store/SteamSpy/
  reviews. `resolve.py` owns title normalization matching and the mention queue.
- `app/llm/` – DeepSeek client. Only IDs from the candidate list are accepted.
- `app/web/routes.py` – all HTML/HTMX routes; templates in `app/web/templates`.
- `app/jobs.py` + `app/worker.py` – SQLite-backed queue; long work never runs in
  a request handler.

## Conventions

- UTC naive timestamps via `app.timeutil.utcnow()`; format with the `dt` filter.
- Keys only from `app.config.Settings` (`pydantic-settings`, `.env`). Never log
  secrets or send them to templates.
- HTML is server-rendered with Jinja + HTMX. No Node build step.
- All external HTTP must go through `httpx` with timeouts and, for scraped
  sources, `app/sources/http.py` (robots + 1 req/s per host).
- Store API responses must be cached in `api_cache`; do not add uncached calls to
  request handlers.
- Recommendation output must be validated via `app.llm.schemas`; unknown IDs are
  dropped. Keep `PROMPT_VERSION` in sync with prompt changes.
- Schema changes require a migration. Tests use a separate `data/test.db`
  (set in `tests/conftest.py`) and `Base.metadata.create_all` is only a test/
  fallback helper.

## Gotchas

- `GetOwnedGames` returns `{}` for private profiles; surface a clear error.
- Steam Store and SteamSpy are rate-limited; `enrich_game` is called sparingly
  and cached for 30 days.
- Candidate diversity is capped per genre/series in `domain/candidates.py`; do
  not remove those caps without checking recommendation quality.
- `game_form.html` must not nest `<form>` elements (HTMX + plain POST mix).
- The job panel polls `/jobs/{id}/panel`; when done it responds with
  `HX-Redirect` to the relevant page.
