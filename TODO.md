# TODO

Planned work and ideas, roughly in order within each section. Nothing here is promised.

## Security review (2026-11-26)

A whole-codebase review: no critical or high findings. Each item was verified, most with a test. Numbers are the review's finding ids.

### Medium

- [x] **#94 Half-sent POST bodies**: a client that disconnects mid-body raises an uncaught `ClientDisconnect` in the form/json readers (`routes.form_lists`, `settings_common.form_lists`, `webhooks.json_body`). Unauthenticated on `/login`; each logs a full traceback on the event loop, so a few hundred stall the app and rotate the log away.
- [x] **#62 Login throttle race**: `wait()` then scrypt then `failed()`, so a parallel burst gets ~40 guesses instead of 5 and ~1 GB of scrypt memory. Reserve the attempt before hashing; at most a couple of hashes at once.
- [x] **#23 Saved secrets follow the URL**: Test/Save (and `PUT /api/config`) with a changed URL and no key sends the saved key to the new address. Tie a secret to the origin it was saved for.
- [x] **#44 Queue cleanup clock**: time a download spent in a warning state that isn't removable counts toward `stuck_for`, so it's removed (and blocklisted) minutes after an import gets blocked. Count from when it first became removable.
- [x] **#47 Sonarr watch protection by numbers**: Plex's season/episode numbers are matched to Sonarr's; a show Plex orders differently (anime) gets no protection. Fail closed when a recent watch can't be placed.
- [x] **#72 TRaSH review hides part of what's sent**: conditions with a repeated name collapse in the diff, added conditions show only `value`.
- [x] **#57 / max_deletes and auto-approve**: removing or raising a remover's `max_deletes` (form or restore) keeps auto-approve on without asking, and an auto-approved rule with no `max_deletes` has no cap.

### Low: deletion safety

- [ ] #46 Movie remover can't see a play in progress (only finished plays are stored), so a first watch in progress is "never played".
- [ ] #50 Movie remover ignores the file's age: added long ago, downloaded yesterday, removable now.
- [ ] #48 Seasons mode can delete a multi-episode file that also holds an episode of a kept season.
- [ ] #49 Sonarr remover reads `0001-01-01` as ancient (Radarr's parser treats it as no date).
- [ ] #51 Duplicates: the multi-episode guard misses Sonarr's `Duplicate`/`NxNN` styles; an unknown arr file date skips `min_age`.
- [ ] #54 The auto-approve confirmation isn't tied to the rule's content: a stale confirm approves an edited rule.

### Low: sign-in and exposure

- [ ] #82 / #2 Login throttle and proxies: behind a proxy everyone shares one bucket (5 bad tries per 15 min lock the admin out of sign-in and password change); with a loopback proxy the key follows `X-Forwarded-For`. `proxy_headers=False` by default, an explicit trusted-proxies setting.
- [ ] #60 / #61 Unauthenticated error pages (e.g. `OPTIONS /healthz`) render the full shell: version, services needing attention, failed runs, plans. Skip `read_needs()` without a user.
- [ ] #88 / #20 / #29 Logs: unauthenticated long paths rotate the 1.5 MB log away; newlines in webhook fields and upstream text forge lines. Cap and escape in one logging filter, raise retention.
- [ ] #4 A failed sign-in logs the typed username, which may be a password typed in the wrong box.
- [ ] #92 500 responses carry no security headers.
- [ ] #5 / #10 / #7 Origin check ignores the scheme, no Host allowlist (DNS rebinding on a LAN install), trailing-slash redirects are absolute `http://`. An optional `PUBLIC_URL` / allowed hosts setting fixes these together, with HSTS.
- [ ] #32 Settings → Login shows any text from `?signed_out=` in its banner.
- [ ] #65 Some inputs raise 500s instead of 4xx (`status_routes`).
- [ ] #34 A malformed URL fragment (`/status#%`) makes `app.js` throw on load.

