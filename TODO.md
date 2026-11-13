# TODO

Planned work and ideas, roughly in order within each section. Nothing here is promised.

## In progress

- **Automatic queue cleanup**: a `clear_stuck` job (`[radarr.queue_cleanup]`, `[sonarr.queue_cleanup]`, migration 0014) removes downloads that the health checks have seen stuck for longer than `stuck_for`, the same way the queue page's Remove does.

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
- Already supported: Radarr, Sonarr, Seerr (Overseerr / Jellyseerr), SABnzbd, Bazarr, Unraid, Plex, Tautulli, Autoscan.

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
