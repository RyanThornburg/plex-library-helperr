---
name: Helperr
description: A quiet control room for a Plex server, where color means something needs you.
colors:
  projector-amber: "#c7790b"
  projector-amber-dark: "#e5a00d"
  warning-ink: "#8f5600"
  signal-green: "#1a7f37"
  signal-green-dark: "#3fb950"
  alarm-red: "#c62828"
  alarm-red-dark: "#f85149"
  alarm-fill-dark: "#da3633"
  console-floor: "#f6f7f9"
  console-floor-dark: "#111418"
  panel: "#ffffff"
  panel-dark: "#1a1f25"
  ink: "#1c2128"
  ink-dark: "#e6e9ed"
  dim-ink: "#5d6673"
  dim-ink-dark: "#98a1ad"
  rule-line: "#d9dde3"
  rule-line-dark: "#2c333c"
  on-amber: "#111111"
typography:
  headline:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.5
  title:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "16px"
    fontWeight: 700
    lineHeight: 1.5
  metric:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.5
    fontFeature: "tnum"
  body:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1.5
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
rounded:
  sm: "6px"
  md: "8px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.projector-amber}"
    textColor: "{colors.on-amber}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "6px 14px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "6px 14px"
  button-danger:
    backgroundColor: "{colors.alarm-red}"
    textColor: "{colors.panel}"
    rounded: "{rounded.sm}"
    padding: "6px 14px"
  input:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "6px 10px"
  card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "14px 16px"
  tag:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  tag-solid-warn:
    backgroundColor: "{colors.projector-amber}"
    textColor: "{colors.on-amber}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  tag-solid-bad:
    backgroundColor: "{colors.alarm-red}"
    textColor: "{colors.panel}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  service-line:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.dim-ink}"
    padding: "10px 16px"
  table-header:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.dim-ink}"
    typography: "{typography.label}"
    padding: "8px 12px"
  topbar:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    padding: "12px 16px"
---

# Design System: Helperr

## Overview

**Creative North Star: "The Control Room"**

Helperr is the room you walk into to check that the server is fine. When everything is healthy it is calm: cool grey floors, white (or slate) panels, black ink, thin rule lines. Color is reserved for meaning. Green says a thing worked, amber says look at this, red says something failed or something will be deleted. An operator scanning the screen should be able to find every problem by color alone, and a screen with no problems should have almost no color on it.

It is a dense, expert surface for one admin at a desktop browser. Information is laid out as tables, fact lists and small cards, not prose; numbers are tabular; logs, paths, release names and cron expressions are monospace. There is no imagery, no illustration, no motion and no shadow. Depth comes from a panel sitting on a slightly darker floor, separated by a 1px rule. Light and dark themes follow the operating system (`prefers-color-scheme`); every color token has a dark counterpart.

The Status page is the reference for the idea: a one-line verdict first ("All 5 connected services are OK", or "2 services need you: …" linking to each), then a card for each service that needs you, then every healthy or unconnected service as a single dim line in one panel, opened for its details.

The look is system-native on purpose: system fonts, native form controls, native `<details>`, `<progress>` and checkboxes, so pages render instantly, work without JavaScript, and satisfy a strict CSP with nothing to load.

**Key Characteristics:**
- Neutral at rest; color only marks state (ok / warn / bad) or the one primary action. A healthy service has no state color at all.
- Flat: panels on a floor, 1px rules, no shadows.
- System fonts, one body size (15px), a short heading scale (20 / 16 px).
- Tables and definition lists over cards; cards group a service or a job, never decorate.
- Light and dark themes from the OS, no theme toggle.

## Colors

A cool slate neutral scale with three signal colors, of which amber also carries the brand.

