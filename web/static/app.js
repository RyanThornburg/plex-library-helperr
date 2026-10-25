// served from /static so the CSP can keep inline scripts off

const pad = (n) => String(n).padStart(2, "0");

// times are rendered in utc, show them in the browser's time zone
for (const el of document.querySelectorAll("time[datetime]")) {
  const d = new Date(el.dateTime);
  if (Number.isNaN(d.getTime())) continue;
  el.title = el.textContent;
  el.textContent =
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
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
