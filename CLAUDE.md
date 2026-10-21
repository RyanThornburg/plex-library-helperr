# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Uses [uv](https://github.com/astral-sh/uv) and Python 3.14+.

```bash
uv sync                                   # install deps (incl. dev group: pytest)
uv run main.py                            # run movers/profilers
uv run main.py --remove                   # run sonarr removers only
uv run main.py --empty-trash              # empty plex library trash only
uv run main.py --serve                    # web app (needs CF_ACCESS_* and ALLOWED_EMAILS env)
uv run pytest tests/ -v                   # all tests
uv run pytest tests/test_main.py::TestRunArr -v   # single test class
```

No linter/formatter is configured in `pyproject.toml`.

## What it does

A one-shot script that reads every movie (Radarr) / series (Sonarr), applies user-defined rules from `config/config.toml`, and issues bulk edits back to the *arr API. Three rule types:

- **Movers** — change root folder (with `move_files=True`) for media matching genre/tag/studio/network/certification filters; optionally tag moved items, then scan the old and new folders via Autoscan (or, without Autoscan, refresh the target Plex library).
- **Profilers** — assign a quality profile (and optionally set `monitored`) for media matched by title/studio/network.
- **Removers** (Sonarr only, `--remove`) — unmonitor then delete stale seasons/episode files from tagged series, keeping the latest `keep` seasons/episodes. Runs instead of movers/profilers, never uses `filter_time`.
- **Empty trash** (`--empty-trash`) — empties Plex library trash (the server has auto-empty off). Movers/removers only trigger scans, so this is scheduled separately, after scans finish.

`dry_run` should be assumed on when testing against a real library.

## Architecture

- `results.py` — `JobResult` (the `Change`s a job made, `scan_dirs`/`plex_libraries` left to scan, and `errors` that stopped part of the job) is passed into `run_arr`, `run_removers` and `empty_plex_trash` and filled as they go, so changes made before an exception are kept. A `Change` is one bulk edit / series removal / library trash / scan with a status: `done`, `dry_run`, `blocked` (a safety check such as `max_deletes`, `max_items` or a scan in progress) or `failed`. Results carry what the future API/UI acts on; why media was skipped stays in the log. Never put URLs or secrets in a `Change`, results will be stored and shown in the UI.
- `main.py` — all rule-matching logic, `run_job(job, settings) -> JobResult` (jobs: `movers` = movers and profilers, `removers`, `empty_trash`), which the API will call, and `main()`, which parses args, runs one job and logs its summary. Run as a script it exits 1 when the result has errors or failed changes. Radarr and Sonarr share one code path via the `arr_dispatch` table, which maps each to its arrapi class and method names (`all_movies`/`all_series`, `edit_multiple_movies`/`edit_multiple_series`) called through `getattr`. Per-instance work lives in `run_arr()`; `run_job()` catches `ArrException` per instance so one failure doesn't stop the other. When adding *arr-specific behavior, branch on `log_name` ("RADARR"/"SONARR") or extend that tuple rather than duplicating the loop.
- Matching functions (`get_matches_based_on_mover_rules`, `get_media_matching_profile_rules`) are pure: they take media lists and return the matching `Movie`/`Series` objects, which are passed directly to `edit_multiple_*`; all API side effects stay in `run_arr()`. Tests exercise these functions with `MagicMock` media objects (see helpers at the top of `tests/test_main.py`).
- Mover evaluation order: skip media already under `path` or any `exclude_paths` → apply excludes (tags, genres, certifications) → require each configured filter (studio, network, certification, genre with `genre_match`, tag with `tag_match`). All comparisons are `casefold()`ed. Movers run in config order and media matched by an earlier mover is skipped by later ones (first match wins).
- `config/__init__.py` — `load_config()` parses TOML into dataclasses in `config/models.py`. Remover durations (`min_age`, `skip_if_watched_within`) are parsed strictly and raise on a bad value, unlike `filter_time`, so a typo can never mean "delete now". If `config/config.toml` is missing it is copied from `config.toml.example`. `[settings].dry_run` is the default for each *arr section, which may override it with its own `dry_run`. `filter_time` (`7d`/`12h`/`30m`) becomes a `timedelta`; media is filtered by `added` against naive UTC now.
- `config/models.py` — dataclasses validate in `__post_init__` (movers/profilers need at least one matching criterion; `*_match` must be `any`/`all`). `_validate_rules` in `config/__init__.py` rejects `networks` on Radarr and `studios` on Sonarr rules. Custom `__repr__`s omit empty fields and mask `apikey`/`token` because the full config is logged at startup — keep secrets masked when adding fields.
- `remover.py` — `run_removers()` plans every series first, then acts. arrapi has no episode endpoints, so `SonarrEpisodes` calls `/api/v3` directly with `requests` (episodes, episodefile, queue/details, `PUT episode/monitor`, `DELETE episodefile/bulk`). `plan_series_removal()` is pure and is where the rules live:
  - Candidates are everything outside the keep window. Only numbered seasons/episodes **with files** count toward `keep`; specials (season 0) never count and are only removable with `include_specials`.
  - `seasons` mode is all or nothing: a season is removed only if every file's `episodeFile.dateAdded` is older than `min_age`. `episodes` mode works per file; a multi-episode file holding any kept episode is kept.
  - Watch protection (`skip_if_watched_within`) only counts recent plays of removal *candidates* (watching the latest episodes never blocks) and protects from the earliest watched candidate onward. It runs before the age filter, so watching a new season still protects older seasons after it. A watched special protects only specials.
  - Series-level skips: `series.added` newer than `min_age`, anything in the download queue, or invalid/missing Sonarr data (`InvalidSeriesData`). Bad data is logged and skipped, never guessed at.
  - A series tagged for two removers is logged as an error and skipped; a tag shared between removers is rejected at config load.
  - `remover_settings.dry_run` defaults to `true` independently of the global `dry_run`. `max_deletes` is all-or-nothing for the run. Episodes are unmonitored before files are deleted; if unmonitoring fails the delete is skipped.
