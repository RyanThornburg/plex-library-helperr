# Plex Library Helperr

Script to help automate/organize your Radarr/Sonarr movie library. Originally created for Radarr to move documentaries to their own library/folder and grew from there. It's configurable enough to handle a variety of sorting rules.

I used **Movers** for moving files to different library. I use **Profilers** primarly as a hack to prevent certain movies from being downloaded. Sort of a silent fail to prevent certain requests from ever being added. The profile I use for that basically means the movie will never reach the desired score and it auto unmonitors it.

**I suggest testing with `dry_run=true` until you're sure you like the results.**

## Features

- **Dry run mode**  -  preview what would change without touching anything
- **Time filter**  -  only evaluate media added within a recent window
- **Movers**  -  moves movies (radarr)/series (sonarr) to a different root folder based on genre, studio, network, certification, or a combination
- **Profilers**  -  assigns a quality profile to movies/series matched by title or studio

## Requirements

- Python 3.14+ (nothing specific to 3.14 that I know, just what I tested on)
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Setup

```bash
# Install dependencies
uv sync

# Run
uv run main.py
```

1. Before running copy `config.toml.example` to `config.toml`.
    - If you run `main.py` before doing this, it will create `config.toml` from `config.toml.example`.
1. Edit `config.toml` with your Radarr/Sonarr URL and API key

`config.toml.example` has examples for movers and profiles. You should delete or modify those before running on your library.

## Configuration

### `[settings]`

| Key           | Description                                                                                                           |
|-----          |-------------                                                                                                          |
| `dry_run`     | `true` to preview changes without applying them (default: `false`)                                                    |
| `filter_time` | Only evaluate movies added within this window. Format: `7d`, `12h`, `30m`. Comment out/delete to evaluate all movies. |

### `[plex]`

| Key       | Description                                                   |
|-----      |-------------                                                  |
| `baseurl` | URL to your Plex instance (e.g. `http://192.168.1.1:32400/`)  |
| `token`   | Plex token                                                    |

### `[radarr]`/`[sonarr]`

| Key       | Description                                                           |
|-----      |-------------                                                          |
| `baseurl` | URL to your Radarr/Sonarr instance (e.g. `http://192.168.1.1:7878/`)  |
| `apikey`  | Your API key                                                   |

### `[[radarr.movers]]`/`[[sonarr.movers]]`

Moves movies matching the rule to the specified root folder. Multiple rules are supported. At least one of `genres`, `tags`, `studios`, `networks`, or `certifications` is required.

| Key                      | Description                                                              |
|-----                     |-------------                                                             |
| `plex_library`           | Plex library to refresh (if plex config setup)                           |
| `path`                   | Root folder path to move matched movies into                             |
| `genres`                 | List of genres to match                                                  |
| `genre_match`            | `"any"` (at least one genre matches) or `"all"` (every genre must match) |
| `exclude_genres`         | Skip movies that have any of these genres                                |
| `tags`                   | List of tags to match                                                    |
| `tag_match`              | `"any"` (at least one tag matches) or `"all"` (every tag must match)     |
| `exclude_tags`           | Skip movies that have any of these tags                                  |
| `certifications`         | Only match movies with these ratings (e.g. `["G", "PG"]`)                |
| `exclude_certifications` | Skip movies with these ratings                                           |
| `exclude_paths`          | Skip movies already under these paths                                    |
| `studios`                | Only match movies from these studios **(Radarr only)**                   |
| `networks`               | Only match tv series from these networks **(Sonarr only)**               |

### `[[radarr.profilers]]`/`[[sonarr.profilers]]`

Assigns a quality profile to movies matched by title or studio/network.

| Key           | Description                                                               |
|-----          |-------------                                                              |
| `profile`     | Name of the Sonarr/Radarr quality profile to assign                       |
| `titles`      | List of movie/series titles to match                                      |
| `studios`     | List of studios to match **(Radarr only)**                                |
| `networks`    | List of networks to match **(Sonarr only)**                               |
| `monitored`   | Set the monitored state (`true`/`false`), or omit to leave unchanged      |

### Example config

See [`config/config.toml.example`](config/config.toml.example) for a full working example.

### Tests

Used claude to generate test coverage in `/tests`.

```bash
uv run pytest tests/ -v
```

## TODO

- [ ] Validation against movies that match more than one mover rule
- [ ] Command-line argument support
- [ ] could improve some of the looping / set checks

## TBD

- Batching? Currently using bulk endpoints and current use case isn't an issue
