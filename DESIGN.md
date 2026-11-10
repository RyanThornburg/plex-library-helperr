---
name: Helperr
description: A quiet control room for a Plex server, where color means something needs you.
colors:
  projector-amber: "#c7790b"
  projector-amber-dark: "#e5a00d"
  gold: "#e5a00d"
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
  caption:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 400
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
  frame: "20px"
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
  button-danger-dark:
    backgroundColor: "{colors.alarm-fill-dark}"
    textColor: "{colors.panel}"
    rounded: "{rounded.sm}"
    padding: "6px 14px"
  input:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "6px 10px"
  filter-control:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "36px"
  card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "14px 16px"
  banner:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "10px 14px"
  verdict:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "12px 16px"
  problem-block:
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  dialog:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    width: "680px"
  dialog-wide:
    width: "960px"
  dialog-head:
    padding: "14px 20px"
  dialog-body:
    padding: "16px 20px 20px"
  dialog-foot:
    backgroundColor: "{colors.console-floor}"
    padding: "12px 20px"
  tag:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  tag-live:
    textColor: "{colors.alarm-red}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  tag-warn:
    textColor: "{colors.warning-ink}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  tag-info:
    textColor: "{colors.signal-blue}"
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
  table-cell:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    padding: "8px 12px"
  table-stack-row:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    padding: "10px 12px"
  share-bar:
    backgroundColor: "{colors.signal-blue}"
    rounded: "{rounded.pill}"
    width: "56px"
    height: "6px"
  appbar:
    backgroundColor: "{colors.chrome}"
    textColor: "{colors.chrome-text}"
    height: "56px"
    padding: "0 20px"
  logo:
    textColor: "{colors.gold}"
    typography: "{typography.title}"
    size: "28px"
  sidebar:
    backgroundColor: "{colors.chrome-side}"
    width: "212px"
  sidebar-item:
    backgroundColor: "{colors.chrome-side}"
    textColor: "{colors.chrome-text}"
    padding: "9px 16px 9px 20px"
  sidebar-item-hover:
    backgroundColor: "{colors.chrome-hover}"
    textColor: "{colors.panel}"
  sidebar-item-active:
    backgroundColor: "color-mix(in srgb, #e5a00d 9%, transparent)"
    textColor: "{colors.gold}"
    padding: "9px 16px 9px 20px"
  sidebar-subitem:
    backgroundColor: "{colors.chrome-side}"
    textColor: "{colors.chrome-muted}"
    padding: "6px 16px 6px 50px"
  toolbar:
    backgroundColor: "{colors.panel}"
    height: "60px"
    padding: "0 16px"
  toolbar-button:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.caption}"
    rounded: "{rounded.sm}"
    padding: "6px 10px"
    width: "64px"
  badge-warn:
    backgroundColor: "{colors.gold}"
    textColor: "{colors.on-amber}"
    rounded: "{rounded.pill}"
    padding: "0 6px"
    height: "20px"
  badge-info:
    backgroundColor: "{colors.signal-blue}"
    textColor: "{colors.panel}"
    rounded: "{rounded.pill}"
    padding: "0 6px"
    height: "20px"
  badge-bad:
    backgroundColor: "{colors.alarm-red}"
    textColor: "{colors.panel}"
    rounded: "{rounded.pill}"
    padding: "0 6px"
    height: "20px"
  badge-bad-dark:
    backgroundColor: "{colors.alarm-fill-dark}"
    textColor: "{colors.panel}"
    rounded: "{rounded.pill}"
    padding: "0 6px"
    height: "20px"
---

# Design System: Helperr

## Overview

**Creative North Star: "The Control Room"**

Helperr is the room you walk into to check that the server is fine. When everything is healthy it is calm: cool grey floors, white (or slate) panels, black ink, thin rule lines, inside a dark frame. Color is reserved for meaning. Green says a thing worked, amber says look at this, red says something failed or something will be deleted, and blue, as in Sonarr and Radarr, is a link, something in progress, or a number's share; blue is never a problem. An operator scanning the screen should be able to find every problem by color alone, and a screen with no problems should have almost no color on it.

