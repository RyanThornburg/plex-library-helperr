// served from /static so the CSP can keep inline scripts off

const pad = (n) => String(n).padStart(2, "0");

// times are rendered in utc, show them in the browser's time zone
for (const el of document.querySelectorAll("time[datetime]")) {
  const d = new Date(el.dateTime);
  if (Number.isNaN(d.getTime())) continue;
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  // data-date: dense tables show the date, the time is in the title
  el.title = "date" in el.dataset ? `${date} ${time}` : el.textContent;
  el.textContent = "date" in el.dataset ? date : `${date} ${time}`;
}

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
