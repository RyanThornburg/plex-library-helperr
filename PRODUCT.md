# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One Plex server admin (the owner), working at a desktop browser, usually in a tab next to other work; the phone is an occasional second screen. They know Radarr, Sonarr, Seerr, SABnzbd, Tautulli and Plex well and want expert density, not hand-holding.

The code may later be published on GitHub for other Plex admins to self-host, so screens should make sense to a technically capable admin who didn't build it. There is still one operator per install: no multi-user roles, sharing or onboarding funnels.

## Product Purpose

Helperr is a single place to keep a Plex library healthy, so the admin doesn't have to log into each service:

1. **Is anything broken?** (the main job). Current state of Plex, Radarr, Sonarr, SABnzbd and Seerr: services down, stuck downloads, low disk space, with a history of health incidents.
2. **Library maintenance.** Movers, profilers, removers and Plex empty trash, run on demand, on schedules or from *arr webhooks, with run history.
3. **Usage insight.** Who requests what and whether they watch it (Seerr + Tautulli), and how Plex streamed recent plays and why they transcoded, so the server can be tuned.

Success: opening Helperr answers "is everything OK, and if not, what do I do" at a glance, and library cleanup happens without surprises.

## Positioning

It acts on the library and gates every deletion. Dashboards like Homarr show status, Tautulli and Tracearr show watch history, Maintainerr deletes; Helperr combines status, upkeep jobs and request-vs-watch insight, and anything that deletes media goes through a reviewable plan.

## Operating Context

- Self-hosted Docker container on Unraid, reached through a Cloudflare Tunnel behind Cloudflare Access (allow-listed emails).
- Talks to Radarr, Sonarr, Plex, Tautulli, Seerr, SABnzbd and Autoscan through their APIs; state lives in a local SQLite database.
- Typical loop: glance at Status → fix stuck downloads or check the cause of an incident → review a removers plan and approve the series/movies to delete → adjust rules or schedules in Settings.
- Status refreshes itself every minute; runs and their logs are kept for review.

## Capabilities and Constraints

- Server-rendered pages (FastAPI + Jinja templates, one `app.css`, one small `app.js`), no frontend build step. Every action also has a JSON API.
- Strict Content Security Policy: no inline scripts or styles, no third-party content. Any font, script or image must be self-hosted. No JS `confirm()`: confirmations are their own pages.
- Every page must work without JavaScript; JS only enhances (local time zones, chip inputs, time zone defaults).
- Deletion safety is a product principle: removers plan first, deletion needs per-item approval or an opt-in per-rule auto-approve (off by default), and a container env var (`REMOVERS_ALLOW_APPLY`) is the breaker the UI can't turn on. Any change that makes a job live goes through a confirmation that says so.
- Dry run is a first-class state everywhere a job appears; the UI must always make clear whether a run did or would do something.
- Secrets are write-only in forms and never shown or logged.
- Terminology from the code: movers, profilers, removers, empty trash, runs, plans, approve/apply, dry run/live, schedules, incidents, stuck downloads.

## Brand Commitments

- Name: **Helperr** (repo: plex-library-helperr), following the *arr naming convention.
- Voice: plain, factual and short; says what happened or what will happen, including what would be deleted and why something was skipped.

## Evidence on Hand

- Real data from the owner's server: health incidents, removers plans and applied runs, request/watch stats (thousands of Seerr requests, ~180k Tautulli plays), playback quality.
- No testimonials, users or public adoption exist yet; don't invent any.

## Product Principles

1. **Problems first.** The first screen answers "is anything wrong" before anything else; healthy services stay quiet.
2. **Nothing deletes by surprise.** Live vs dry run, what will be removed and why must be unmistakable before any destructive action.
3. **Act where you look.** When a problem is shown, the fix (or a direct link to it) sits next to it.
4. **Dense, not cluttered.** An expert reads it in seconds: tables, counts and states over prose and decoration.
5. **Readable to a stranger.** Labels and copy should make sense to another Plex admin who installs it from GitHub.
