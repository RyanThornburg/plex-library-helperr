# Contributing

This is a personal project built around my own setup, but bug reports, ideas and questions are welcome.

## Issues

Open an [issue](https://github.com/RyanThornburg/plex-library-helperr/issues) with your version (bottom of the sidebar) and what you expected to happen. The [Troubleshooting](https://github.com/RyanThornburg/plex-library-helperr/wiki/Troubleshooting) page may already have the answer.

Before you paste logs or settings, check them for API keys, tokens and URLs you don't want public. The app redacts the keys it knows about, but not everything.

## Pull requests

Welcome too. For anything bigger than a small fix, open an issue first so we can agree on the approach. Please make sure the tests pass.

## Development

Needs Python 3.14 and [uv](https://github.com/astral-sh/uv).

```bash
uv sync                          # install dependencies
uv run pytest tests/ -v          # tests
HELPERR_SECRET_KEY=... uv run main.py --serve   # the web app on :8080
docker build -t plex-library-helperr .
```

`DATA_DIR` keeps `helperr.db` and `logs/` somewhere other than the repo. The [Command Line](https://github.com/RyanThornburg/plex-library-helperr/wiki/Command-Line) page has the other jobs `main.py` can run.

New settings go in the config document and a settings form; add them to the wiki's [Settings Reference](https://github.com/RyanThornburg/plex-library-helperr/wiki/Settings-Reference) too.

## Releases

1. Set the new version in `pyproject.toml` and run `uv lock`.
2. Add a `## 2.1.0 (date)` section to the top of `CHANGELOG.md`.
3. Commit, tag, and push the commit and the tag together: `git tag -a v2.1.0 -m v2.1.0 && git push origin mainline v2.1.0`. Pushing them at once means the `edge` build reads 2.1.0 too.

CI checks the tag matches `pyproject.toml`, publishes the image tags and makes a GitHub release from the changelog section.
