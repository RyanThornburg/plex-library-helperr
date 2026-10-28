---
target: Status page
total_score: 26
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
target_identity: "file:/home/ratt/code/plex-library-helperr/web/templates/status.html"
target_fingerprint: "sha256:1e3b771e2cf0d2502a6a85cf62dbbc9b8daa90e393b7cb19e70e6b1336a83a36"
target_path: /home/ratt/code/plex-library-helperr/web/templates/status.html
timestamp: 2026-10-28T03-22-43Z
slug: web-templates-status-html
closed: true
---
Method: dual-agent (A: design review · B: detector + rendered-HTML scan)

# Critique: Status page (web/templates/status.html)

Rendered with test data (mixed problems, all ok, all off, many incidents) at desktop light/dark and 390px; no user-visible overlay.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|---|---|---|
| 1 | Visibility of System Status | 3 | No board-level verdict; cards don't say how long a problem has lasted |
| 2 | Match System / Real World | 3 | Native *arr vocabulary; a few cramped labels |
| 3 | User Control and Freedom | 3 | 60s auto-refresh can't be paused |
| 4 | Consistency and Standards | 2 | problem/warning/down vs error/warning; 4px vs 2px stripes; amber = link and warning |
| 5 | Error Prevention | 3 | "Remove selected" has no count or consequence |
| 6 | Recognition Rather Than Recall | 3 | Problem detail on card, duration in table |
| 7 | Flexibility and Efficiency | 2 | No links to the services' UIs, no mute for known warnings |
| 8 | Aesthetic and Minimalist Design | 2 | Healthy board has 10 green marks; equal-height empty cards; overflow |
| 9 | Error Recovery | 2 | Down card is a dead end |
| 10 | Help and Documentation | 3 | Plain inline explanations |
| **Total** | | **26/40** | **Acceptable** |

## Design Specificity Verdict

Specific in words, interchangeable in form: generic monitoring-tile grid (stripe + pill + key/value), fixed service order instead of severity, healthy board breaks the Quiet Board Rule. Detector: 4 false positives on raw Jinja (no CSS); 9 true positives rendered: 8 side-tab (app.css:318-321), 1 low-contrast dark-mode danger button (#fff on #f85149, 3.4:1, queue.html:55, app.css:107). Missed by detector: incident-row stripe (app.css:360-361), 2px item stripe (app.css:332-334).

## What's Working
- Honest domain-true copy and safety (re-check before removal, season packs grouped, redacted errors)
- Failure isolation per card; config error banner; unconnected cards deep-link to their form
- History "Lasted" and per-service downtime turn alerts into evidence

## Priority Issues

- [P1] No board-level verdict; severity not encoded in order; recent[:RECENT_LIMIT] (status_routes.py:82) can drop old ongoing incidents. Fix: headline under h1 linking to cards; sort ongoing first. Commands: clarify + layout.
- [P1] Replace the state stripe; quiet healthy board. Fix: healthy = neutral border + dim ok tag; warn/bad = full 1px state border, filled state pill, problems first in a 10% wash block; state words on items/queue rows; Level column + ongoing wash on incidents; banners 1px outline + state word; update DESIGN.md Shapes. Commands: colorize, then document.
- [P1] Long strings overflow cards (desktop + 390px horizontal scroll), incidents break mid-word. Fix: min-width:0 on cards, overflow-wrap:anywhere on hints/problems/facts, release names/paths in 12px mono, stacked incident rows below 700px. Commands: harden + adapt.
- [P2] Act where you look stops at the card: down card has no next step, no links to services, no durations, Seerr pending (status.py:474) warns for a to-do queue. Fix: "Down for 17m · Test · Connections", "Open Sonarr ↗", durations, Seerr pending neutral, "Remove 3 downloads" + radio groups stating consequences. Command: clarify.
- [P2] Accessibility: forced 60s meta refresh (WCAG 2.2.1), amber problem text 3.4:1 (app.css:329), dark danger button 3.4:1, unlabeled progress bars. Fix: pause link (?refresh=0), Ink problem text + bold state word, darker dark-mode danger fill, aria-labels. Command: audit.

## Persona Red Flags
- Alex: no mute for accepted warnings, no jump-out links, absolute wrapping timestamps, no count on destructive button.
- Sam: forced reload, color-only severity, amber below AA, unlabeled progress bars.
- GitHub installer: low-disk without threshold, false all-clear when nothing connected (status.html:68), unexplained state words, logo goes to Runs.

## Minor Observations
- facts lack tabular-nums and thousands separators
- progress bars amber (app.css:337); queued downloads show 0% bars
- off cards get a grey 4px stripe
- equal-height grid rows (use align-items:start)
- Plex card green while its transcode row is amber
- queue table has no .table-scroll; duplicate status messages

## Questions to Consider
- Should a healthy service be one dim line rather than a card?
- Are Now playing and the SABnzbd download list status or activity?
- Should Status be the home page?