It is a dense, expert surface for one admin at a desktop browser, readable on a phone. Information is laid out as tables, fact lists and small cards, not prose; numbers are tabular; logs, paths, release names and cron expressions are monospace. There is no imagery, no illustration, no motion and no shadow; the only pictures are small monochrome line icons, each next to a word. Depth comes from a panel sitting on a slightly darker floor, separated by a 1px rule. Light and dark themes follow the operating system (`prefers-color-scheme`); every themed color has a dark counterpart.

The shell is modelled on Sonarr and Radarr, so Helperr reads as a sibling *arr: a dark header and a dark sidebar in both themes, and a toolbar of icon-over-label actions above each page. The chrome frames the board; it carries no state color except the badges and the header pill that say something needs you, and Gold for the brand and where you are. The Status page is the reference for the idea: a one-line verdict first, then a card for each service that needs you, then every healthy or unconnected service as one dim line.

The look is system-native on purpose: system fonts, native form controls, native `<details>`, `<progress>` and checkboxes, so pages render instantly, work without JavaScript, and satisfy a strict CSP with nothing to load.

**Key Characteristics:**
- Neutral at rest; color only marks state (ok / warn / bad), blue marks links, progress and data, and Gold marks the brand and where you are. A healthy service has no state color at all.
- A dark *arr-style frame in both themes: 56px header, 212px sidebar, then a toolbar of the page's actions.
- Flat: panels on a floor, 1px rules, no shadows, no motion.
- System fonts, one body size (15px), one small size (13px), a short heading scale (20 / 16px).
- Tables and definition lists over cards; cards group a service, never decorate.
- Confirmations are pages laid out as *arr dialogs, the decision last and named by its consequence.
- Light and dark themes from the OS, no theme toggle.

## Colors

A cool slate neutral scale with three signal colors and the *arrs' blue, inside a dark slate chrome; amber also carries the brand as Gold.

### Primary
- **Primary** (`--primary` / `--on-primary`): the primary button. Ink fill with Panel text in light mode; Ink (dark) with Console Floor (dark) text in dark mode. Ink, so gold keeps meaning brand, here and warning. Run, Apply, Save and form submits use it.
- **Gold** (`gold`, `--gold`, the same in both themes): the chrome's brand color. The "H" mark and the Helperr wordmark, the active sidebar section and its current sub page, focus rings inside the chrome, and the warning badge. Never on the board as decoration and never a button.
- **Projector Amber** (`projector-amber` / `projector-amber-dark`, `--accent`): the warning color for borders, fills and washes: a warning card's and banner's outline, the filled `warning` tag, the warning verdict's border, an ongoing warning incident's row, and a share bar past half. The dark value is Plex's gold.
- **Warning Ink** (`warning-ink`, `--warn-text`; dark mode uses Projector Amber): amber as text. Warning state words, `tag-warn` labels, blocked statuses and transcode decisions use it, because Projector Amber on white is only about 3.4:1.

