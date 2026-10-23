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

// a "select all" box in a table header toggles the named checkboxes in its form
for (const all of document.querySelectorAll("input[data-select-all]")) {
  const boxes = () => all.form.querySelectorAll(`input[name="${all.dataset.selectAll}"]`);
  all.addEventListener("change", () => {
    for (const box of boxes()) box.checked = all.checked;
  });
}
