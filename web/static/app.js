// served from /static so the CSP can keep inline scripts off

const pad = (n) => String(n).padStart(2, "0");

// "in 12 min", "3 hours ago", "yesterday": within a week, null past that
const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const relative = (d) => {
  const seconds = (d.getTime() - Date.now()) / 1000;
  const away = Math.abs(seconds);
  if (away < 45) return seconds < 0 ? "just now" : "in under a minute";
  if (away < 3600) return rtf.format(Math.round(seconds / 60), "minute");
  if (away < 86400) return rtf.format(Math.round(seconds / 3600), "hour");
  if (away < 7 * 86400) return rtf.format(Math.round(seconds / 86400), "day");
  return null;
};

// times are rendered in utc, show them in the browser's time zone: to the minute, the
// seconds in the title; data-date (dense tables) shows only the date, data-relative
// how long ago or how soon, kept current
const renderTimes = () => {
  for (const el of document.querySelectorAll("time[datetime]")) {
    const d = new Date(el.dateTime);
    if (Number.isNaN(d.getTime())) continue;
    const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const minutes = `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    el.title = `${minutes}:${pad(d.getSeconds())}`;
    const when = "relative" in el.dataset && relative(d);
    el.textContent = when || ("date" in el.dataset ? date : minutes);
  }
};
renderTimes();
if (document.querySelector("time[data-relative]")) setInterval(renderTimes, 30000);

// a new schedule starts in the browser's time zone
const zone = document.querySelector("select[data-browser-zone]");
if (zone) {
  const browser = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if ([...zone.options].some((option) => option.value === browser)) zone.value = browser;
}

// the from/to dates of a period filter only show for a custom period
const period = document.querySelector("select[data-period]");
if (period) {
  const toggle = () => {
    for (const field of period.form.querySelectorAll("[data-period-custom]")) {
      field.hidden = period.value !== "custom";
    }
  };
  period.addEventListener("change", toggle);
  toggle();
}

// a request's server picks which server's fields show (profile, root folder, tags and
// seasons are each server's own)
const serverPick = document.querySelector("select[data-server-pick]");
if (serverPick) {
  const show = () => {
    for (const group of serverPick.form.querySelectorAll("[data-server]")) {
      group.hidden = group.dataset.server !== serverPick.value;
    }
  };
  serverPick.addEventListener("change", show);
  show();
}

// a "select all" box in a table header toggles the named checkboxes in its form
for (const all of document.querySelectorAll("input[data-select-all]")) {
  const boxes = () => all.form.querySelectorAll(`input[name="${all.dataset.selectAll}"]`);
  all.addEventListener("change", () => {
    for (const box of boxes()) box.checked = all.checked;
  });
}

// a button with data-count="<name>" says how many of the named checkboxes are checked,
// from its data-count-one/data-count-many ("{n}" is the count)
for (const button of document.querySelectorAll("button[data-count]")) {
  const label = button.textContent;
  const update = () => {
    const n = button.form.querySelectorAll(`input[name="${button.dataset.count}"]:checked`).length;
    if (!n) button.textContent = label;
    else button.textContent = n === 1 ? button.dataset.countOne : button.dataset.countMany.replace("{n}", n);
  };
  button.form.addEventListener("change", update);
  update();
}

// a button with data-copy="<input id>" copies that input's value
for (const button of document.querySelectorAll("button[data-copy]")) {
  button.addEventListener("click", async () => {
    const input = document.getElementById(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(input.value);
      button.textContent = "Copied";
    } catch {
      input.select();
    }
  });
}

// a list field is a textarea with one value per line, shown as removable chips with an
// input that suggests values from its datalist. Without js the textarea still works
for (const area of document.querySelectorAll("textarea[data-chips]")) {
  const box = document.createElement("div");
  box.className = "chips";
  const input = document.createElement("input");
  input.id = area.id;
  area.removeAttribute("id");
  input.autocomplete = "off";
  input.spellcheck = false;
  input.placeholder = "Add…";
  if (area.dataset.options) input.setAttribute("list", area.dataset.options);

  const values = () => area.value.split("\n").map((v) => v.trim()).filter(Boolean);
  const render = () => {
    const chips = values().map((value) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.append(value);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `Remove ${value}`);
      remove.addEventListener("click", () => {
        area.value = values().filter((v) => v !== value).join("\n");
        render();
        input.focus();
      });
      chip.append(remove);
      return chip;
    });
    box.replaceChildren(...chips, input);
  };
  const add = () => {
    const value = input.value.trim().replace(/,$/, "").trim();
    input.value = "";
    if (!value) return;
    const current = values();
    if (!current.some((v) => v.toLowerCase() === value.toLowerCase())) {
      area.value = [...current, value].join("\n");
    }
    render();
    input.focus();
  };

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      add();
    } else if (event.key === "Backspace" && !input.value && values().length) {
      area.value = values().slice(0, -1).join("\n");
      render();
      input.focus();
    }
  });
  // picking a suggestion from the datalist
  input.addEventListener("input", (event) => {
    if (!(event instanceof InputEvent) || event.inputType === "insertReplacementText") add();
  });
  // a value typed but not added yet is kept when the form is submitted
  area.form.addEventListener("submit", add);
  area.hidden = true;
  area.after(box);
  render();
}

// localStorage can throw (private windows, blocked site data): without it nothing is
// remembered and the page works as rendered
const stored = (key, fallback) => {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
};
const store = (key, value) => {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {}
};

// a folded line a link points at (Status's "Connect Sonarr" → #sonarr) opens
const openTarget = () => {
  const target = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
  const fold = target && (target.matches("details") ? target : target.querySelector(":scope > details"));
  if (fold) fold.open = true;
};
openTarget();
window.addEventListener("hashchange", openTarget);

// a toolbar menu closes on a click outside it or Escape, like a menu
for (const menu of document.querySelectorAll("details.tool-menu")) {
  document.addEventListener("click", (event) => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
  menu.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    menu.querySelector("summary").focus();
  });
}

// status page: service links open the public address unless this browser chose local
const linkChoice = document.querySelector("[data-link-choice]");
if (linkChoice) {
  const apply = (where) => {
    for (const link of document.querySelectorAll("a.open-link")) {
      link.href = (where === "local" && link.dataset.local) || link.dataset.public || link.dataset.local;
    }
    for (const radio of linkChoice.querySelectorAll("input")) radio.checked = radio.value === where;
  };
  apply(stored("helperr.links", "public"));
  linkChoice.hidden = false;
  linkChoice.addEventListener("change", (event) => {
    store("helperr.links", event.target.value);
    apply(event.target.value);
  });
}

// status page: a warning or error can be muted in this browser until it clears. A card
// with everything muted goes quiet and leaves the verdict
const board = document.querySelector(".status-board");
if (board) {
  const key = "helperr.muted";
  let muted = stored(key, {});
  // a mute ends when its problem clears: forget those no longer shown by a service
  // whose problems are all on the page
  const shown = new Set([...board.querySelectorAll("[data-mute]")].map((li) => li.dataset.mute));
  const checked = new Set(
    [...document.querySelectorAll(".service-lines details")].map((d) => d.closest("li").id)
  );
  // not a card that's down or shows only some of its problems ("and 3 more")
  for (const card of board.querySelectorAll(".card[data-service]")) {
    if (!card.querySelector(".problem-block p, .problems li.muted")) checked.add(card.dataset.service);
  }
  for (const id of Object.keys(muted)) {
    if (checked.has(id.split("|")[0]) && !shown.has(id)) delete muted[id];
  }
  store(key, muted);

  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const render = () => {
    let loud = 0;
    for (const card of board.querySelectorAll(".card[data-service]")) {
      const items = [...card.querySelectorAll("[data-mute]")];
      for (const li of items) {
        const isMuted = li.dataset.mute in muted;
        li.classList.toggle("is-muted", isMuted);
        li.querySelector("button.mute").textContent = isMuted ? "Unmute" : "Mute";
        li.parentElement.append(...(isMuted ? [li] : []));
      }
      const quiet = !card.hasAttribute("data-unmutable") && items.length > 0 && items.every((li) => li.classList.contains("is-muted"));
      card.classList.toggle("card-muted", quiet);
      card.querySelector("[data-state-tag]").hidden = quiet;
      card.querySelector("[data-muted-tag]").hidden = !quiet;
      if (!quiet) loud += 1;
      const part = document.querySelector(`.verdict-part[data-service="${card.dataset.service}"]`);
      if (!part) continue;
      part.hidden = quiet;
      if (card.querySelector(".problem-block p")) continue;
      const count = (level) => items.filter((li) => li.dataset.level === level && !li.classList.contains("is-muted")).length;
      const parts = [];
      if (count("bad")) parts.push(plural(count("bad"), "error"));
      if (count("warn")) parts.push(plural(count("warn"), "warning"));
      if (Number(part.dataset.stuck)) parts.push(`${part.dataset.stuck} stuck`);
      part.textContent = `${part.dataset.label}: ${parts.join(", ")}${part.dataset.since ? `, for ${part.dataset.since}` : ""}`;
    }
    const verdict = document.querySelector("[data-verdict]");
    if (verdict) {
      verdict.hidden = loud === 0;
      document.querySelector("[data-verdict-muted]").hidden = loud !== 0;
      verdict.querySelector("[data-verdict-count]").textContent =
        `${loud} ${loud === 1 ? "service needs" : "services need"} you:`;
    }
  };

  for (const li of board.querySelectorAll("[data-mute]")) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mute";
    button.addEventListener("click", () => {
      if (li.dataset.mute in muted) delete muted[li.dataset.mute];
      else muted[li.dataset.mute] = new Date().toISOString();
      store(key, muted);
      render();
    });
    li.append(" ", button);
  }
  render();
}

// a table with data-sortable sorts by a column when its header is clicked, like the
// *arrs' tables. A cell sorts by its data-sort, its <time>, its leading number or its
// text; numbers and times sort biggest/newest first on the first click, text A-Z.
// Rows without a value ("-") stay last. Only tables that show every row are sortable
for (const table of document.querySelectorAll("table[data-sortable]")) {
  const body = table.tBodies[0];
  const headers = [...table.tHead.rows[0].cells];
  const key = (cell) => {
    if (!cell) return null;
    if (cell.dataset.sort !== undefined) {
      const n = Number(cell.dataset.sort);
      return cell.dataset.sort === "" ? null : Number.isNaN(n) ? cell.dataset.sort.toLowerCase() : n;
    }
    const time = cell.querySelector("time[datetime]");
    if (time) return new Date(time.dateTime).getTime();
    const text = cell.textContent.trim();
    if (text === "" || text === "-") return null;
    const number = text.match(/^-?[\d,]*\.?\d+/);
    return number ? Number(number[0].replace(/,/g, "")) : text.toLowerCase();
  };
  headers.forEach((th, column) => {
    if ("nosort" in th.dataset) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sort";
    button.append(...th.childNodes);
    th.append(button);
    button.addEventListener("click", () => {
      const rows = [...body.rows];
      const keys = new Map(rows.map((row) => [row, key(row.cells[column])]));
      const numeric = [...keys.values()].some((k) => typeof k === "number");
      const current = th.getAttribute("aria-sort");
      const descending = current ? current === "ascending" : numeric;
      for (const other of headers) other.removeAttribute("aria-sort");
      th.setAttribute("aria-sort", descending ? "descending" : "ascending");
      rows.sort((a, b) => {
        const x = keys.get(a), y = keys.get(b);
        if (x === null || y === null) return (x === null) - (y === null);
        const order = typeof x === typeof y ? (x < y ? -1 : x > y ? 1 : 0) : typeof x === "number" ? -1 : 1;
        return descending ? -order : order;
      });
      body.append(...rows);
    });
  });
}

// TRaSH Guides: a profile's score in the arr now copies into its "your score" field, so
// keeping the current one is a click
for (const now of document.querySelectorAll("[data-copy-to]")) {
  const input = document.getElementById(now.dataset.copyTo);
  if (!input) continue;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy-score";
  button.title = "Keep this score";
  button.append(...now.childNodes);
  button.addEventListener("click", () => {
    input.value = now.dataset.copyScore;
    input.focus();
  });
  now.append(button);
}

// the assistant's chat, in the popup on every page and on the Assistant page. The
// server renders it (the fragment at /assistant/panel[/id]); this fetches it and posts
// its forms (adding panel=1, so the answer is the fragment again). While Claude works it
// follows the chat's event stream (/assistant/id/stream): the answer appears as it's
// written and the chat is read again when a lookup starts or ends; if the stream fails
// it polls every 3s. What you're typing, open folds and the scroll are kept. Without
// the script the popup's button is a link and the page refreshes itself
const chatView = (body, { popup = false, announce, onShow = () => {} }) => {
  let timer = null;
  // the event stream, the chat it follows, and whether streams work here at all
  let source = null;
  let sourceFor = null;
  let streams = "EventSource" in window;
  // the running answer was shown as it was written, so it needn't be jumped to
  let streamed = false;
  const current = () => body.querySelector("[data-chat-panel]");
  const chatId = () => current()?.dataset.chatId || null;
  const isRunning = (fragment) => Boolean(fragment) && "running" in fragment.dataset;
  const nearEnd = (fragment) => {
    if (!popup) return innerHeight + scrollY >= document.documentElement.scrollHeight - 120;
    const scroller = fragment?.querySelector(".chat-scroll");
    return !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;
  };

  const problem = (text) => {
    body.querySelector("[data-chat-problem]")?.remove();
    const p = document.createElement("p");
    p.className = "banner banner-bad";
    p.dataset.chatProblem = "";
    p.textContent = text;
    const scroller = body.querySelector(".chat-scroll");
    if (!scroller) return body.replaceChildren(p);
    scroller.append(p);
    if (popup) scroller.scrollTop = scroller.scrollHeight;
    for (const button of body.querySelectorAll("form button")) button.disabled = false;
  };

  // asked: a question was just sent, so its box starts empty and the end shows
  const show = (fragment, asked = false) => {
    const before = current();
    const same = Boolean(before) && before.dataset.chatId === fragment.dataset.chatId;
    const answered = same && isRunning(before) && !isRunning(fragment);
    const atEnd = nearEnd(before);
    const top = before?.querySelector(".chat-scroll")?.scrollTop ?? 0;
    const folds = new Set([...body.querySelectorAll("details[open][data-fold]")].map((d) => d.dataset.fold));
    const area = before?.querySelector(".ask textarea");
    const typing = area && document.activeElement === area;
    const [from, to] = typing ? [area.selectionStart, area.selectionEnd] : [0, 0];

    body.replaceChildren(fragment);
    if (same) {
      for (const fold of fragment.querySelectorAll("details[data-fold]")) fold.open = folds.has(fold.dataset.fold);
    }
    const next = fragment.querySelector(".ask textarea");
    if (next && same && !asked && area?.value) next.value = area.value;
    if (next && (typing || asked)) {
      next.focus({ preventScroll: true });
      if (typing && !asked) next.setSelectionRange(from, to);
    }
    for (const hint of fragment.querySelectorAll("[data-enter-hint]")) hint.hidden = false;

    // a new answer that arrived whole reads from its question down; otherwise stay
    // put, or follow the end
    const turn = [...fragment.querySelectorAll(".turn")].at(-1);
    const jump = answered && atEnd && turn && !streamed;
    if (answered) streamed = false;
    if (popup) {
      const scroller = fragment.querySelector(".chat-scroll");
      if (jump) {
        scroller.scrollTop += turn.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 8;
      } else {
        scroller.scrollTop = same && !atEnd && !asked ? top : scroller.scrollHeight;
      }
    } else if (jump) {
      turn.scrollIntoView({ block: "start" });
    } else if (asked) {
      fragment.querySelector(".ask")?.scrollIntoView({ block: "end" });
    }
    if (answered && announce) announce.textContent = "Claude answered.";

    renderTimes();
    clearTimeout(timer);
    if (isRunning(fragment)) follow(fragment);
    else quiet();
    onShow(fragment, { answered });
  };

  const quiet = () => {
    clearTimeout(silence);
    source?.close();
    source = sourceFor = null;
  };
  const poll = () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(), 3000);
  };
  // what Claude has written so far, where its answer goes, following the end if you were there
  const writing = (html) => {
    const box = body.querySelector("[data-live-answer]");
    if (!box) return;
    const atEnd = nearEnd(current());
    box.innerHTML = html;
    box.hidden = false;
    streamed = true;
    const progress = body.querySelector("[data-progress]");
    if (progress) progress.textContent = "Writing the answer";
    if (!atEnd) return;
    if (popup) {
      const scroller = body.querySelector(".chat-scroll");
      scroller.scrollTop = scroller.scrollHeight;
    } else {
      // the page's end, so the progress line and the question box stay in view
      scrollTo({ top: document.documentElement.scrollHeight });
    }
  };
  // the stream pings every 5s: nothing for 12s means something holds it back, so poll
  let silence = null;
  const heard = () => {
    clearTimeout(silence);
    silence = setTimeout(() => {
      streams = false;
      quiet();
      load();
    }, 12000);
  };
  const follow = (fragment) => {
    const id = fragment.dataset.chatId;
    if (!streams) return poll();
    if (source && sourceFor === id) return;
    quiet();
    sourceFor = id;
    source = new EventSource(`/assistant/${id}/stream?v=${fragment.dataset.version || 0}`);
    heard();
    source.addEventListener("ping", heard);
    source.addEventListener("answer", (event) => {
      heard();
      writing(JSON.parse(event.data).html);
    });
    source.addEventListener("refresh", () => {
      heard();
      load();
    });
    source.addEventListener("done", () => {
      quiet();
      load();
    });
    // a connection that ends is reopened by the browser; one it gives up on (signed
    // out, a proxy that won't stream) falls back to polling for good
    source.addEventListener("error", () => {
      if (source?.readyState !== EventSource.CLOSED) return;
      streams = false;
      quiet();
      poll();
    });
  };

  const load = async (url, { method, data, asked = false, reset = () => {} } = {}) => {
    url ??= chatId() ? `/assistant/panel/${chatId()}` : "/assistant/panel";
    try {
      // json, so an error is fastapi's {"detail"}, not an error page
      const response = await fetch(url, { method, body: data, headers: { Accept: "application/json" } });
      // signed out, a GET is sent to the sign-in page
      if (response.redirected && new URL(response.url).pathname === "/login") {
        return problem("You've been signed out: reload the page to sign in again.");
      }
      const type = response.headers.get("content-type") || "";
      if (type.includes("text/html")) {
        const doc = new DOMParser().parseFromString(await response.text(), "text/html");
        const fragment = doc.querySelector("[data-chat-panel]");
        if (fragment) return show(document.adoptNode(fragment), asked);
      }
      if (response.status === 404 && !method && reset()) return;
      let detail = `Helperr answered ${response.status}.`;
      if (response.status === 401) detail = "You've been signed out: reload the page to sign in again.";
      else if (type.includes("json")) {
        const answer = await response.json().catch(() => ({}));
        if (typeof answer.detail === "string") detail = answer.detail;
      }
      problem(detail);
    } catch {
      // Cloudflare Access sends an expired sign-in elsewhere, which fetch can't follow
      problem("Helperr can't be reached right now. If this keeps up, reload the page: you may need to sign in again.");
      clearTimeout(timer);
      if (isRunning(current()) && !source) timer = setTimeout(() => load(), 10000);
    }
  };

  body.addEventListener("submit", (event) => {
    const form = event.target;
    // a new chat from the Assistant page opens as its own page
    if (!popup && !chatId()) return;
    event.preventDefault();
    const url = event.submitter?.getAttribute("formaction") || form.getAttribute("action");
    const data = new URLSearchParams(new FormData(form));
    data.set("panel", "1");
    for (const button of form.querySelectorAll("button")) button.disabled = true;
    load(url, { method: "POST", data, asked: form.matches(".ask") && url === form.getAttribute("action") });
  });
  // Enter asks and Shift+Enter is a new line, like a chat. While Claude works the box
  // only takes the next question down: its button is Stop then
  body.addEventListener("keydown", (event) => {
    const area = event.target.closest(".ask textarea");
    if (!area || event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    const ask = area.form.querySelector("button:not([formaction])");
    if (ask && !ask.disabled && area.value.trim()) area.form.requestSubmit(ask);
  });

  return {
    load,
    show,
    current,
    chatId,
    stop: () => {
      clearTimeout(timer);
      quiet();
    },
  };
};

// the Assistant page: its chat updates in place
const chatHost = document.querySelector("[data-chat-host]");
if (chatHost) {
  const announce = document.createElement("p");
  announce.className = "visually-hidden";
  announce.setAttribute("aria-live", "polite");
  chatHost.after(announce);
  const view = chatView(chatHost, { announce });
  for (const hint of chatHost.querySelectorAll("[data-enter-hint]")) hint.hidden = false;
  // a chat being answered: follow it from the page as rendered
  const rendered = view.current();
  if (rendered && "running" in rendered.dataset) view.show(rendered);
}

// the popup on every other page. Which chat and whether it's open carry over to the
// next page, and an answer that comes while it's closed marks the button
const popup = document.querySelector("[data-chat-popup]");
if (popup) {
  const launcher = document.querySelector("[data-chat-launcher]");
  const label = launcher.querySelector("[data-chat-label]");
  const body = popup.querySelector("[data-chat-body]");
  const title = popup.querySelector("[data-chat-title]");
  const full = popup.querySelector("[data-chat-full]");
  const key = "helperr.chat";
  // {open, id, running, news}
  const state = stored(key, {});
  const save = () => store(key, state);
  const showNews = () => {
    launcher.classList.toggle("has-news", Boolean(state.news));
    label.textContent = state.news ? "Answer ready" : "Ask";
  };

  const view = chatView(body, {
    popup: true,
    announce: popup.querySelector("[data-chat-announce]"),
    onShow: (fragment) => {
      // remembered as answering, now answered: maybe on an earlier page
      const id = fragment.dataset.chatId || null;
      const running = "running" in fragment.dataset;
      if (popup.hidden && state.running && !running && id && String(state.id) === id) state.news = true;
      state.id = id;
      state.running = running;
      save();
      showNews();
      title.textContent = fragment.dataset.title;
      full.href = state.id ? `/assistant/${state.id}` : "/assistant";
    },
  });
  // the chat to open: the one last shown, else the one remembered
  const url = (id) => (id ? `/assistant/panel/${id}` : "/assistant/panel");
  // the remembered chat was deleted: start a new one
  const reset = () => {
    if (!state.id) return false;
    state.id = null;
    state.running = false;
    save();
    view.load(url(null));
    return true;
  };
  const focusQuestion = () => body.querySelector(".ask textarea")?.focus();

  const open = async (focus) => {
    popup.hidden = false;
    launcher.hidden = true;
    launcher.setAttribute("aria-expanded", "true");
    state.open = true;
    state.news = false;
    save();
    showNews();
    await view.load(url(view.chatId() ?? state.id), { reset });
    if (focus) focusQuestion();
  };
  const close = () => {
    popup.hidden = true;
    launcher.hidden = false;
    launcher.setAttribute("aria-expanded", "false");
    state.open = false;
    save();
    launcher.focus();
  };

  launcher.addEventListener("click", (event) => {
    event.preventDefault();
    open(true);
  });
  popup.querySelector("[data-chat-close]").addEventListener("click", close);
  popup.querySelector("[data-chat-new]").addEventListener("click", async () => {
    view.stop();
    await view.load(url(null));
    focusQuestion();
  });
  popup.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
  // a recent chat opens here, not on its page
  body.addEventListener("click", async (event) => {
    const link = event.target.closest("a[data-panel-chat]");
    if (!link) return;
    event.preventDefault();
    await view.load(url(link.dataset.panelChat));
    focusQuestion();
  });

  showNews();
  if (state.open) open(false);
  // closed while Claude works: keep checking, so the button says when it answered
  else if (state.running && state.id) view.load(url(state.id), { reset });
}