### Secondary
- **Signal Blue** (`signal-blue` / `signal-blue-dark`, `--info`; `--info-fill` is the light value in both themes, behind white text): the *arrs' blue, never a problem. Running and queued run statuses, started requests, the plan-for-review tag, the Activity badge when it counts plans only, `note` state words on Status, checkbox/radio/progress accents (`accent-color`), text selection (30% wash), chip-field focus, the rule preview's highlighted rows (10% wash), and share bars. About 5.5:1 on white light, 6.5:1 on the dark floor (Sonarr's blue).
- **Link Ink** (`link-ink`, `--link`): Signal Blue in both themes, underlined 2px below the text. A fold's heading turns Link Ink under the pointer.
- **Signal Green** (`signal-green` / `signal-green-dark`, `--ok`): success only. Succeeded and done statuses, direct play, finished requests, the saved banner's border, added diff lines.
- **Alarm Red** (`alarm-red` / `alarm-red-dark`, `--bad`): failure and destruction. Failed runs, down services, error state words, error lists, the `live` tag and an auto-approve `on` tag, the error banner's and card's border, an ongoing error incident's row, removed diff lines, a chip's remove button under the pointer.
- **Alarm Fill** (`--bad-fill`: Alarm Red in light mode, `alarm-fill-dark` in dark): red behind white text (the danger button, the filled `down`/`error` tags, the bad badge and pill). Dark mode's Alarm Red is too light for white text (3.4:1); Alarm Fill is 4.6:1.

### Neutral
- **Console Floor** (`console-floor` / `console-floor-dark`, `--bg`): the page background behind every panel, and a dialog's foot.
- **Panel** (`panel` / `panel-dark`, `--surface`): cards, tables, the toolbar, inputs, banners, the verdict, service lines, dialogs, logs, diffs.
- **Ink** (`ink` / `ink-dark`, `--text`): all primary text, the primary button's fill, secondary-button and toolbar text, focus outlines on the board, a not-started request.
- **Dim Ink** (`dim-ink` / `dim-ink-dark`, `--muted`): table headers, fact labels, hints, empty states, dry-run statuses, a healthy service line's state and facts, notes after a mode tag, the CSS chevrons.
- **Rule Line** (`rule-line` / `rule-line-dark`, `--border`): every border and divider, 1px, including the neutral tag's outline and the toolbar's group dividers.
- **On Amber** (`on-amber`, `--on-accent`): text on the filled warning tag and the warning badge, in both themes.

### Chrome
Dark in both themes, a step darker in dark mode so the frame still sits below the page. Chrome Text and Chrome Muted keep one value in both themes.
- **Chrome** (`chrome` / `chrome-dark`, `--chrome`): the header.
- **Chrome Side** (`chrome-side` / `chrome-side-dark`, `--chrome-side`): the sidebar, full page height.
- **Chrome Hover** (`chrome-hover` / `chrome-hover-dark`, `--chrome-hover`): a sidebar link under the pointer (its text turns white, `--chrome-strong`), the open phone Menu.
- **Chrome Text** (`chrome-text`, `--chrome-text`): sidebar section labels and icons, the Menu button.
- **Chrome Muted** (`chrome-muted`, `--chrome-muted`): sidebar sub pages and the signed-in email.

### Named Rules
**The Quiet Board Rule.** A healthy screen is almost colorless. Green, amber and red appear only where a state needs reading; blue only on links, things in progress, selection and data. Never use any of them for decoration, section color-coding or emphasis. Waiting isn't failing: a request not started yet is Ink, not red. Gold in the chrome marks the brand and where you are, nothing else.

**The Red Means Real Rule.** Alarm Red is for failures and for anything that deletes or goes live. A dry run is never red; it's Dim Ink.

**The Amber Contrast Caveat.** Light-mode Projector Amber on white is about 3.4:1, below AA for body-size text. Warning text uses Warning Ink; Projector Amber stays for fills, borders and washes; Gold sits on the dark chrome, where it reads.

## Typography

**Body Font:** system-ui (with -apple-system, Segoe UI, sans-serif)
**Mono Font:** ui-monospace (with SFMono-Regular, Menlo, Consolas, monospace)

**Character:** The operating system's own UI font, so Helperr reads like a native tool. Monospace is for machine text: logs, file lists, diffs, release names, paths, cron expressions, API keys.

### Hierarchy
- **Headline** (700, 20px, 1.5): the page `h1`, one per page; a dialog's question is its `h1`.
- **Title** (700, 16px, 1.5): `h2` section, card and fold titles; the Status verdict and the wordmark are 16px too.
- **Body** (400, 15px, 1.5): everything else, including table cells, fields and filter controls.
- **Label** (600, 13px, 1.5): table headers. Form labels and fieldset legends are 600 at body size. At 400, 13px carries hints, card subheadings (`h3`, Dim Ink), job descriptions, state words (600), sidebar sub pages, the email, a stacked row's secondary cells, and inline `code`.
- **Caption** (400, 12px): tags, badges (600) and toolbar labels.
- **Mono** (400, 12px, 1.45): logs and release names; file lists and diffs at 1.5; inline `code` at 13px.

### Named Rules
**The Two Sizes Rule.** Text is 15px or 13px; headings (20 / 16px, which the verdict and wordmark share), and 12px tags, badges, toolbar labels and mono blocks, are the only exceptions. Don't introduce 14px, 17px or 18px.

**The Tabular Numbers Rule.** Any number meant to be compared (counts, sizes, metrics, dates in dense tables) uses tabular numerals.

## Layout

An app shell on a CSS grid: a 56px header across the top, a 212px sidebar down the left, then the page's toolbar (at least 60px) and the page under it. The header, the toolbar and the sidebar's list are sticky on desktop; anchors land below the header and toolbar (132px scroll padding). The page is fluid up to 1600px, left aligned next to the sidebar, with 20px top, 24px side and 40px bottom padding. Dim explanation paragraphs under a page or card heading are capped at 75ch. Pages flow top to bottom: the `h1`, banners, then cards or tables, with 24px above each section heading.

Card groups are auto-fit grids with their own minimums: status cards 340px and connection forms 420px (both aligned to the top, never stretched to a neighbour's height), stuck-download options 280px; 12px gaps. Forms are a two-column grid (labels at max-content, fields up to 520px) with 12px × 16px gaps; fact lists use the same two-column `dt`/`dd` grid at 4px × 16px. Filter bars are a wrapping row of labelled controls, 8px × 16px apart, every control 36px tall.

Spacing steps are 4, 8, 12, 16 and 24px, with 20px as the frame inset (header sides, page top, dialog sides). At 768px and below the sidebar becomes the header's Menu, the toolbar scrolls with the page, the email is hidden, the header padding is 12px and the page padding 16px. At 700px and below, card grids collapse to one column, forms stack labels over fields, dialog buttons stack, and wide tables either stack their rows or scroll sideways inside their own box, never the page. Long words (release names, paths, URLs) wrap anywhere on the page; table headers, buttons, tags and status words never wrap.

## Elevation & Depth

Flat. There are no shadows, gradients or transitions anywhere. Depth is tonal: a panel (white / slate) on a slightly darker floor, outlined by a 1px Rule Line, all framed by the dark chrome; the sticky header and toolbar sit above the page only by stacking order. Emphasis is a wash of a token color via `color-mix`: the active sidebar section 9% Gold, a toolbar button under the pointer 7% Ink, a service line under the pointer 4% Ink, highlighted rule-preview rows 10% Signal Blue, a problem block 10% of its card's state, ongoing incident rows 7% Alarm Red or 9% Projector Amber, warning and error banners 8% and 7%, diff lines 15% green or red.

### Named Rules
**The Flat Panel Rule.** Surfaces never lift. If something needs to stand out, give it a state color or move it up the page, not a shadow.

## Shapes

Gently rounded rectangles. Small controls and inset blocks (buttons, toolbar buttons, inputs, chip fields, banners, problem blocks, logs, diffs) use a 6px radius; cards, the verdict, service-line panels and dialogs use 8px; tags, chips, badges and share bars are full pills (999px). Tables are square-cornered. Borders are always 1px: Rule Line, or the state color for a container in a state. The only drawn shape is the disclosure chevron: two 1.5px Dim Ink strokes of a 6px square, rotated, on service lines and folds.

**State markers.** A container in a state takes the state color on its whole 1px border (warning and error cards, banners, the verdict); never a thick colored edge on one side. Inside, the state is said in words: a bold 13px state word ("error", "warning", "down", "note") before the line, so color is never the only signal.

### Named Rules
**The Whole Border Rule.** State is an outline, a fill or a wash, never a stripe. The 4px left border was retired in October 2026. The sidebar's active section is a wash, not a stripe, so the rule holds there with no exception needed.

## Components

### Buttons
Plain and decisive: one filled button per decision, everything else outlined.
- **Shape:** gently rounded (6px), 1px border, 600 weight, 6px × 14px padding. Links styled as buttons look identical, so a confirmation step can be a plain GET link.
- **Primary:** Primary (Ink) fill and border, On Primary text. Run, Start dry run, Plan removals, Save and form submits. Never amber.
- **Secondary:** transparent fill, Ink text, Rule Line border. Cancel, Dry run, Run…, Test, Preview, Edit, row actions.
- **Danger:** Alarm Fill fill and border, white text. Only the final click of something destructive or live: deleting files, a rule or a schedule, removing downloads, auto-approving a remover, running live or partly live, saving a change that goes live.
- **Focus:** 2px Ink outline, 2px offset, the same ring on links, summaries and fields. **Disabled:** 40% opacity. No hover transform or transition.
- **Labels are words:** "Edit", "Turn off", "Delete 13 files"; an icon may sit beside the word, never replace it.

### Toolbar
The page's actions, the way the *arrs show them. A strip at least 60px tall on Panel with a bottom Rule Line, 16px side padding (8px on a phone), sticky under the header on desktop and scrolling with the page on a phone, and scrolling sideways when it overflows. It is left out when a page has no actions.
- **Buttons:** a 20px icon over a 12px label, Ink, no fill or border, at least 64px wide (56px on a phone), 6px radius, 3px between icon and label. Hover is a 7% Ink wash; focus is a 2px Ink outline inset; disabled is 40% opacity. Monochrome: no state color, no primary fill.
- **Groups:** related actions sit together 2px apart; groups are split by a 1px Rule Line with 8px either side. A last group may sit at the right end, without a divider.
- **Save:** a settings page's Save is in the toolbar at 600 weight, submitting its form with `form=`. Forms that edit a rule or a connection keep their own buttons next to their fields.

### Icons
A Lucide SVG sprite (ISC), self-hosted, drawn with `<use>`: 18px (20px in the toolbar), 2px stroke, round caps and joins, `currentColor`, no fill. Every icon sits next to a word and is hidden from screen readers. Helperr's own mark is a Gold square with 7px corners and a Chrome "H", 28px in the header.

### Navigation
- **Header:** Chrome, 56px, 20px side padding, in both themes. The mark and "Helperr" in bold 16px Gold, linking to Status; at the right a solid state pill (`warning` or `error` fill: "Sonarr needs you", "2 services need you") linking to Status, absent when nothing needs you; then the signed-in email in 13px Chrome Muted. A skip link to the content appears on focus. Focus inside the chrome is a 2px Gold outline.
- **Sidebar:** Chrome Side, 212px, its list sticky below the header. Six sections, each an 18px icon and a label, 12px apart: Status (Incident history; Radarr queue and Sonarr queue while they have stuck downloads), Activity (Runs, Schedules, Events, Log files), Removers, Requests, Playback, Settings (General, Connections, Radarr, Sonarr, Empty trash, Webhook, History). Links are Chrome Text, white on Chrome Hover. Only the section holding the current page shows its sub pages (13px Chrome Muted, indented to 50px). The open section is Gold text on a 9% Gold wash; its current sub page is Gold at 600.
- **Settings:** the sidebar's Settings section is the settings sub-navigation; there are no tabs. Each settings page sets its own `h1`.
- **Phone (768px and below):** the sidebar becomes a `<details>` "Menu" (icon and 13px label) at the left of the header, opening the same list full width under it; no script needed.

### Badges
A count on a sidebar section, read from the database only. A 20px pill, at least 20px wide, 6px side padding, 12px 600 tabular numbers; the label is in visually hidden text and the tooltip.
- **Status:** services with open health incidents: Gold with On Amber text when the worst is a warning, Alarm Fill with white text when any is an error.
- **Activity:** removers plans to review plus runs failed in the last 24 hours: Signal Blue (`--info-fill`) with white text for plans only, Alarm Fill when any run failed.

### Tags
- **Style:** pill, 1px Rule Line border, 1px × 8px padding, 12px text, transparent fill. Neutral for facts with no state: dry run, off, paused, current, connected, not set up, movie, muted.
- **State:** the border and text take a color: `tag-live` Alarm Red (live, live for some *arrs, review with auto-approve on, apply plan, a remover's auto-approve on), `tag-warn` Projector Amber border with Warning Ink text (not by removers), `tag-info` Signal Blue (plan for review).
- **Solid:** a state that needs reading now is a filled pill at 600: Alarm Fill with white text (`down`, `error`, an ongoing error incident, the error pill), Projector Amber with On Amber text (`warning`, an ongoing warning, the warning pill).

### Mode Tags
A job's or run's mode has one name and one tag everywhere (the Jobs table, Recent runs, a run's page, Schedules, dialogs), from one table in the code: dry run (neutral), live (red), live for some *arrs (red), plan for review (blue), review, auto-approve on (red; on the Jobs table it reads "review, auto-approve on for N" and links to Removers), apply plan (red), unknown (neutral). A dry run may say why in Dim Ink after it ("forced", "from the settings"). "settings" is never a mode.

### Cards / Containers
- **Corner Style:** 8px.
- **Background:** Panel on Console Floor.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** 1px Rule Line; the state color on the whole border when the card is in a state.
- **Internal Padding:** 14px × 16px, children 8px apart in a column; card actions pin to the bottom.

### Banners
A notice under the page heading, 12px above the next thing: 1px border, 6px radius, 10px × 14px padding, Panel fill. A warning banner has a Projector Amber border over an 8% amber wash, an error banner an Alarm Red border over a 7% red wash; a saved banner has a green border on Panel; a plain banner states a fact. A list of form errors is an error banner in Alarm Red text.

### Tables
The main way data is shown. Full width, Panel background, 1px outer border and row rules, 8px × 12px cells, left-aligned. Headers are Label type in Dim Ink and never wrap. Row actions sit right-aligned and never wrap. Status words in cells are 600 in their state color, written as words ("dry run", not `dry_run`); a request's progress is a word in its color (finished green, started blue, not started Ink, unavailable and unmatched Dim Ink). Cells keep their words whole; a cell that can hold a long name, path or text breaks anywhere and is at least 8rem, so a release name never widens the page and never squeezes an id or count. A count that opens a list (a plan's files, a change's items) is a native `<details>` whose summary says what it counts ("9 files", "1 item") and stays on one line. Times read to the minute in the browser's time zone, the seconds in their tooltip; an action column's header is visually hidden text ("Actions").

### Stacked Tables
Below 700px a table scrolls inside its own box, except the ones you read on a phone (Recent runs, a removers plan and its apply dialog, both Requests tables), whose rows stack. The header row hides; each row is a wrapping flex block, 10px × 12px, split by a Rule Line. Leading cells (a checkbox, a run number) come first at 15px; the name follows in 600, taking the rest of the line, with its status at the right at 15px; then the other cells in 13px, each with its column's name before it in Dim Ink; a cell marked to own a line takes the full width; empty cells disappear. The Jobs, Fix these and incidents tables have their own stacked layouts: a job per block with its buttons last; who and count, then the device in Dim Ink, then the change; service and cleared, then the problem, then started and lasted.

### Jobs Table
Runs lists its jobs the way the *arrs' System → Tasks does: a "Jobs" table with the job (name in 600, a 13px Dim Ink description under it), Mode (a mode tag), Next (the scheduled time and schedule, the follow-up empty trash, or "Not scheduled" in Dim Ink), Last run (number, status word, then the time or "Review the plan"), and right-aligned Dry run and Run… secondary buttons, or "Queued or running…" in Dim Ink.

### Dialogs
Every confirmation (run a job, apply a plan, auto-approve a remover, make a job live) is a page laid out like an *arr dialog, not a modal: no script, and Back works. A Panel up to 680px wide (960px when it lists a plan), centered, 8px radius, 1px Rule Line.
- **Head:** the question as the `h1` ("Run Movers & profilers?", "Apply plan #12?", "Make this job live?") with the mode tag at the right, 14px × 20px, a Rule Line under it.
- **Body:** what will happen, in a banner (`banner-live`, the error banner's red, when it changes or deletes things, so banner, mode tag and decision button agree; error when it can't go ahead), with any list of what's affected; 16px × 20px (20px at the bottom), 12px between blocks.
- **Foot:** on Console Floor behind a Rule Line, 12px × 20px, buttons 8px apart at the right: Cancel, then any safer alternative ("Dry run instead"), then the decision last. The decision's label names its consequence ("Start dry run", "Run live", "Run partly live", "Plan removals", "Run and delete auto-approved", "Delete 13 files", "Delete without review", "Save and go live"), never "OK"; it's Danger when it changes or deletes things.
- **Phone:** 16px side padding, the head wraps, the buttons stack full width with the decision on top and Cancel last.

### Status Verdict
The first thing on the Status page: a Panel box, 8px radius, 12px × 16px, 16px text, one sentence. Healthy: "All N connected services are OK." on a Rule Line border. Otherwise: "N services need you:" in bold, then one link per service ("Sonarr: 2 warnings, 3 stuck, for 2d 4h"), separated by a Dim Ink dot, in Ink at 600 and underlined, with the border in the worst state's color. Nothing connected: a sentence linking to Connections. Playback opens with a verdict box too ("178 of 466 plays transcoded (38%)").

### State Cards
A service that needs you: a card with its whole border in the state color, its name (and Dim Ink version) with a filled state tag in the header, then the problem block (6px radius, 8px × 12px, a 10% wash of the state: each problem with its state word and how long it has lasted), then its facts and items, then its links. A down card's block says why and links to that service's connection form. A problem can be muted in this browser with an underlined Dim Ink text button; a card with everything muted turns neutral (Rule Line border, neutral wash, a `muted` tag). Cards sit in an auto-fit grid (340px minimum), aligned to the top, never stretched to equal height.

### Service Lines
Healthy and unconnected services, as rows of one Panel (8px radius) with Rule Lines between them. Each row is a grid, 12px gaps, 10px × 16px: the chevron (8px), name and version (up to 14rem, 600 Ink, version in 13px Dim Ink), state (7rem, 13px Dim Ink), facts (Dim Ink, tabular, joined by " · ", wrapping to more lines when long, never clipped) and the service's links (13px). A healthy row is a `<details>` that opens to its facts, items and notes, indented 48px; its chevron is drawn in CSS and the row takes a 4% Ink wash under the pointer. An unconnected row says "not connected" and links to its Connections form. Below 700px the facts sit under the name.

### Share Bars
A number's share drawn beside it: a 56px × 6px inline `<svg>` (`ui.share()`; the CSP allows no inline widths, but a rect's width attribute is markup, and Chrome's styled `<meter>` drew its fill half the track's height), a pill track of 12% Ink with a square-ended fill, hidden from screen readers since the number beside it says it. In a table it sits at the cell's start, vertically centred, with the number 80px in, so the bars line up down the column and never wrap; in a stacked phone row it follows its number, 8px after. Signal Blue is data (finished requests); a rate you want low (transcoded plays) turns Projector Amber past half. Used in the Requests users table and Playback's transcode columns, never as decoration or without the number.

### Fix List
Playback leads with what to change, not rates: its verdict, then a "Fix these" table with one row per person and device (a setting lives on one app on one device), worst first, its count as "N of M" in Warning Ink with a share bar, ending in the change: the cause and current value, a Dim Ink arrow, and the target in bold. It stops at 8 devices, once it covers three quarters of the transcodes, or below 3 transcodes.

### Folds
Detail sections a page keeps for digging in (Playback's causes, users, devices, formats, files, recent plays) are closed `<details>`: the `h2` in the summary with its count in Dim Ink after it, and the same CSS chevron as Service Lines. Consecutive closed folds sit 4px apart, 24px when one is open. Empty ones are left out rather than shown as an empty fold.

### Inputs / Fields
- **Style:** Panel fill, 1px Rule Line border, 6px radius, 6px × 10px padding, body font, full width of their column; number inputs max 160px. Cron and API key fields are monospace.
- **Filter bars:** each control under a 13px Dim Ink label, 36px tall, up to 16rem, 0 × 10px padding; the bar's buttons match the height.
- **Chip lists:** list fields (one value per line in a textarea) become a row of 13px pill chips with an inline input when JavaScript runs; the field gets a 2px Signal Blue outline while focused, and a chip's remove button turns Alarm Red under the pointer.
- **Checkboxes, radios and progress:** native, accented in Signal Blue. Choices sit in borderless fieldsets with a 600 legend.
- **Errors and hints:** errors are listed in an error banner above the form; hints sit under the field in 13px Dim Ink.

### Logs and Diffs
Panel boxes, 6px radius, in 12px monospace, capped at 60% of the viewport height with their own scroll. Logs wrap; diffs keep each line whole and color it with a 15% green or red wash, hunk headers in Dim Ink.

## Do's and Don'ts

### Do:
- **Do** use the CSS custom properties (`--bg`, `--surface`, `--text`, `--muted`, `--border`, `--accent`, `--warn-text`, `--info`, `--link`, `--ok`, `--bad`, `--bad-fill`, `--primary`, `--gold`, `--chrome*`) and give any new themed one both a light and a dark value.
- **Do** keep pages working without JavaScript and within the CSP: one stylesheet, self-hosted assets, no inline styles or scripts; use native `<details>` and `<progress>` where a script or inline width would otherwise be needed.
- **Do** put the page's actions in the toolbar as icon-over-label buttons; keep a form's own buttons next to its fields.
- **Do** show a job's or run's mode with its mode tag, using the one name from the modes table, every time.
- **Do** make every confirmation a dialog page: question as the heading, mode tag at the right, what happens in a banner, Cancel first and the decision last, named by its consequence.
- **Do** use tables for any list of more than a few comparable items, with tabular numbers and thousands separators; mark the tables you read on a phone to stack.
- **Do** say every state in a word next to its color, and let long names and paths wrap; set release names and paths in 12px mono.
- **Do** collapse what's healthy: a healthy thing is one dim line, a thing that needs you is a card.

### Don't:
- **Don't** use green, amber or red for anything but state, or blue for anything but links, progress, selection and data; Gold marks only the brand and where you are. The primary action is Ink, never amber.
- **Don't** color waiting as failure: a request not started yet is Ink, a dry run is Dim Ink.
- **Don't** add shadows, gradients, transitions, background images or decorative illustration.
- **Don't** mark state with a colored side border (`border-left` wider than 1px); use the whole border, a fill or a wash.
- **Don't** add web fonts or icon fonts; system fonts only, and icons from the self-hosted SVG sprite, always next to a word.
- **Don't** set warning text in Projector Amber; use Warning Ink.
- **Don't** reload a page on a timer without a way to pause it (`?refresh=0` on Status).
- **Don't** use the danger button for anything reversible or dry; a dry run's button is the plain primary.
- **Don't** call "settings" a mode, or label a decision "OK".
