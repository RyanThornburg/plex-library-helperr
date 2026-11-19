# Changelog

What changed in each release. Versions are `MAJOR.MINOR.PATCH`: the major number goes up when upgrading needs you to do something (a new required setting, a removed feature, a change that breaks the API), the minor number for new features, the patch number for fixes.

## 2.7.0 (2026-11-19)

### New

- Assistant: ask Claude about a problem in plain words ("S02E04 won't play on the living room TV", "why didn't this movie download?") and it investigates Radarr, Sonarr, SABnzbd, Plex, Tautulli, Bazarr, Seerr and Helperr's own runs, health history and log, then says what it found. It only reads. When it finds a fix it can make (search for episodes or a movie, refresh and rescan, remove a stuck download, refresh an item in Plex) it proposes it as a card with a button that says what will happen, and nothing happens until you click it. Nothing it can propose deletes library media.
- An **Ask** button at the bottom right of every page opens the chat as a popup that stays with you from page to page; the Assistant page shows every chat full width. Answers appear as Claude writes them, the line under your question says what it's looking up, you can write your next question while it works, and **Stop** ends a question after the step it's on. Every lookup is folded under the answer so you can check its work.
- Set it up on Settings → Assistant with an API key from console.anthropic.com (billed per use, separately from a Claude.ai subscription), with the model, effort and how many lookups a question may make. The key is stored encrypted. Your questions and what Claude reads are sent to Anthropic, with Helperr's keys removed and web addresses cut to their host; chats are kept for 90 days.
- `POST /api/assistant`, `GET /api/assistant/{id}` and `POST /api/assistant/{id}/stop`.

## 2.6.0 (2026-11-17)

### New

- Status: Unraid's UPS on its card, read live from Unraid (online or on battery, charge, runtime left, load). Running on battery, a low or worn-out battery, an overload or lost contact with the UPS are problems on the card and in the health history.

### Fixed

- Status: a power cut kept showing "Running on batteries" long after the power came back. Unraid never clears that alert (the "power has returned" one is only a normal notice), so its UPS alerts are left out now that the card has the UPS's live state. Without a UPS, or when Unraid can't read it, they're shown as before.

## 2.5.0 (2026-11-16)

### New

- TRaSH Guides sync (Runs, Settings → TRaSH Guides): keeps Radarr's and Sonarr's custom formats, quality profiles and quality sizes in line with TRaSH Guides, planned for review like removers. Link each quality profile to the guide profile it follows, with a score set and your own scores, which win. Each run lists what differs from the guides (a fixed regex, a new score, a quality size), you tick what to apply, and applying sends only what's still exactly as planned. Custom format fixes can be applied without review. Nothing is deleted. The guides are downloaded from GitHub once, then only when they change.
- TRaSH Guides: import a guide profile the arr doesn't have yet. Tick it on Settings → TRaSH Guides and the sync plans making it in the arr with the guide's qualities, cutoff, minimum scores and scores, for review like every other change.
- TRaSH Guides: each linked profile opens to a table of every custom format it scores, with the arr's score now, the guide's and yours, the ones the sync would change first, and its settings and allowed qualities next to the guide's (whether or not you follow them). Your own scores are set right there: empty follows the guide, and other custom formats are picked from a list, so a misspelled name can't happen. It replaces the "Your scores" text box.
- TRaSH Guides: apply part of a change. On a run's plan every score, setting and the allowed qualities of a quality profile (and each quality size) has its own checkbox; untick one to leave it as it is for now, and it's planned again next time. To keep your value for good, set it in the profile's table on Settings → TRaSH Guides.
- TRaSH Guides: differences are tables instead of lines of text, on the settings page (compared with each arr right now, before you run anything), on a run's plan and when applying it: a custom format's regex with the words it adds or removes marked, a profile's scores that differ next to the guide's, quality sizes as min, preferred and max.
- Status: a Right now band between the cards that need you and the healthy services, with what Plex is playing and what's downloading. Each stream shows where it is, its tracks and what Plex sends; a transcode is outlined with why and what to change, as on Playback. Each download is one row (Radarr's or Sonarr's title, a season pack as one, with SABnzbd's progress and time left), and a running parity check shows there too.

### Changed

- The [wiki](https://github.com/RyanThornburg/plex-library-helperr/wiki) walks through every page with screenshots.

### Fixed

- `opentelemetry-api` 1.45.2 (GHSA-mf5v-w3w3-f6cg), which FastAPI uses for tracing. Helperr wasn't open to it: it only reads trace context from HTTP headers, which can't hold the newline the attack needs.

## 2.4.0 (2026-11-14)

### New

- Kometa on the Status page. Kometa has no API, so it reports its runs through its webhooks instead: Settings → Webhook has the lines to paste into Kometa's `config.yml`. The card shows when the last run finished, how long it took and what it changed, and lists Kometa's errors from that run (a list that no longer exists, a filter it doesn't know), which go into the health history like any other problem. It warns when no run has finished for well over Kometa's usual time between runs, learned from its runs, and notes when a newer Kometa is out.

### Fixed

- Settings → Stuck downloads described Import blocked and Import pending the wrong way round for most stuck imports. Radarr and Sonarr call a single file they refused (not an upgrade, a TBA title) import pending; only a release of several files where some won't import is import blocked. To have those removed automatically, add Import pending (for Sonarr with a stuck time over 2 days, since a TBA title imports by itself 48 hours after airing).

## 2.3.0 (2026-11-14)

### New

- Library: every movie, show and season in Plex with everyone's plays of it, to find what nobody watches. Shows what's never been watched since it was added and what hasn't been watched in a while (180 days by default, any number you like), with filters for library, type and requester (anyone, no one, either, or one person), TV per show or per season, counts per requester and per library, sorting by longest idle, title, added, last played, plays or requester, and a CSV download. Plays count from Tautulli's whole history, including episodes Plex no longer has and shows moved to another library. It's synced every hour with Requests and only needs Tautulli and Plex. `GET /api/library` returns the same list. It replaces the separate plex-library-watch-history script.

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