### Primary
- **Projector Amber** (light `projector-amber`, dark `projector-amber-dark`): the brand mark, links, the primary button, the active settings tab and focus rings on chip inputs. It is also the warning color for borders, fills and washes (a warning card's outline, the filled `WARNING` tag), so amber always means "your attention here", whether that's an action or a caution. The dark value is Plex's gold.
- **Warning Ink** (`warning-ink`, `--warn-text`; dark mode uses Projector Amber): amber as text. Warning words, warn tags, running/queued/blocked statuses and transcode decisions use it, because Projector Amber on white is only about 3.4:1.

### Secondary
- **Signal Green** (`signal-green` / `signal-green-dark`): success only. Succeeded and done runs, healthy services, direct play, finished requests, added diff lines.
- **Alarm Red** (`alarm-red` / `alarm-red-dark`): failure and destruction. Failed runs, down services, errors, the danger button, the LIVE tag (a run that will really change or delete things), removed diff lines.
- **Alarm Fill** (`--bad-fill`: Alarm Red in light mode, `alarm-fill-dark` in dark): red behind white text (the danger button, filled `DOWN`/`ERROR` tags). Dark mode's Alarm Red is too light for white text (3.4:1); Alarm Fill is 4.6:1.

### Neutral
- **Console Floor** (`console-floor` / `console-floor-dark`): the page background behind every panel.
- **Panel** (`panel` / `panel-dark`): cards, tables, the top bar, inputs, logs, diffs.
- **Ink** (`ink` / `ink-dark`): all primary text, secondary-button text, focus outlines on buttons.
- **Dim Ink** (`dim-ink` / `dim-ink-dark`): table headers, labels, hints, empty states, dry-run status, the signed-in email.
- **Rule Line** (`rule-line` / `rule-line-dark`): every border and divider, 1px.
- **On Amber** (`on-amber`, `--on-accent`): text on the amber primary button and filled warning tags, in both themes.

### Named Rules
**The Quiet Board Rule.** A healthy screen is almost colorless. Green, amber and red appear only where a state needs reading; never use them for decoration, section color-coding or emphasis.

**The Red Means Real Rule.** Alarm Red is for failures and for anything that deletes or goes live. A dry run is never red; it's Dim Ink.

**The Amber Contrast Caveat.** Light-mode Projector Amber on white is about 3.4:1, below AA for body-size text. Warning text uses Warning Ink; Projector Amber stays for fills, borders, the brand and links. Links are still Projector Amber and still below AA in light mode, a known gap.

## Typography

**Body Font:** system-ui (with -apple-system, Segoe UI, sans-serif)
**Mono Font:** ui-monospace (with SFMono-Regular, Menlo, Consolas, monospace)

**Character:** The operating system's own UI font, so Helperr reads like a native tool. Monospace is for machine text: logs, file paths, diffs, cron expressions, API keys.

### Hierarchy
- **Headline** (700, 20px, 1.5): the page `h1`, one per page, often in a heading row with the page's primary action on the right.
- **Title** (700, 16px, 1.5): `h2` section and card titles.
- **Metric** (700, 22px, tabular numerals): the big number in a playback tile.
- **Body** (400, 15px, 1.5): everything else, including table cells and form fields.
- **Label** (600, 13px): table headers and form labels (form labels are 600 at body size); card subheadings (`h3`) are 13px in Dim Ink. Hints, tags and the user email are 12–13px at 400.
- **Mono** (400, 12px, 1.45): logs, file lists and diffs; inline `code` is 13px and doesn't wrap.

### Named Rules
**The Two Sizes Rule.** Text is 15px or 13px; headings are the only exceptions. Don't introduce 14px, 17px or 18px.

**The Tabular Numbers Rule.** Any number meant to be compared (counts, sizes, metrics) uses tabular numerals.

## Layout

A single centered column, at most 1100px wide, 16px padding, under a full-width top bar holding the brand, the page links and the signed-in email. Pages flow top to bottom: heading row (title left, primary action right), then banners, then cards or tables, with 24px above each section heading.

Groups of cards use auto-fit grids, each with its own minimum: job cards 260px, status cards 340px, connection forms 420px, metric tiles 140px, all with a 12px gap. Forms are a two-column grid (labels at max-content, fields up to 520px) with 12px × 16px gaps. Fact lists use the same two-column `dt`/`dd` grid.

Spacing steps are 4, 8, 12, 16 and 24px. At 700px and below, card grids collapse to one column, forms stack labels over fields, and wide tables scroll sideways inside their own box instead of the page.

## Elevation & Depth

Flat. There are no shadows anywhere. Depth is tonal: a panel (white / slate) on a slightly darker floor, outlined by a 1px Rule Line. Highlighted rows get a 10% amber wash; diff lines get 15% green or red washes.

### Named Rules
**The Flat Panel Rule.** Surfaces never lift. If something needs to stand out, give it a state color or move it up the page, not a shadow.

## Shapes

Gently rounded rectangles. Small controls (buttons, inputs, chip fields, banners, logs, diffs) use a 6px radius; cards and tiles use 8px; tags and chips are full pills. Tables are square-cornered. Borders are always 1px: Rule Line, or the state color for a container in a state (below).

**State markers.** A container in a state takes the state color on its whole 1px border (warning cards, banners, playback's transcode tile, the verdict); never a thick colored edge on one side. Inside, the state is said in words: a bold 13px state word ("error", "warning", "down", "note") before the line, so color is never the only signal. A container's problems sit in a block with a 10% wash of its state color (`color-mix`), 6px radius, 8px × 12px padding. Ongoing incident rows get the same wash across the row.

### Named Rules
**The Whole Border Rule.** State is an outline, a fill or a wash, never a stripe. The 4px left border was retired in October 2026.

## Components

### Buttons
Plain and decisive: one filled button per decision, everything else outlined.
- **Shape:** gently rounded (6px), 1px border.
- **Primary:** Projector Amber fill and border, On Amber text, 600 weight, 6px × 14px padding. Used for Run, Save, Apply.
- **Secondary:** transparent fill, Ink text, Rule Line border. Cancel, Test, Preview, row actions.
- **Danger:** Alarm Red fill and border, white text. Only for the final click of something destructive or live: deleting files, a rule or a schedule, removing downloads, turning on auto-approve, running or saving with live settings.
- **Focus:** 2px Ink outline, 2px offset. **Disabled:** 40% opacity. No hover transform or transition.
- Links styled as buttons (`.button`) look identical, so a confirmation step can be a plain GET link.

### Tags
- **Style:** pill, 1px Rule Line border, 12px text, transparent fill.
- **State:** the border and text take the state color: LIVE in Alarm Red, warnings in Projector Amber (text in Warning Ink), ok in Signal Green. Dry run uses the neutral tag.
- **Solid:** a state that needs reading now is a filled pill, 600 weight: Alarm Fill with white text (`down`, `error`, an ongoing error incident), Projector Amber with On Amber text (`warning`, an ongoing warning).

### Cards / Containers
- **Corner Style:** 8px.
- **Background:** Panel on Console Floor.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** 1px Rule Line.
- **Internal Padding:** 14px × 16px, children 8px apart; card actions pin to the bottom.

### Tables
The main way data is shown. Panel background, 1px outer border and row rules, 8px × 12px cells, left-aligned. Headers are Label type in Dim Ink. Row actions sit right-aligned and never wrap. Status words in cells are 600 weight in their state color.

### Inputs / Fields
- **Style:** Panel fill, 1px Rule Line border, 6px radius, 6px × 10px padding, body font; number inputs max 160px.
- **Chip lists:** list fields (one value per line in a textarea) become a row of pill chips with an inline input when JavaScript runs; the field gets a 2px amber outline while focused.
- **Errors:** listed in Alarm Red above the form; hints sit under the field in 13px Dim Ink.

### Navigation
- **Top bar:** Panel background, bottom rule, brand in bold Projector Amber, page links in Ink turning amber on hover; signed-in email at the right in 13px Dim Ink. Wraps on narrow screens.
- **Settings tabs:** a row of links over a bottom rule; the current tab is Ink, 600 weight, with a 2px amber underline; others are Dim Ink.

### Banners
A full-width notice under the page heading: 1px border, 6px radius, 10px × 14px padding. A warning or error banner takes the state color on its whole border over a 7–8% wash of it; a success banner has a green border on Panel.

### Status Verdict
The first thing on the Status page: a Panel box, 16px text, one sentence. Healthy: "All N connected services are OK." on a neutral border. Otherwise: "N services need you:" in bold, then one link per service ("Sonarr: 2 warnings, 3 stuck, for 2d 4h") in Ink, 600 weight, underlined, with the border in the worst state's color. Nothing connected: a sentence linking to Connections.

### Service Lines
Healthy and unconnected services, as rows of one Panel with 1px rules between them. Each row is a grid (disclosure chevron 8px, name and version 14rem, state 7rem, facts): name 600 in Ink, version, state and facts in Dim Ink, facts on one line with an ellipsis. A healthy row is a `<details>` that opens to its facts, items and notes; its chevron is drawn in CSS. An unconnected row says "not connected" and links to its Connections form. Below 700px the facts wrap under the name.

### State Cards
A service that needs you: a card with its whole border in the state color, a filled state tag in the header, then the problem block (problems with state words and how long each has lasted), then its facts and items. A down card's block says why and links to that service's connection form. Cards sit in an auto-fit grid (340px minimum), aligned to the top, never stretched to equal height.

### Metric Tiles
Small panels in an auto-fit grid: a 22px tabular number over a 13px Dim Ink label. Used on Playback for counts and rates; a tile that needs attention (transcodes) takes an amber border.

### Logs and Diffs
Panel boxes in 12px monospace, capped at 60% of the viewport height with their own scroll. Diffs color whole lines with a 15% green or red wash.

## Do's and Don'ts

### Do:
- **Do** use the CSS custom properties (`--bg`, `--surface`, `--text`, `--muted`, `--border`, `--accent`, `--ok`, `--bad`) and add both a light and a dark value for any new one.
- **Do** keep pages working without JavaScript and within the CSP: one stylesheet, self-hosted assets, no inline styles or scripts.
- **Do** put the page's primary action in the heading row, right of the `h1`.
- **Do** show dry run vs live with a tag next to the job or run, every time.
- **Do** use tables for any list of more than a few comparable items, with tabular numbers and thousands separators.
- **Do** say every state in a word next to its color, and let long names and paths wrap (`overflow-wrap: anywhere` in cards, `min-width: 0` on grid children); set release names and paths in 12px mono.
- **Do** collapse what's healthy: a healthy thing is one dim line, a thing that needs you is a card.

### Don't:
- **Don't** use green, amber or red for anything but state (and amber for the one primary action).
- **Don't** add shadows, gradients, background images or decorative illustration.
- **Don't** mark state with a colored side border (`border-left` wider than 1px); use the whole border, a fill or a wash.
- **Don't** add web fonts or icon fonts; system fonts only.
- **Don't** set warning text in Projector Amber; use Warning Ink.
- **Don't** reload a page on a timer without a way to pause it (`?refresh=0` on Status).
- **Don't** use the danger button for anything reversible or dry; a dry run's Run button is the plain primary.
