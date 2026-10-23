# Plex Library Helperr

Script to help automate/organize your Radarr/Sonarr movie library. Originally created for Radarr to move documentaries to their own library/folder and grew from there. It's configurable enough to handle a variety of sorting rules.

I used **Movers** for moving files to different library. I use **Profilers** primarly as a hack to prevent certain movies from being downloaded. Sort of a silent fail to prevent certain requests from ever being added. The profile I use for that basically means the movie will never reach the desired score and it auto unmonitors it.

**I suggest testing with `dry_run=true` until you're sure you like the results.**

## Features

- **Dry run mode**  -  preview what would change without touching anything
- **Time filter**  -  only evaluate media added within a recent window
- **Movers**  -  moves movies (radarr)/series (sonarr) to a different root folder based on genre, studio, network, certification, or a combination
- **Profilers**  -  assigns a quality profile to movies/series matched by title or studio/network
- **Removers**  -  (Sonarr only) unmonitors and deletes old seasons/episodes from tagged series, keeping the latest ones
- **Empty trash**  -  empties Plex library trash on demand, skipping libraries that are scanning or have a suspiciously full trash

**Removers delete files.** They default to `dry_run = true` on their own. Check the `--remove` log before setting `[sonarr.remover_settings] dry_run = false`.

## Requirements

- Python 3.14+ (nothing specific to 3.14 that I know, just what I tested on)
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Setup

```bash
# Install dependencies
uv sync

# Run movers and profilers
uv run main.py

# Run Sonarr removers only (movers and profilers are skipped)
uv run main.py --remove

# Empty Plex library trash only
uv run main.py --empty-trash
```

Each run ends with a summary of what it changed (or would change in a dry run) and exits with status 1 if anything failed.

`--remove` is a separate run so you can schedule it on its own, less often than the movers. It ignores `filter_time` and has its own `dry_run` setting in `[sonarr.remover_settings]`.

`--empty-trash` is for Plex servers with "Empty trash automatically after every scan" turned off. Removers and movers only trigger a scan, which moves deleted files to the trash. Schedule `--empty-trash` to run after those scans have finished (with Autoscan, the scan happens after its `minimum-age`).

1. Before running copy `config.toml.example` to `config.toml`.
    - If you run `main.py` before doing this, it will create `config.toml` from `config.toml.example`.
1. Edit `config.toml` with your Radarr/Sonarr URL and API key

`config.toml.example` has examples for movers and profiles. You should delete or modify those before running on your library.

## Docker (Unraid)

The image runs one job and exits. Config and logs live in the `/config` volume (`config.toml` and `logs/`), and the container runs as Unraid's `nobody:users` (99:100).

Every push to `mainline` runs the tests and publishes `ghcr.io/ryanthornburg/plex-library-helperr:latest` (plus a `sha-<commit>` tag to pin or roll back to).

### Docker tab

Add Container, one per job:

| Field | Value |
|---|---|
| Repository | `ghcr.io/ryanthornburg/plex-library-helperr:latest` |
| Network Type | The custom network your *arr containers are on, if `config.toml` uses container names (see below) |
| Extra Parameters | `--read-only --cap-drop ALL --security-opt no-new-privileges` |
| Post Arguments | Empty for movers/profilers, `--remove`, or `--empty-trash` |
| Path | Container path `/config`, host path `/mnt/user/appdata/plex-library-helperr/config` |
| Variable (optional) | `TZ` = your zone, e.g. `America/New_York`, for local log timestamps |

- **Apply starts the container right away**, so set `dry_run = true` in `config.toml` first. With an empty `/config`, the first run creates `config.toml` from the example, which has no server URLs, so that run changes nothing.
- **Turn Autostart off** on the Docker tab, or the job runs every time the array starts.
- Run a job with **Start**; the container stops when the job finishes. Its log icon shows the output, and `logs/log.log` in the config folder keeps it.
- Updates show on the Docker tab like any other container (or let the CA Auto Update plugin apply them).
- If the run fails with `Permission denied: '/config/logs'`, the container user can't write the config folder: `chown 99:100 /mnt/user/appdata/plex-library-helperr/config`.