- `watch_history.py` — `WatchEvent`s (naive UTC) from Tautulli `get_history` (includes partial plays and all users, show TVDB id from `get_metadata` guids), falling back to Plex server history (completed plays only). Returns `None` on failure, and removers with `skip_if_watched_within` are then skipped. Events are matched to series by TVDB id, and by title only when the history has no TVDB id. Tautulli's `apikey` is a query param, so `requests` error text contains it: redact it before logging or storing any Tautulli error.
- `autoscan.py` — POSTs the old and new folders of moved media to Autoscan's `/triggers/manual`. Paths are sent as the *arr sees them; the rewrite on Autoscan's Plex target maps them to Plex paths and its `minimum-age` covers the async move.
- `plex.py` — fallback when Autoscan isn't configured: connects via plexapi and refreshes each library section collected from executed (non-dry-run) movers. Also `empty_plex_trash()` for `--empty-trash`: counts each movie/show section's trash via `/library/sections/{key}/all?type=<movie|episode>&trash=1` (includes old versions left by upgrades) and skips a section that is `refreshing` or over `[plex.empty_trash].max_items`, which guards against a missing mount trashing a whole library. Its `dry_run` defaults to `[settings].dry_run`.
- `web/` — FastAPI app run by `main.py --serve` (one uvicorn worker on purpose: the scheduler will live in-process, no Redis). `create_app(settings, keys)` is a factory; tests pass `keys` to replace the team's signing keys.
  - `settings.py` reads `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` and `ALLOWED_EMAILS` and refuses to start if any is missing or malformed, so auth can never be half configured.
  - `auth.py` is pure ASGI middleware that wraps every route (static files and 404s too) except `PUBLIC_PATHS` (`/healthz`). It verifies `Cf-Access-Jwt-Assertion` (RS256 only, keys from `https://<team>/cdn-cgi/access/certs`, issuer, audience, exp), then the email against `ALLOWED_EMAILS`. Denial reasons are logged, never returned. Fails closed, including when Cloudflare's keys can't be fetched.
  - `security.py` adds a strict CSP (no inline scripts/styles, no third-party content, so self-host any JS such as htmx) and other headers, and rejects non-GET requests whose `Origin` doesn't match `Host` (CSRF). It runs before auth.
  - `db.py` uses SQLite at `$DATA_DIR/helperr.db` (WAL, foreign keys, busy timeout). Schema changes go through Alembic migrations in `web/migrations/versions`, applied at startup. Generate new ones with autogenerate against a scratch db, then tidy them.
  - Templates link assets root-relative (`/static/...`). Never use `url_for` for URLs: behind cloudflared the app sees `http`, so absolute URLs would be mixed content.
  - No interactive API docs (`/docs`, `/openapi.json`); they would be one more surface to protect.
- `logging_config.py` — dictConfig with a Rich console handler (`markup=False`, so titles with brackets render literally) and a rotating plain-text file at `logs/log.log`. Log lines are prefixed with `log_name` and a tab.

## Notes

- Code uses Python 3.14 syntax, e.g. unparenthesized multi-exception `except A, B:` in `plex.py` (PEP 758) — this is intentional, not a bug.
- `main()` takes `argv` (tests call `main.main([])` so pytest's argv isn't parsed).
- `config/config.toml` holds real credentials and is gitignored; edit `config.toml.example` for documented defaults and keep the README config tables in sync when adding rule fields.
- `DATA_DIR` (set to `/config` in the Dockerfile) moves `config.toml` and `logs/` out of the code dirs; unset, they stay at `config/config.toml` and `logs/`. Docker can't mount over `/app/config` because that's the config Python package.
- `.github/workflows/docker.yml` runs the tests, then builds the image; pushes to `mainline` publish `ghcr.io/ryanthornburg/plex-library-helperr` (`latest` and `sha-<commit>`), which Unraid pulls. Actions are pinned to commit SHAs (with the version in a comment) and Dependabot updates them along with uv and the base images.
- `.dockerignore` is an allowlist so `config/config.toml` never reaches the build context. When adding a module outside the top level or `config/`, add it there and to the Dockerfile's `COPY` lines.
