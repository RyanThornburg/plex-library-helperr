# Changelog

What changed in each release. Versions are `MAJOR.MINOR.PATCH`: the major number goes up when upgrading needs you to do something (a new required setting, a removed feature, a change that breaks the API), the minor number for new features, the patch number for fixes.

## 2.2.0 (2026-11-13)

### New

- Duplicate versions (Runs, Settings → Duplicates): finds movies and episodes with more than one version in Plex and plans deleting every version that isn't the file Radarr or Sonarr tracks, for review like removers (approve, then a confirmation naming the files). It skips anything it can't be sure of: a file added in the last few days, a download in progress, a tracked file Plex hasn't scanned, a version much longer or shorter than the tracked one, or a file holding several episodes. Approved versions are deleted through Plex, which needs Allow media deletion on. Applying needs `REMOVERS_ALLOW_APPLY=true` and stops at `max_deletes`.

## 2.1.0 (2026-11-13)

### New

- Unraid on the Status page (Settings → Connections → Unraid, with a read-only Viewer API key, Unraid 7.2 or later). The array, each disk's state, read/write errors, temperature and fill level against your Unraid thresholds, the running or last parity check, containers set to autostart that aren't running, and Unraid's unread warnings and alerts. Each disk's problems are their own incidents in the health history, and a disabled or missing disk puts the card at the top of the page as an error.
- Stuck downloads can be removed automatically (Settings → Stuck downloads), separately for Radarr and Sonarr. Pick how long a download has to be stuck, which kinds count (failed, import blocked, import pending), whether to remove it from the download client, whether to blocklist the release, and a max per run. It starts off and in dry run. The health check runs it when a download comes due, and it waits while the arr reports a missing root folder, mount or download client. The queue pages say when each download will go.

### Changed

- Helperr is licensed GPL-3.0-or-later, and the license ships in the image.

### Fixed

- The version at the bottom of the sidebar stays under the menu instead of scrolling with the page.

## 2.0.1 (2026-11-10)

### Fixed

- Playback: for plays where only the audio was converted, the advice now points at the Plex app's audio passthrough setting. Passthrough on only helps when a receiver or soundbar takes the format. With it off, a player that can decode the format plays it itself. The old text suggested turning passthrough on or adding an AC3/AAC track.

## 2.0.0 (2026-11-10)

Version 2 is a web app. Version 1 was a command line script that read `config.toml`. It isn't maintained anymore: its last commit is tagged `v1`, and its image is `ghcr.io/ryanthornburg/plex-library-helperr:sha-b421917`.

### Upgrading from 1.x

- `config.toml` isn't read anymore. Settings live in the app's database and are set up on its Settings pages, so start the web app and enter your connections and rules there.
- The web app needs `HELPERR_SECRET_KEY` (32 or more random characters). It encrypts the API keys in the database.
- The app always needs a sign-in: a username and password (the default), or Cloudflare Access with `AUTH_MODE=cloudflare`.
- The command line jobs still work, and read the same database as the web app.
- Image tags: `:latest` is now the newest release, `:2` follows 2.x releases, and `:edge` is every push to `mainline`.

### New

- Status page for Plex (through Tautulli), Radarr, Sonarr, SABnzbd, Bazarr and Seerr, with health checks every 5 minutes, a year of incident history and removing stuck downloads.
- Run any job from the app, with dry run, or on a cron schedule. Every run is kept with what it changed and its log.
- Removers plan what they'd delete, and you review and approve the plan. Auto-approve can be turned on per rule. Nothing is deleted unless `REMOVERS_ALLOW_APPLY=true`.
- Radarr removers, for old movies in a root folder, with watch checks through Tautulli.
- Empty trash runs on a schedule, and again a little while after movers, removers or upgrades changed files.
- Radarr and Sonarr webhooks run movers as soon as something is added.
- Requests page: who requested what in Seerr, and how much of it they watched.
- Playback page: how each play was streamed, why it transcoded, and what to change on which device.
- Settings in the app, with a Test button for connections, a Preview for rules, and a history of every change you can compare and restore.
- Events and log files, viewable and downloadable from the app.
- A JSON API for most pages.
- `--reset-login` for a forgotten password.