URLs in `config.toml` can't use `localhost`, which inside the container is the container itself. The server's IP works as it would from any other machine. Container names (e.g. `http://sonarr:8989`) work when this container is on the same custom network as those containers. Plex in host network mode has no container name to use, so give it the server's IP.

### User Script (alternative)

For running jobs from a schedule without the web app (with the web app, use its Schedules page instead):

```bash
#!/bin/bash
IMAGE=ghcr.io/ryanthornburg/plex-library-helperr:latest
# set only when config.toml uses container names
NETWORK_ARGS=()  # e.g. NETWORK_ARGS=(--network my-docker-network)
docker pull -q "$IMAGE"
docker run --rm "${NETWORK_ARGS[@]}" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  -v /mnt/user/appdata/plex-library-helperr/config:/config \
  "$IMAGE"            # add --remove or --empty-trash for those jobs
docker image prune -f --filter "label=org.opencontainers.image.title=plex-library-helperr"
```

### Other

- To build locally: `docker build -t plex-library-helperr .` in a clone of this repo.
- Outside Docker, set `DATA_DIR` to keep `config.toml` and `logs/` somewhere other than the repo.

## Web app (in progress)

`--serve` runs a web app for running and reviewing jobs:

- Each job has a **Dry run** button, which forces every `dry_run` setting on, and a **Run…** button, which uses `config.toml` as-is. Run… always goes to a confirmation page that says whether the run will make changes. The UI can't make a run live that `config.toml` has as dry run, with one exception, approved remover plans:
- **Remover plans:** every removers run saves its plan, the seasons/episodes it would remove from each series with their file paths. On a dry run's page, select series and review: the confirmation page shows how many files will be deleted, then an apply run removes them, even with `remover_settings.dry_run = true` (that setting keeps removers from deleting on their own; approving is the decision to delete). The apply run plans each approved series again from fresh Sonarr data and removes only seasons/episodes whose files were all approved and are still eligible: anything watched, replaced, downloading or no longer outside the keep window since is skipped and reported. `max_deletes` still applies. A plan can be applied once, within 7 days. Applying is off unless the container has `REMOVERS_ALLOW_APPLY=true`.
- **Auto-approve:** the Removers page lists the remover rules in `config.toml`, each with an auto-approve switch (off by default, turning it on goes through a confirmation page). A removers run that uses `config.toml`'s settings (Run…, or a schedule without "Force dry run") deletes an auto-approved rule's removals without review, even with `remover_settings.dry_run = true`; other rules' removals are planned for review. Forced dry runs never delete, `max_deletes` applies, and it needs `REMOVERS_ALLOW_APPLY=true` like applying a plan. The switch is stored in the app's database by the rule's tags, so changing a rule's tags turns it back off.
- Jobs run one at a time in the order queued. A job that's already queued or running can't be queued again.
- Every run is kept with who started it, what it changed (or would change), errors and its log. Runs interrupted by a restart are marked failed and never re-run automatically.
- **Schedules** run a job on a cron expression (`minute hour day month weekday`, or `@hourly`/`@daily`/`@weekly`/`@monthly`) in the schedule's own time zone, which a new schedule takes from your browser. They're created and edited on the Schedules page and stored in the app's database, not `config.toml`. Like the buttons, a schedule either forces dry run (the default) or uses `config.toml`'s settings. A schedule whose job is still queued or running skips that time, and times missed while the app was down are skipped rather than caught up.
- The same actions are available as JSON: `POST /api/jobs/<movers|removers|empty_trash>` (body `{"dry_run": false}` for a config run, dry by default), `GET /api/runs`, `GET /api/runs/<id>`, and `GET`/`POST /api/schedules`, `GET`/`PUT`/`DELETE /api/schedules/<id>` (body `{"name", "job", "cron", "timezone": "UTC", "dry_run": true, "enabled": true}`), `POST /api/runs/<id>/apply` (body `{"series_ids": [...]}`) to apply a removers plan, and `GET /api/removers`, `POST /api/removers/auto-approve` (body `{"rule": "<tags, lowercased, sorted, comma joined>", "enabled": true}`).
- Times on the pages are shown in your browser's time zone, so the container needs no `TZ`.

