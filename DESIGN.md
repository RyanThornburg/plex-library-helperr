---
name: Helperr
description: A quiet control room for a Plex server, where color means something needs you.
colors:
  projector-amber: "#c7790b"
  projector-amber-dark: "#e5a00d"
  warning-ink: "#8f5600"
  link-ink: "#1a67c9"
  signal-blue: "#1a67c9"
  signal-blue-dark: "#5d9cec"
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
  chrome: "#171b20"
  chrome-dark: "#0b0e11"
  chrome-side: "#1f252c"
  chrome-side-dark: "#14181d"
  chrome-hover: "#29313a"
  chrome-hover-dark: "#1f252c"
  chrome-text: "#c3cbd4"
  chrome-muted: "#9aa4af"
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
    backgroundColor: "{colors.ink}"
    textColor: "{colors.panel}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "6px 14px"
  button-primary-dark:
    backgroundColor: "{colors.ink-dark}"
    textColor: "{colors.console-floor-dark}"
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
  appbar:
    backgroundColor: "{colors.chrome}"
    textColor: "{colors.chrome-text}"
    height: "56px"
    padding: "0 20px"
  sidebar-item:
    backgroundColor: "{colors.chrome-side}"
    textColor: "{colors.chrome-text}"
    width: "212px"
    padding: "9px 16px 9px 20px"
  sidebar-item-active:
    backgroundColor: "color-mix(in srgb, #e5a00d 9%, transparent)"
    textColor: "{colors.projector-amber-dark}"
    padding: "9px 16px 9px 20px"
  sidebar-subitem:
    backgroundColor: "{colors.chrome-side}"
    textColor: "{colors.chrome-muted}"
    padding: "6px 16px 6px 50px"
  toolbar-button:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "6px 10px"
    height: "60px"
  badge:
    backgroundColor: "#3a434e"
    textColor: "{colors.ink-dark}"
    rounded: "{rounded.pill}"
    padding: "0 6px"
    height: "20px"
  badge-warn:
    backgroundColor: "{colors.projector-amber-dark}"
    textColor: "{colors.on-amber}"
    rounded: "{rounded.pill}"
    height: "20px"
  badge-info:
    backgroundColor: "{colors.signal-blue}"
    textColor: "{colors.panel}"
    rounded: "{rounded.pill}"
    height: "20px"
  badge-bad:
    backgroundColor: "{colors.alarm-fill-dark}"
    textColor: "{colors.panel}"
    rounded: "{rounded.pill}"
    height: "20px"
---

# Design System: Helperr

## Overview

**Creative North Star: "The Control Room"**

Helperr is the room you walk into to check that the server is fine. When everything is healthy it is calm: cool grey floors, white (or slate) panels, black ink, thin rule lines, inside a dark frame. Color is reserved for meaning. Green says a thing worked, amber says look at this, red says something failed or something will be deleted, and blue, as in Sonarr and Radarr, is a link, something in progress, or a number's share; blue is never a problem. An operator scanning the screen should be able to find every problem by color alone, and a screen with no problems should have almost no color on it.

It is a dense, expert surface for one admin at a desktop browser. Information is laid out as tables, fact lists and small cards, not prose; numbers are tabular; logs, paths, release names and cron expressions are monospace. There is no imagery, no illustration, no motion and no shadow; the only pictures are small monochrome line icons, each next to a word. Depth comes from a panel sitting on a slightly darker floor, separated by a 1px rule. Light and dark themes follow the operating system (`prefers-color-scheme`); every color token has a dark counterpart.

The Status page is the reference for the idea: a one-line verdict first ("All 5 connected services are OK", or "2 services need you: …" linking to each), then a card for each service that needs you, then every healthy or unconnected service as a single dim line in one panel, opened for its details.

The shell is modelled on Sonarr and Radarr, so Helperr reads as a sibling *arr: a dark header and a dark sidebar in both themes, and a toolbar of icon-over-label actions above each page. The chrome frames the board; it never carries state color except the badges and pill that say something needs you.

