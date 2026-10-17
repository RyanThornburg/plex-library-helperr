# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Uses [uv](https://github.com/astral-sh/uv) and Python 3.14+.

```bash
uv sync                                   # install deps (incl. dev group: pytest)
uv run main.py                            # run the script
uv run pytest tests/ -v                   # all tests
uv run pytest tests/test_main.py::TestRunArr -v   # single test class
```

No linter/formatter is configured in `pyproject.toml`.

## What it does

A one-shot script that reads every movie (Radarr) / series (Sonarr), applies user-defined rules from `config/config.toml`, and issues bulk edits back to the *arr API. Two rule types:

- **Movers** — change root folder (with `move_files=True`) for media matching genre/tag/studio/network/certification filters; optionally tag moved items, then scan the old and new folders via Autoscan (or, without Autoscan, refresh the target Plex library).
- **Profilers** — assign a quality profile (and optionally set `monitored`) for media matched by title/studio/network.

`dry_run` should be assumed on when testing against a real library.

## Architecture

- `main.py` — all rule-matching logic plus `main()`. Radarr and Sonarr share one code path via the `arr_dispatch` table, which maps each to its arrapi class and method names (`all_movies`/`all_series`, `edit_multiple_movies`/`edit_multiple_series`) called through `getattr`. Per-instance work lives in `run_arr()`; `main()` catches `ArrException` per instance so one failure doesn't stop the other. When adding *arr-specific behavior, branch on `log_name` ("RADARR"/"SONARR") or extend that tuple rather than duplicating the loop.
- Matching functions (`get_matches_based_on_mover_rules`, `get_media_matching_profile_rules`) are pure: they take media lists and return the matching `Movie`/`Series` objects, which are passed directly to `edit_multiple_*`; all API side effects stay in `run_arr()`. Tests exercise these functions with `MagicMock` media objects (see helpers at the top of `tests/test_main.py`).
- Mover evaluation order: skip media already under `path` or any `exclude_paths` → apply excludes (tags, genres, certifications) → require each configured filter (studio, network, certification, genre with `genre_match`, tag with `tag_match`). All comparisons are `casefold()`ed. Movers run in config order and media matched by an earlier mover is skipped by later ones (first match wins).
- `config/__init__.py` — `load_config()` parses TOML into dataclasses in `config/models.py`. If `config/config.toml` is missing it is copied from `config.toml.example`. `[settings].dry_run` is the default for each *arr section, which may override it with its own `dry_run`. `filter_time` (`7d`/`12h`/`30m`) becomes a `timedelta`; media is filtered by `added` against naive UTC now.
- `config/models.py` — dataclasses validate in `__post_init__` (movers/profilers need at least one matching criterion; `*_match` must be `any`/`all`). `_validate_rules` in `config/__init__.py` rejects `networks` on Radarr and `studios` on Sonarr rules. Custom `__repr__`s omit empty fields and mask `apikey`/`token` because the full config is logged at startup — keep secrets masked when adding fields.
- `autoscan.py` — POSTs the old and new folders of moved media to Autoscan's `/triggers/manual`. Paths are sent as the *arr sees them; the rewrite on Autoscan's Plex target maps them to Plex paths and its `minimum-age` covers the async move.
- `plex.py` — fallback when Autoscan isn't configured: connects via plexapi and refreshes each library section collected from executed (non-dry-run) movers.
- `logging_config.py` — dictConfig with a Rich console handler (`markup=False`, so titles with brackets render literally) and a rotating plain-text file at `logs/log.log`. Log lines are prefixed with `log_name` and a tab.

## Notes

- Code uses Python 3.14 syntax, e.g. unparenthesized multi-exception `except A, B:` in `plex.py` (PEP 758) — this is intentional, not a bug.
- `config/config.toml` holds real credentials and is gitignored; edit `config.toml.example` for documented defaults and keep the README config tables in sync when adding rule fields.
