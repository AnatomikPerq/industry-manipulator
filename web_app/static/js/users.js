/* ============================================================
   Панель управления пользователями (только администратору).

   Заводит/удаляет пользователей, назначает администратором, меняет пароль.
   Обычному пользователю нужен только логин; администратору - пароль (в открытом
   виде: разграничение мягкое, см. users.py на бэкенде).
   ============================================================ */

import { USER } from "./state.js";
import { $, esc, fetchJSON } from "./util.js";

export function initUsers() {
  $("users-close").addEventListener("click", closeUsers);
  $("users-modal").addEventListener("click", (e) => {
    if (e.target === $("users-modal")) closeUsers();   // клик по фону
  });
  // Пароль в форме добавления нужен только администратору - показываем по галке.
  $("nu-admin").addEventListener("change", (e) => {
    $("nu-pass").classList.toggle("hidden", !e.target.checked);
    if (e.target.checked) $("nu-pass").focus();
  });
  $("users-add-form").addEventListener("submit", (e) => { e.preventDefault(); addUser(); });
}

export function openUsers() {
  $("users-modal").classList.add("show");
  loadUsers();
}
export function closeUsers() {
  $("users-modal").classList.remove("show");
}

async function loadUsers() {
  const box = $("users-list");
  box.innerHTML = `<div class="hint-text">Загрузка…</div>`;
  try {
    const data = await fetchJSON("/api/users");
    box.innerHTML = data.users.map(renderRow).join("");
    bindRows();
  } catch (e) {
    box.innerHTML = `<div class="l-err">${esc(e.message)}</div>`;
  }
}

function renderRow(u) {
  const me = u.canonical === USER.canonical;
  const badge = u.is_admin
    ? `<span class="u-badge u-admin">администратор</span>`
    : `<span class="u-badge">пользователь</span>`;
  const actions = [];
  if (u.is_admin) {
    actions.push(`<button class="btn btn-ghost" data-act="password" data-login="${esc(u.login)}">Сменить пароль</button>`);
    actions.push(`<button class="btn btn-ghost" data-act="demote" data-login="${esc(u.login)}">Убрать из админов</button>`);
  } else {
    actions.push(`<button class="btn btn-ghost" data-act="promote" data-login="${esc(u.login)}">Сделать админом</button>`);
  }
  actions.push(`<button class="btn btn-cancel" data-act="delete" data-login="${esc(u.login)}">Удалить</button>`);
  return `
    <div class="u-row">
      <div class="u-main">
        <span class="u-login">${esc(u.login)}${me ? ` <span class="u-me">(вы)</span>` : ""}</span>
        ${badge}
      </div>
      <div class="u-actions">${actions.join("")}</div>
    </div>`;
}

function bindRows() {
  $("users-list").querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", () => rowAction(btn.dataset.act, btn.dataset.login));
  });
}

async function rowAction(act, login) {
  try {
    if (act === "delete") {
      if (!confirm(`Удалить пользователя «${login}»? Его сессии останутся, но станут бесхозными.`)) return;
      await fetchJSON(`/api/users/${encodeURIComponent(login)}/delete`, { method: "POST" });
    } else if (act === "promote") {
      const password = prompt(`Пароль для администратора «${login}»:`);
      if (!password) return;
      await updateUser(login, { is_admin: true, password });
    } else if (act === "demote") {
      if (!confirm(`Убрать «${login}» из администраторов? Он перестанет видеть чужие сессии.`)) return;
      await updateUser(login, { is_admin: false });
    } else if (act === "password") {
      const password = prompt(`Новый пароль для «${login}»:`);
      if (!password) return;
      await updateUser(login, { password });
    }
    await loadUsers();
  } catch (e) {
    alert(e.message);
  }
}

function updateUser(login, body) {
  return fetchJSON(`/api/users/${encodeURIComponent(login)}/update`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function addUser() {
  const login = $("nu-login").value.trim();
  const isAdmin = $("nu-admin").checked;
  const password = $("nu-pass").value;
  const msg = $("nu-msg");
  msg.textContent = "";
  if (!login) { msg.textContent = "Введите логин."; return; }
  try {
    await fetchJSON("/api/users", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login, is_admin: isAdmin, password }),
    });
    $("nu-login").value = "";
    $("nu-pass").value = "";
    $("nu-admin").checked = false;
    $("nu-pass").classList.add("hidden");
    await loadUsers();
  } catch (e) {
    msg.textContent = e.message;
  }
}