The look is system-native on purpose: system fonts, native form controls, native `<details>`, `<progress>` and checkboxes, so pages render instantly, work without JavaScript, and satisfy a strict CSP with nothing to load.

**Key Characteristics:**
- Neutral at rest; color only marks state (ok / warn / bad) and, in gold, the brand and where you are. A healthy service has no state color at all.
- A dark *arr-style frame in both themes: header, 212px sidebar, then a toolbar of the page's actions.
- Flat: panels on a floor, 1px rules, no shadows.
- System fonts, one body size (15px), a short heading scale (20 / 16 px).
- Tables and definition lists over cards; cards group a service or a job, never decorate.
- Light and dark themes from the OS, no theme toggle.

## Colors

A cool slate neutral scale with three signal colors, of which amber also carries the brand, inside a dark slate chrome.

### Primary
- **Projector Amber** (light `projector-amber`, dark `projector-amber-dark`, `--accent`): the warning color for borders, fills and washes (a warning card's outline, the filled `WARNING` tag), and a share bar past its high mark. Amber means brand, where you are, or a warning; it is never the primary action. The dark value is Plex's gold.
- **Gold** (`--gold`, the same value as `projector-amber-dark`, in both themes): the chrome's brand color. The "H" mark and the Helperr wordmark, the active sidebar section and sub page, focus rings in the chrome, and the warning badge.
- **Primary** (`--primary` / `--on-primary`): the primary button. Ink with Panel text in light mode; Ink (dark) with Console Floor (dark) text in dark mode. Ink, so gold keeps meaning brand, here and warning.
- **Link Ink** (`link-ink`, `--link`): Signal Blue in both themes. Links were amber until November 2026, which made every count in a table look like a warning.
- **Warning Ink** (`warning-ink`, `--warn-text`; dark mode uses Projector Amber): amber as text. Warning words, warn tags, blocked statuses and transcode decisions use it, because Projector Amber on white is only about 3.4:1.

### Secondary
- **Signal Blue** (`signal-blue` / `signal-blue-dark`, `--info`; `--info-fill` behind white text): the *arrs' blue, never a problem. Links, running and queued runs, started requests, the plan-for-review tag and the Activity badge's plans, info notes on Status, checkbox/radio/progress accents and text selection, chip-field focus, the rule preview's highlighted rows, and share bars. 5.5:1 on white (#1a67c9), 6.5:1 on the dark floor (#5d9cec, Sonarr's blue).
- **Signal Green** (`signal-green` / `signal-green-dark`): success only. Succeeded and done runs, healthy services, direct play, finished requests, added diff lines.
- **Alarm Red** (`alarm-red` / `alarm-red-dark`): failure and destruction. Failed runs, down services, errors, the danger button, the LIVE tag (a run that will really change or delete things), removed diff lines.
- **Alarm Fill** (`--bad-fill`: Alarm Red in light mode, `alarm-fill-dark` in dark): red behind white text (the danger button, filled `DOWN`/`ERROR` tags). Dark mode's Alarm Red is too light for white text (3.4:1); Alarm Fill is 4.6:1.

### Neutral
- **Console Floor** (`console-floor` / `console-floor-dark`): the page background behind every panel.
- **Panel** (`panel` / `panel-dark`): cards, tables, the toolbar, inputs, logs, diffs.
- **Ink** (`ink` / `ink-dark`): all primary text, the primary button's fill, secondary-button and toolbar text, focus outlines on buttons.
- **Dim Ink** (`dim-ink` / `dim-ink-dark`): table headers, labels, hints, empty states, dry-run status.
- **Rule Line** (`rule-line` / `rule-line-dark`): every border and divider, 1px.
- **On Amber** (`on-amber`, `--on-accent`): text on filled warning tags and the warning badge, in both themes.

