/* ============================================================
   Переключатель темы оформления: светлая / тёмная / системная.

   Выбор хранится в браузере (localStorage, ключ "im-theme") и общий на
   все экраны. Хранится ИМЕННО предпочтение ("system"), а не вычисленный
   из него цвет: иначе выбравший «как в системе» не увидел бы смену темы
   ОС на лету. Конкретное значение ("light"/"dark") резолвится здесь и
   ставится атрибутом data-theme на <html> — по нему и работает CSS
   (styles.css, блок [data-theme="dark"]).

   Раннее применение (до отрисовки, чтобы не мигало светлым) делает
   инлайн-скрипт в <head> index.html. Здесь — переключатель в интерфейсе
   и слежение за системной темой.
   ============================================================ */

const KEY = "im-theme";
const PREFS = ["light", "system", "dark"];

const LABELS = {
  light:  "Светлая тема",
  system: "Как в системе",
  dark:   "Тёмная тема",
};

// Иконки (наследуют цвет через currentColor).
const ICONS = {
  light: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.2"/>
      <path d="M12 2.5v2.4M12 19.1v2.4M4.6 4.6l1.7 1.7M17.7 17.7l1.7 1.7M2.5 12h2.4M19.1 12h2.4M4.6 19.4l1.7-1.7M17.7 6.3l1.7-1.7"/></svg>`,
  system: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="13" rx="2"/>
      <path d="M8.5 20.5h7M12 17v3.5"/></svg>`,
  dark: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 14.3A8.5 8.5 0 1 1 9.7 3.5a6.7 6.7 0 0 0 10.8 10.8z"/></svg>`,
};

const CONTAINER_IDS = ["theme-switch-header", "theme-switch-auth"];

function storedPref() {
  try {
    const v = localStorage.getItem(KEY);
    return PREFS.includes(v) ? v : "system";
  } catch { return "system"; }
}

function systemDark() {
  return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
}

function resolve(pref) {
  return pref === "system" ? (systemDark() ? "dark" : "light") : pref;
}

function applyPref(pref) {
  document.documentElement.setAttribute("data-theme", resolve(pref));
}

function setPref(pref) {
  try { localStorage.setItem(KEY, pref); } catch { /* приватный режим */ }
  applyPref(pref);
  renderAll();
}

function renderAll() {
  const pref = storedPref();
  for (const id of CONTAINER_IDS) {
    const host = document.getElementById(id);
    if (!host) continue;
    if (!host.dataset.built) buildInto(host);
    host.querySelectorAll(".theme-opt").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.themePref === pref);
      b.setAttribute("aria-pressed", b.dataset.themePref === pref ? "true" : "false");
    });
  }
}

function buildInto(host) {
  const seg = document.createElement("div");
  seg.className = "theme-seg";
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Тема оформления");
  for (const pref of PREFS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "theme-opt";
    b.dataset.themePref = pref;
    b.title = LABELS[pref];
    b.setAttribute("aria-label", LABELS[pref]);
    b.innerHTML = ICONS[pref];
    b.addEventListener("click", () => setPref(pref));
    seg.appendChild(b);
  }
  host.innerHTML = "";
  host.appendChild(seg);
  host.dataset.built = "1";
}

export function initTheme() {
  applyPref(storedPref());
  renderAll();
  // Смена системной темы на лету касается только режима «как в системе».
  if (window.matchMedia) {
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => { if (storedPref() === "system") applyPref("system"); };
    if (mql.addEventListener) mql.addEventListener("change", onChange);
    else if (mql.addListener) mql.addListener(onChange);   // старые браузеры
  }
}