### Low: other services' secrets and the assistant

- [ ] #66 Indexer keys in SABnzbd and Unraid text reach `health_incidents` and `web.log`. Apply `url_hosts()` centrally where problem text and keys are made.
- [ ] #53 / #43 Credentials in a URL (`user:pass@host`) are stored plain, shown on pages, kept by `url_hosts()` and sent to Anthropic.
- [ ] #19 / #25 The assistant's `clean()` removes only our own secrets: token fields, emails and URL userinfo in upstream answers go to Anthropic and the db.
- [ ] #33 / #37 Assistant answers can link anywhere, including helperr's own deletion pages; third-party text in action descriptions lands in the `<context>` block.
- [ ] #38 An episode search's description names every episode under the first episode's series.
- [ ] #39 Confirm/dismiss of a proposal isn't atomic: a double confirm runs it twice.
- [ ] #40 Tool params are unrestricted: GET flags that act (Plex async refresh/analysis, SABnzbd benchmark) get through.
- [ ] #42 `helperr_query`'s time and size limits can be bypassed (GBs of memory in one call).
- [ ] #85 Under `--read-only`, `helperr_query` fails with "disk I/O error" on GROUP BY (sqlite temp store).

### Low: robustness

- [ ] #24 / #58 Quadratic regexes in `url_hosts()`/`failure_text()` and quadratic list dedup in settings forms.
- [ ] #26 / #27 Redirects followed on key-carrying requests (only Unraid's refuses); no overall deadline or size cap on upstream answers.
- [ ] #67 / #68 A partial Plex/Unraid check failure closes open incidents as cleared; a temperature all-clear clears an earlier disk error.
- [ ] #71 Out-of-range numbers from a service crash its whole card.
- [ ] #14 / #15 / #16 Webhook key: a negative Kometa `run_time` pins the card to ok; tables can grow without limit; it also schedules empty trash follow-ups.
- [ ] #56 A "changed since this page was loaded" error re-renders stale values with the current version, so the next Save overwrites the other change.
- [ ] #73 / #74 / #76 / #77 TRaSH: a malformed snapshot is stored before validation (500 until upstream changes); no size limits on the download; case-only name clashes share a plan key; re-downloading an old commit makes it "newest".
- [ ] New request/poster code: #78 Seerr calls on the event loop in the new POST handlers, #79 `users()` pages forever if Seerr ignores `skip`, #80 a poster name ending in a newline 500s, #81 urllib3 read errors uncaught.
- [ ] #84 uv 0.11.2 in the Dockerfile has published advisories; Dependabot PRs unmerged.
- [ ] #86 `.gitignore` misses `helperr.db`, its `-wal`/`-shm` and `posters/`.
- [ ] #87 `HELPERR_SECRET_KEY` only from an env var, likely in the same backups as the db: support `HELPERR_SECRET_KEY_FILE` and say so in the docs.

### Hardening

- [ ] Create `helperr.db`, WAL/SHM, logs and posters owner-only (umask 077).
- [ ] Register the webhook API key with log redaction; `hide_parameters=True` on the SQLAlchemy engine.
- [ ] Don't follow redirects on requests that carry keys (one shared outbound helper).
- [ ] Validate service URLs in `build_config` (scheme, host, no userinfo), so the API and restore match the forms; `check_types()` for the service config classes; bound durations and require ASCII digits.
- [ ] Refuse auto-approve while `max_deletes` is unset; re-confirm auto-approve rows when `REMOVERS_ALLOW_APPLY` goes from off to on; a confirm flag on `POST /api/removers/auto-approve`.
- [ ] A `max_items` cap for movers/profilers runs.
- [ ] Raise scrypt cost (OWASP: 2^17, r 8, p 1) and rehash on sign-in; the throttle shouldn't block a signed-in admin's password change.
- [ ] Drop the `WWW-Authenticate: Basic` challenge on webhook 401s.
- [ ] Fetch Metadata (`Sec-Fetch-Site`) check, reject duplicate `Origin` headers; HSTS and a `__Host-` cookie on https.
- [ ] `SecretBox`: bind ciphertexts to their name, allow key rotation (MultiFernet), warn on a low-entropy key.
- [ ] Allowlist query params per assistant tool, `[0-9]+` rather than `\d+` in path patterns; `rel="noopener noreferrer"` on answer links and mark external ones.
- [ ] Cap paging loops (Seerr, Tautulli) and bound the TRaSH download.
- [ ] Render the live assistant answer off the event loop.
- [ ] Run Alembic migrations with `foreign_keys` off.
- [ ] One test that walks every route in both auth modes (auth, Origin, headers).
- [ ] CI: a dependency scan (pip-audit/osv), digest-pinned base images visible to Dependabot, a Dependabot cooldown.
- [ ] Docs: the README publishes the port (`-p 8780:8080`) while the Dockerfile says never to; say what plain http on a LAN means for the cookie.

