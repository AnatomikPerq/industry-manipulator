/* ============================================================
   Экран 1: список сессий.

   Сессии общие для всех: любой сотрудник видит чужие, может открыть отчёт и
   снять сессию с исполнения. Авторизации нет сознательно - инструмент
   корпоративный.
   ============================================================ */

import { S, statusBadge } from "./state.js";
import { $, esc, fetchJSON, fmtTime } from "./util.js";

// ------------------------------------------------------------
// Экран 1: список сессий
// ------------------------------------------------------------
export async function refreshSessions() {
  let data;
  try { data = await fetchJSON("/api/sessions"); }
  catch { return; }   // сеть моргнула - подождём следующего тика

  const list = $("session-list");
  const empty = $("session-empty");
  const sessions = data.sessions || [];
  empty.style.display = sessions.length ? "none" : "block";

  // Очередей теперь две, и говорить о них надо раздельно: скрипты считаются
  // параллельно и почти никогда не ждут, а к серверу ИИ пропускается строго
  // одна сессия - именно там и стоит настоящая очередь.
  // «Считается» - это script_busy, а не число живых прогонов: сессия, ушедшая
  // ждать очереди к ИИ, свой скриптовый слот уже отдала и процессор не грузит,
  // хотя её процесс жив и в running она есть.
  const nRunning = data.script_busy != null
    ? data.script_busy : (data.running || []).length;
  const nQueued = (data.queued || []).length;
  const nLlm = (data.llm_queue || []).length;
  const parts = [];
  if (nRunning) parts.push(`считается сессий: ${nRunning}`);
  if (nQueued) parts.push(`ждут свободного обработчика: ${nQueued}`);
  if (data.llm_busy) parts.push("сервер ИИ занят");
  if (nLlm) parts.push(`в очереди к ИИ: ${nLlm}`);
  $("queue-note").textContent = parts.length
    ? parts.join(" · ") + "."
    : "Сейчас ничего не считается.";

  list.innerHTML = sessions.map(renderSessionCard).join("");
  list.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const { act, id } = e.currentTarget.dataset;
      if (act === "open") location.hash = "#/s/" + encodeURIComponent(id);
      if (act === "cancel") cancelSession(id, refreshSessions);
      if (act === "delete") deleteSession(id);
    });
  });
}

export function renderSessionCard(s) {
  const status = statusBadge(s);
  const findings = s.n_findings != null
    ? `<span class="sc-findings">замечаний: <b>${s.n_findings}</b></span>` : "";
  const when = s.finished_at || s.started_at || s.created_at;
  const canCancel = s.status === "queued" || s.status === "running";
  return `
    <div class="session-card">
      <div class="sc-main">
        <a class="sc-name" href="#/s/${encodeURIComponent(s.id)}"
           data-act="open" data-id="${esc(s.id)}">${esc(s.name)}</a>
        <div class="sc-meta">
          ${status}
          <span>файлов: ${s.n_files}</span>
          ${findings}
          <span class="sc-time">${fmtTime(when)}</span>
        </div>
        ${s.error ? `<div class="sc-error">${esc(s.error)}</div>` : ""}
      </div>
      <div class="sc-actions">
        <button class="btn" data-act="open" data-id="${esc(s.id)}">Открыть</button>
        ${canCancel
          ? `<button class="btn btn-cancel" data-act="cancel" data-id="${esc(s.id)}">Отменить</button>`
          : `<button class="btn btn-ghost" data-act="delete" data-id="${esc(s.id)}">Удалить</button>`}
      </div>
    </div>`;
}



export function toggleNewSession(on) {
  $("new-session").classList.toggle("hidden", !on);
  if (on) { $("new-session-name").value = ""; $("new-session-name").focus(); }
}

export async function createSession() {
  const name = $("new-session-name").value.trim();
  try {
    const meta = await fetchJSON("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    location.hash = "#/s/" + encodeURIComponent(meta.id);
  } catch (e) {
    alert("Не удалось создать сессию: " + e.message);
  }
}

export async function deleteSession(id) {
  if (!confirm("Удалить сессию вместе с её файлами и отчётом? Отменить это нельзя.")) return;
  try {
    await fetchJSON(`/api/sessions/${encodeURIComponent(id)}/delete`, { method: "POST" });
    if (S.id === id) location.hash = "#/";
    else await refreshSessions();
  } catch (e) {
    alert("Не удалось удалить: " + e.message);
  }
}

// Отмена и из очереди, и на ходу - сервер сам разбирается, что именно делать.
export async function cancelSession(id, after) {
  try {
    await fetchJSON(`/api/sessions/${encodeURIComponent(id)}/cancel`, { method: "POST" });
  } catch (e) {
    alert("Не удалось отменить: " + e.message);
  }
  if (after) await after();
}