The web app logs to `logs/web.log`, separate from the CLI's `logs/log.log`.

 It only accepts requests that come through [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/): every route except `/healthz` requires a valid Access token, checked against your team's signing keys, the application's audience tag and an email allowlist. It refuses to start without those settings.

It is reached through a Cloudflare Tunnel, so the container publishes **no port**. Nothing on your network or router points at it.

### Cloudflare setup

1. **Tunnel:** Zero Trust → Networks → Tunnels → Create a tunnel (Cloudflared), named e.g. `helperr`. Choose Docker and copy the token from the command it shows.
2. **cloudflared container** (Unraid Docker tab):

    | Field | Value |
    |---|---|
    | Repository | `cloudflare/cloudflared:2026.10.1` (pin a version, update it deliberately) |
    | Network Type | `rattproxy` (the same network as the web app) |
    | Post Arguments | `tunnel --no-autoupdate run` |
    | Variable | `TUNNEL_TOKEN` = the token from step 1 |

3. **Public hostname** on the tunnel: e.g. `helperr.<your domain>` → service `http://plex-library-helperr:8080`.
4. **Access application:** Zero Trust → Access → Applications → Add → Self-hosted, for the same hostname. Add an Allow policy that includes only your email. Copy the application's **Audience (AUD) tag** from its overview.

### Web container