## TRaSH Guides sync, later

- [ ] Optional custom format groups (the guides' cf-groups, like `[Unwanted] Unwanted Formats`): offer a group's new custom formats for a linked profile, so a format TRaSH adds to a group you use shows up in the plan.
- [ ] Naming: the guides' file and folder naming formats.

## Services I don't use (for other people's setups)

Support for a service comes in three levels, and most only need the first:

1. A connection with a Test button, a Status card, and health incidents
2. Actions from Status, like removing stuck downloads
3. Rules: movers, profilers, removers

- [ ] **Prowlarr** (level 1): failing or disabled indexers (`indexerstatus`, `health`) and the apps it syncs to. A dead indexer quietly stops new grabs, so this is the most useful of these.
- [ ] **More than one instance of an arr**, such as a 4K Radarr. Today there's one `[radarr]` and one `[sonarr]`. This touches config, secrets, rules, webhooks and Status, so do it before more rules work for other setups.
- [ ] **Lidarr** (levels 1–2): health, queue, missing items, root folders. Its API is v1, not v3, and it works with artists and albums. Music movers only if someone asks for them.
- [ ] **Other download clients**: qBittorrent, NZBGet, Transmission, Deluge (level 1). Queue, stalled or errored downloads, free space.
- [ ] **Jellyfin / Emby**: a big job, because Tautulli and the Requests and Playback pages are Plex-only. Probably not.
- Skip: Readarr (retired, and its metadata service no longer answers) and Whisparr.
- Already supported: Radarr, Sonarr, Seerr (Overseerr / Jellyseerr), SABnzbd, Bazarr, Unraid, Plex, Tautulli, Autoscan, Kometa (through its webhooks).

## Add requests from the site

- [ ] Search for a movie or show and add it.
  - **Through Seerr** when it's connected (`POST /api/v1/request`). Seerr's defaults apply, the request counts in Requests stats, and Seerr tracks when it's available.
  - **Directly to Radarr/Sonarr** when Seerr isn't connected (`movie/lookup` + `POST movie`, `series/lookup` + `POST series`). Root folder, quality profile, tags and monitor/search options come from pick lists, which `Lookups` already has. The add webhook then runs movers as usual.
  - Something already in the library shows where it is instead of an Add button.
  - Posters: the CSP blocks third-party images. Either go without posters or serve them through the app (fetched from TMDB on the server and cached).
- [ ] Maybe: approve or decline Seerr's pending requests (Status only counts them today).

## Other ideas

- [ ] **Orphaned files**: files in root folders that neither Plex nor an arr knows about, with their sizes. Review before deleting, like removers. Needs the media mounted in the container, which it has avoided so far (everything goes through the APIs); duplicate versions cover the re-download leftovers Plex sees.
- [ ] **Stop a Plex stream** from Status (with a message), for a transcode that's eating bandwidth.
- [ ] **Seerr issues** listed on Status, not just counted.
