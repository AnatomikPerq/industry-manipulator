/* ============================================================
   Мелкие общие вещи: доступ к DOM, экранирование, форматы,
   запрос к API, консоль и строка состояния.

   Ничего доменного здесь нет и быть не должно: это то, чем
   пользуются все экраны сразу.
   ============================================================ */

export const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------
// Ответ разбираем ОСТОРОЖНО: JSON приходит не всегда. BaseHTTPRequestHandler
// отвечает на send_error() страничкой HTML, и прежний безусловный res.json()
// падал на ней SyntaxError'ом - пользователь видел «Unexpected token '<'»
// вместо «сессия не найдена». Тело читаем один раз текстом и разбираем сами.
export async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* не JSON */ }
  if (!res.ok) {
    throw new Error((data && data.error) || res.statusText || `HTTP ${res.status}`);
  }
  if (data === null) throw new Error("сервер вернул не JSON");
  return data;
}
export function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
export function joinNonEmpty(arr, sep) {
  const v = arr.filter((x) => x !== null && x !== undefined && x !== "");
  return v.length ? v.join(sep) : null;
}
export function fmtSize(b) {
  if (b < 1024) return b + " Б";
  if (b < 1024 * 1024) return (b / 1024).toFixed(0) + " КБ";
  return (b / 1024 / 1024).toFixed(1) + " МБ";
}
export function fmtCtx(n) { return n >= 1000 ? Math.round(n / 1000) + "K" : String(n); }
export function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit",
    year: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function showToast(text, kind, sessionId) {
  let host = $("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    document.body.appendChild(host);
  }
  const t = document.createElement("div");
  t.className = "toast toast-" + (kind || "ok");
  t.textContent = text;
  t.title = "Открыть сессию";
  t.addEventListener("click", () => {
    location.hash = "#/s/" + encodeURIComponent(sessionId);
    t.remove();
  });
  host.appendChild(t);
  setTimeout(() => { t.classList.add("gone"); }, 8000);
  setTimeout(() => t.remove(), 8600);
}

export function logLine(text, cls) {
  const el = $("console");
  const span = document.createElement("div");
  if (cls) span.className = "l-" + cls;
  span.textContent = text;
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}
export function classifyLog(line) {
  const l = line.toLowerCase();
  if (l.includes("[error]") || l.includes("ошибка") || l.includes("!!!")) return "err";
  if (l.includes("[warning]") || l.includes("пропущен")) return "warn";
  if (l.includes("готово") || l.includes("сохранён") || l.includes("===")) return "ok";
  return "";
}
export function setStatus(text, busy, cls) {
  const bar = $("status-bar");
  bar.classList.remove("hidden");
  $("status-text").textContent = text;
  $("status-text").className = cls === "err" ? "dot-err" : cls === "ok" ? "dot-ok" : "";
  $("status-spinner").style.display = busy ? "block" : "none";
}


export function showView(which) {
  $("view-list").classList.toggle("show", which === "list");
  $("view-session").classList.toggle("show", which === "session");
}

export function askNotifyPermission() {
  if ("Notification" in window && Notification.permission === "default") {
    try { Notification.requestPermission(); } catch { /* старый браузер */ }
  }
}