| Field | Value |
|---|---|
| Name | `plex-library-helperr` (the tunnel's public hostname points at this name) |
| Repository | `ghcr.io/ryanthornburg/plex-library-helperr:latest` |
| Network Type | `rattproxy` |
| Extra Parameters | `--read-only --cap-drop ALL --security-opt no-new-privileges` |
| Post Arguments | `--serve` |
| Path | `/config` → `/mnt/user/appdata/plex-library-helperr/config` |
| Ports | none |
| Variable `CF_ACCESS_TEAM_DOMAIN` | `<team>.cloudflareaccess.com` |
| Variable `CF_ACCESS_AUD` | the AUD tag from step 4 |
| Variable `ALLOWED_EMAILS` | your Access email (comma separate more than one) |
| Variable `REMOVERS_ALLOW_APPLY` | `true` to allow applying approved remover plans from the UI (default off). An environment variable so nothing in the app can turn it on |

The database is `helperr.db` in `/config`, next to `config.toml`. Keep `/config` on a cache pool (not the array), since SQLite and Unraid's FUSE layer don't mix.

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

### `[plex.empty_trash]` (optional)

Only used by `--empty-trash`. A library is skipped while it is scanning, and when its trash holds more than `max_items` items, since a missing mount or share makes Plex trash everything in it. Items are counted as movies or episodes with a deleted file, which includes old versions left behind by upgrades.

| Key         | Description                                                                                    |
|-----        |-------------                                                                                   |
| `libraries` | Plex libraries to empty the trash of. Omit for every movie and TV library.                     |
| `max_items` | Skip a library if its trash holds more items than this (default: `100`)                        |
| `dry_run`   | `true` to log trash counts without emptying (default: `[settings]` `dry_run`)                  |

### `[tautulli]` (optional)

Watch history for remover watch protection (`skip_if_watched_within`). Tautulli is preferred because it records partial plays from every user. Without it, Plex server history is used, which only records completed plays. If neither is configured or the history can't be read, removers that use `skip_if_watched_within` are skipped for that run.

| Key      | Description                                                   |
|-----     |-------------                                                  |
| `url`    | URL to your Tautulli instance (e.g. `http://192.168.1.1:8181`) |
| `apikey` | Tautulli API key                                              |

### `[autoscan]` (optional)

When `url` is set, each moved title's old and new folder is sent to [Autoscan](https://github.com/Cloudbox/autoscan)'s manual trigger instead of refreshing whole Plex libraries. Autoscan's `minimum-age` gives Radarr/Sonarr time to finish moving files, and scanning the old folder removes stale entries from the source library.

| Key        | Description                                                  |
|-----       |-------------                                                 |
| `url`      | URL to your Autoscan instance (e.g. `http://192.168.1.1:3030`) |
| `username` | Autoscan basic auth username (optional)                      |
| `password` | Autoscan basic auth password (optional)                      |

Paths are sent as Radarr/Sonarr see them, the same as Autoscan's own Sonarr/Radarr triggers. If Plex uses different paths, the rewrite on your Autoscan Plex target handles it (no manual trigger rewrite needed):

```yaml
targets:
  plex:
    - url: http://192.168.1.1:32400
      token: xxxxx
      rewrite:
        - from: /data/media/
          to: /mnt/nfs/
```

### `[radarr]`/`[sonarr]`

| Key       | Description                                                           |
|-----      |-------------                                                          |
| `baseurl` | URL to your Radarr/Sonarr instance (e.g. `http://192.168.1.1:7878/`)  |
| `apikey`  | Your API key                                                          |
| `tag`     | Adds a tag identifing any files moved by this script                  |

### `[[radarr.movers]]`/`[[sonarr.movers]]`

Moves movies matching the rule to the specified root folder. Multiple rules are supported and are checked in order. If media matches more than one rule, the first matching rule wins. At least one of `genres`, `tags`, `studios`, `networks`, or `certifications` is required.

| Key                      | Description                                                              |
|-----                     |-------------                                                             |
| `plex_library`           | Plex library to refresh (if plex config setup and autoscan is not)       |
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

### `[sonarr.remover_settings]`

Only used by `--remove`.

| Key           | Description                                                                                                                |
|-----          |-------------                                                                                                               |
| `dry_run`     | `true` to log what would be removed without removing it (default: `true`). Separate from `[settings]` and `[sonarr]` `dry_run`. |
| `max_deletes` | If a run would delete more files than this, nothing is removed. Removals only planned for review don't count. Omit for no limit. |

### `[[sonarr.removers]]`

Unmonitors and then deletes the files of old seasons or episodes in series that have one of the remover's `tags`. Episodes are unmonitored first so Sonarr doesn't download them again. If unmonitoring fails, the files aren't deleted. Every series is planned before anything is removed. `max_deletes` applies to the whole run, so if the files it would delete are over the limit nothing is removed.

| Key                      | Description                                                                                                  |
|-----                     |-------------                                                                                                 |
| `type`                   | `"seasons"` removes whole seasons, `"episodes"` removes individual episode files                             |
| `keep`                   | Number of latest seasons/episodes to always keep (integer >= 1). Only numbered seasons/episodes with files count. |
| `tags`                   | Series with any of these Sonarr tags use this remover. A tag can only belong to one remover.                 |
| `exclude_tags`           | Skip series with any of these tags                                                                           |
| `min_age`                | **Required.** Only remove files added to Sonarr longer ago than this (`180d`, `12h`, `30m`). An invalid value is a config error. |
| `include_specials`       | `true` to also remove specials (season 0). Specials never count toward `keep` (default: `false`)             |
| `skip_if_watched_within` | Protect content that was watched within this window (`30d`, ...). Needs `[tautulli]` or `[plex]`.            |
| `plex_library`           | Plex library to refresh after removing (if Plex is configured and Autoscan is not)                           |

How rules are applied:

- Everything outside the `keep` window is a candidate. Seasons/episodes without files don't count toward `keep`.
- `seasons` mode is all or nothing: a season is removed only if **every** file in it is older than `min_age`.
- `episodes` mode works per file. A multi-episode file that holds a kept episode is kept.
- `skip_if_watched_within` only looks at plays of removal candidates, so watching the latest episodes never blocks removal. A recent play protects that season/episode and everything after it. It is checked before `min_age`, so someone watching a new season still protects the older seasons after it. A watched special protects only specials.
- A whole series is skipped if it was added to Sonarr within `min_age`, has anything in the download queue, or Sonarr returns missing/invalid episode data.
- A series tagged for two removers is logged as an error and skipped.
- After removing, the folders are scanned by Autoscan, or `plex_library` is refreshed if Autoscan isn't configured.

Example: `type = "seasons"`, `keep = 2`, `min_age = "90d"` on a series with seasons 1-5 removes seasons 1-3 once all their files are older than 90 days.

### Example config

See [`config/config.toml.example`](config/config.toml.example) for a full working example.

### Tests

Used claude to generate test coverage in `/tests`.

```bash
uv run pytest tests/ -v
```

## TODO

- [ ] Command-line argument support
- [ ] could improve some of the looping / set checks

## TBD

- Batching? Currently using bulk endpoints and current use case isn't an issue