### Chrome
Dark in both themes, a step darker in dark mode so the frame still sits below the page.
- **Chrome** (`chrome` / `chrome-dark`, `--chrome`): the header.
- **Chrome Side** (`chrome-side` / `chrome-side-dark`, `--chrome-side`): the sidebar, full page height.
- **Chrome Hover** (`chrome-hover` / `chrome-hover-dark`, `--chrome-hover`): a sidebar link under the pointer (its text turns white), the open phone menu.
- **Chrome Text** (`chrome-text`, `--chrome-text`): sidebar section labels and icons, the Menu button.
- **Chrome Muted** (`chrome-muted`, `--chrome-muted`): sidebar sub pages and the signed-in email.

### Named Rules
**The Quiet Board Rule.** A healthy screen is almost colorless. Green, amber and red appear only where a state needs reading; blue only on links, things in progress, selection and data. Never use any of them for decoration, section color-coding or emphasis. Waiting isn't failing: a request not started yet is Ink, not red. Gold in the chrome marks the brand and where you are, nothing else.

**The Red Means Real Rule.** Alarm Red is for failures and for anything that deletes or goes live. A dry run is never red; it's Dim Ink.

**The Amber Contrast Caveat.** Light-mode Projector Amber on white is about 3.4:1, below AA for body-size text. Warning text uses Warning Ink; Projector Amber stays for fills, borders and washes; Gold sits on the dark chrome, where it reads.

## Typography

**Body Font:** system-ui (with -apple-system, Segoe UI, sans-serif)
**Mono Font:** ui-monospace (with SFMono-Regular, Menlo, Consolas, monospace)

**Character:** The operating system's own UI font, so Helperr reads like a native tool. Monospace is for machine text: logs, file paths, diffs, cron expressions, API keys.

### Hierarchy
- **Headline** (700, 20px, 1.5): the page `h1`, one per page, often in a heading row with the page's primary action on the right.
- **Title** (700, 16px, 1.5): `h2` section and card titles.
- **Body** (400, 15px, 1.5): everything else, including table cells and form fields.
- **Label** (600, 13px): table headers and form labels (form labels are 600 at body size); card subheadings (`h3`) are 13px in Dim Ink. Hints, tags and the user email are 12–13px at 400; sidebar sub pages are 13px.
- **Mono** (400, 12px, 1.45): logs, file lists and diffs; inline `code` is 13px and doesn't wrap.

### Named Rules
**The Two Sizes Rule.** Text is 15px or 13px; headings, and 12px tags, badges and toolbar labels, are the only exceptions. Don't introduce 14px, 17px or 18px.

**The Tabular Numbers Rule.** Any number meant to be compared (counts, sizes, metrics) uses tabular numerals.

## Layout

An app shell: a 56px header across the top, a 212px sidebar down the left, then the page's toolbar and the page beside it. The header and the toolbar are sticky on desktop (anchors land below both). The page is fluid up to 1600px, left aligned next to the sidebar, with 20px × 24px padding. Prose paragraphs (a page's or card's dim explanation) are capped at 75ch so they stay readable on a wide screen. Pages flow top to bottom: the `h1`, then banners, then cards or tables, with 24px above each section heading.

Groups of cards use auto-fit grids, each with its own minimum: job cards 260px, status cards 340px, connection forms 420px, all with a 12px gap. Forms are a two-column grid (labels at max-content, fields up to 520px) with 12px × 16px gaps. Fact lists use the same two-column `dt`/`dd` grid.

Spacing steps are 4, 8, 12, 16 and 24px. At 768px and below the sidebar becomes the header's Menu, the toolbar scrolls with the page, the email is hidden and the page padding is 16px. At 700px and below, card grids collapse to one column, forms stack labels over fields, and wide tables either stack their rows (`table.stack`) or scroll sideways inside their own box, never the page.

## Elevation & Depth

Flat. There are no shadows anywhere. Depth is tonal: a panel (white / slate) on a slightly darker floor, outlined by a 1px Rule Line, all framed by the dark chrome. Highlighted rows (a rule preview's matches) get a 10% blue wash; ongoing warning incidents a 9% amber wash; diff lines get 15% green or red washes; the active sidebar section a 9% gold wash.

### Named Rules
**The Flat Panel Rule.** Surfaces never lift. If something needs to stand out, give it a state color or move it up the page, not a shadow.

## Shapes

Gently rounded rectangles. Small controls (buttons, toolbar buttons, inputs, chip fields, banners, logs, diffs) use a 6px radius; cards and tiles use 8px; tags and chips are full pills. Tables are square-cornered. Borders are always 1px: Rule Line, or the state color for a container in a state (below).

**State markers.** A container in a state takes the state color on its whole 1px border (warning cards, banners, the verdict); never a thick colored edge on one side. Inside, the state is said in words: a bold 13px state word ("error", "warning", "down", "note") before the line, so color is never the only signal. A container's problems sit in a block with a 10% wash of its state color (`color-mix`), 6px radius, 8px × 12px padding. Ongoing incident rows get the same wash across the row.

### Named Rules
**The Whole Border Rule.** State is an outline, a fill or a wash, never a stripe. The 4px left border was retired in October 2026. The sidebar's active section is a wash, not a stripe, so the rule holds there with no exception needed.

## Components

### Buttons
Plain and decisive: one filled button per decision, everything else outlined.
- **Shape:** gently rounded (6px), 1px border.
- **Primary:** Primary (Ink) fill and border, On Primary text, 600 weight, 6px × 14px padding. Used for Run, Apply and form submits. Never amber.
- **Secondary:** transparent fill, Ink text, Rule Line border. Cancel, Test, Preview, row actions.
- **Danger:** Alarm Red fill and border, white text. Only for the final click of something destructive or live: deleting files, a rule or a schedule, removing downloads, turning on auto-approve, running or saving with live settings.
- **Focus:** 2px Ink outline, 2px offset, the same ring on links, summaries and fields. **Disabled:** 40% opacity. No hover transform or transition.
- **Labels are words:** "Up", "Down", "Edit", never arrow glyphs standing in for icons. An icon may sit beside the word, never replace it.
- Links styled as buttons (`.button`) look identical, so a confirmation step can be a plain GET link.

### Toolbar
The page's actions, the way the *arrs show them. A strip 60px tall on Panel with a bottom Rule Line, sticky under the header on desktop and scrolling with the page on a phone.
- **Buttons:** an icon (20px) over a 12px label, Ink, no fill or border, at least 64px wide (56px on a phone), 6px radius. Hover is a 7% Ink wash; focus is a 2px Ink outline inset; disabled is 40% opacity. Monochrome: no state color, no primary fill.
- **Groups:** related actions sit together; groups are split by a 1px Rule Line. A last group may sit at the right end.
- **Save:** a settings page's Save is in the toolbar, 600 weight, submitting its form with `form=`. Forms that edit a rule or a connection keep their own buttons next to their fields.

### Icons
A Lucide SVG sprite (ISC), self-hosted, drawn with `<use>`: 18px (20px in the toolbar), 2px stroke, round caps, `currentColor`, no fill. Every icon sits next to a word and is hidden from screen readers. Helperr's own mark is a gold rounded square with a dark "H", 28px in the header.

### Badges
A count on a sidebar section, from the database only. A 20px pill, 12px 600 tabular numbers; the label is in visually hidden text and the tooltip. Signal Blue (`--info-fill`) with white text for work to do, Gold with dark text for warn, Alarm Fill with white text for bad.
- **Status:** services with open health incidents, in the worst level's color.
- **Activity:** removers plans to review plus runs failed in the last 24 hours; red when any failed, blue otherwise.

### Tags
- **Style:** pill, 1px Rule Line border, 12px text, transparent fill.
- **State:** the border and text take the state color: LIVE in Alarm Red, warnings in Projector Amber (text in Warning Ink), ok in Signal Green, plan for review in Signal Blue. Dry run and "not set up" use the neutral tag.
- **Solid:** a state that needs reading now is a filled pill, 600 weight: Alarm Fill with white text (`down`, `error`, an ongoing error incident), Projector Amber with On Amber text (`warning`, an ongoing warning).

### Cards / Containers
- **Corner Style:** 8px.
- **Background:** Panel on Console Floor.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** 1px Rule Line.
- **Internal Padding:** 14px × 16px, children 8px apart; card actions pin to the bottom.

### Tables
The main way data is shown. Panel background, 1px outer border and row rules, 8px × 12px cells, left-aligned. Headers are Label type in Dim Ink and never wrap. Row actions sit right-aligned and never wrap. Status words in cells are 600 weight in their state color, written as words ("dry run", not `dry_run`). Cells keep their words whole; a cell that can hold a long name, path or text is `.long` (breaks anywhere, at least 8rem), so a release name never widens the page and never squeezes an id or count. Below 700px a table scrolls inside its own box, except the ones you read on a phone (`table.stack`: Recent runs, a removers plan, both Requests tables), whose rows stack: the name leads in 600 with its status at the right, then the other cells in 13px with their column's name before them in Dim Ink. The Jobs, Fix these and incidents tables have their own stacked layouts.

### Inputs / Fields
- **Style:** Panel fill, 1px Rule Line border, 6px radius, 6px × 10px padding, body font; number inputs max 160px.
- **Chip lists:** list fields (one value per line in a textarea) become a row of pill chips with an inline input when JavaScript runs; the field gets a 2px Signal Blue outline while focused.
- **Errors:** listed in Alarm Red above the form; hints sit under the field in 13px Dim Ink.

### Navigation
- **Header:** Chrome, 56px, in both themes. The mark and "Helperr" in bold 16px Gold, linking to Status; at the right a solid state pill ("Sonarr needs you", "2 services need you") linking to Status, absent when nothing needs you; then the signed-in email in 13px Chrome Muted. A skip link to the content appears on focus.
- **Sidebar:** Chrome Side, 212px. Six sections, each an 18px icon and a label: Status, Activity (Runs, Schedules), Removers, Requests, Playback, Settings (General, Connections, Radarr, Sonarr, Empty trash, Webhook, History). Links are Chrome Text, white on Chrome Hover. Only the section holding the current page shows its sub pages (13px Chrome Muted, indented under the label). The active section is Gold text on a 9% gold wash; its current sub page is Gold at 600.
- **Settings:** the sidebar's Settings section replaces the old settings tabs.
- **Phone (768px and below):** the sidebar becomes a `<details>` "Menu" in the header, opening the same list under it; no script needed.

### Jobs Table
Runs lists its jobs the way the *arrs' System → Tasks does: a "Jobs" table with the job (name in 600, a 13px Dim Ink description under it), Mode (a tag: dry run, live, mixed, plan for review), Next (the scheduled time and schedule, or the follow-up empty trash), Last run (number, status word, time or "Review the plan"), and right-aligned Dry run and Run… secondary buttons. Below 700px it stacks one job per block.

### Share Bars
A number's share drawn after it: a native `<meter>` (the CSP allows no inline widths), 56px × 6px, a pill track of 12% Ink, hidden from screen readers since the number beside it says it. Signal Blue is data (finished requests); a rate you want low (transcoded plays, `low=25 high=50 optimum=0`) turns Projector Amber past half. Used in the Requests users table and Playback's transcode columns, never as decoration or alone without the number.

### Dialogs
Every confirmation (run a job, apply a plan, turn on auto-approve, make a job live) is a page laid out like an *arr dialog, not a modal: no script, and Back works. A Panel up to 680px wide (960px when it lists a plan), centered in the page, 8px radius. The head holds the question as the `h1` ("Run Movers & profilers?") with the mode tag at the right; the body says what will happen in a banner; the foot, on Console Floor behind a 1px rule, holds the buttons at the right: Cancel, then any safer alternative ("Dry run instead"), then the decision last. The decision's label names its consequence ("Run live", "Plan removals", "Delete 13 files", "Delete without review", "Save and go live"), never "OK" or "Run with the settings"; it's Danger when it changes or deletes things. On a phone the buttons stack full width, the decision on top.

### Mode Tags
A job's or run's mode has one name and one tag everywhere (Runs, a run's page, Schedules, dialogs): dry run (neutral), live (Alarm Red), live for some *arrs (amber), plan for review (Signal Blue), review, auto-approve on (amber), apply plan (Alarm Red). A dry run says why in Dim Ink after it ("forced", "from the settings"). "settings" is never a mode.

### Banners
A full-width notice under the page heading: 1px border, 6px radius, 10px × 14px padding. A warning or error banner takes the state color on its whole border over a 7–8% wash of it; a success banner has a green border on Panel.

### Status Verdict
The first thing on the Status page: a Panel box, 16px text, one sentence. Healthy: "All N connected services are OK." on a neutral border. Otherwise: "N services need you:" in bold, then one link per service ("Sonarr: 2 warnings, 3 stuck, for 2d 4h") in Ink, 600 weight, underlined, with the border in the worst state's color. Nothing connected: a sentence linking to Connections.

### Service Lines
Healthy and unconnected services, as rows of one Panel with 1px rules between them. Each row is a grid (disclosure chevron 8px, name and version 14rem, state 7rem, facts): name 600 in Ink, version, state and facts in Dim Ink, facts wrapping to a second line when long, never clipped. A healthy row is a `<details>` that opens to its facts, items and notes; its chevron is drawn in CSS. An unconnected row says "not connected" and links to its Connections form. Below 700px the facts sit under the name.

### State Cards
A service that needs you: a card with its whole border in the state color, a filled state tag in the header, then the problem block (problems with state words and how long each has lasted), then its facts and items. A down card's block says why and links to that service's connection form. Cards sit in an auto-fit grid (340px minimum), aligned to the top, never stretched to equal height.

### Fix List
Playback leads with what to change, not rates: a verdict sentence ("178 of 466 plays transcoded (38%). 5 devices account for 77% of them."), then a "Fix these" table with one row per person and device (a setting lives on one app on one device), worst first, ending in the change: the cause and current value, a Dim Ink arrow, and the target in bold. It stops once it covers three quarters of the transcodes. Below 700px a row stacks: name and count, the device in Dim Ink, the change.

### Folds
Detail sections a page keeps for digging in (Playback's by user, by device, formats, recent) are closed `<details>`: the h2 in the summary with its count in Dim Ink after it, and the same CSS chevron as Service Lines. Empty ones are left out rather than shown as an empty fold.

### Logs and Diffs
Panel boxes in 12px monospace, capped at 60% of the viewport height with their own scroll. Diffs color whole lines with a 15% green or red wash.

## Do's and Don'ts

### Do:
- **Do** use the CSS custom properties (`--bg`, `--surface`, `--text`, `--muted`, `--border`, `--accent`, `--ok`, `--bad`) and add both a light and a dark value for any new one.
- **Do** keep pages working without JavaScript and within the CSP: one stylesheet, self-hosted assets, no inline styles or scripts.
- **Do** put the page's actions in the toolbar as icon-over-label buttons; keep a form's own buttons next to its fields.
- **Do** show dry run vs live with a tag next to the job or run, every time.
- **Do** use tables for any list of more than a few comparable items, with tabular numbers and thousands separators.
- **Do** say every state in a word next to its color, and let long names and paths wrap (`overflow-wrap: anywhere` in cards, `min-width: 0` on grid children); set release names and paths in 12px mono.
- **Do** collapse what's healthy: a healthy thing is one dim line, a thing that needs you is a card.

### Don't:
- **Don't** use green, amber or red for anything but state; gold also marks the brand and where you are. The primary action is Ink, never amber.
- **Don't** add shadows, gradients, background images or decorative illustration.
- **Don't** mark state with a colored side border (`border-left` wider than 1px); use the whole border, a fill or a wash.
- **Don't** add web fonts or icon fonts; system fonts only, and icons from the self-hosted SVG sprite, always next to a word.
- **Don't** set warning text in Projector Amber; use Warning Ink.
- **Don't** reload a page on a timer without a way to pause it (`?refresh=0` on Status).
- **Don't** use the danger button for anything reversible or dry; a dry run's Run button is the plain primary.
