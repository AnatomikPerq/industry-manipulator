/* ============================================================
   Экран входа: логин, при необходимости - регистрация или пароль админа.

   Один шаг за раз (см. server.py::_api_login):
     login    - введён логин, спрашиваем сервер, что дальше;
     register - такого логина нет, предлагаем зарегистрировать;
     password - это администратор, просим пароль.

   Токен входа сервер кладёт в cookie сам - здесь его не видно и не нужно;
   кто вошёл, говорит /api/auth/me. Поэтому auth.js хранит состояние только на
   время диалога входа, а не «сессию пользователя».
   ============================================================ */

import { $, esc, fetchJSON } from "./util.js";

let step = "login";
let onSuccess = null;   // колбэк app.js: setUser + запуск приложения

// ------------------------------------------------------------
// Показ/скрытие экрана. Класс на body: пока unauthed - приложение спрятано
// (styles.css), виден только этот экран.
// ------------------------------------------------------------
export function showAuth() {
  document.body.classList.add("unauthed");
  resetToLogin();
  const login = $("auth-login");
  if (login) login.focus();
}
export function hideAuth() {
  document.body.classList.remove("unauthed");
}

// Кто вошёл прямо сейчас (или null). Вызывается на старте app.js.
export async function whoAmI() {
  try {
    const data = await fetchJSON("/api/auth/me");
    return data.user || null;
  } catch {
    return null;
  }
}

export async function logout() {
  try { await fetchJSON("/api/auth/logout", { method: "POST" }); }
  catch { /* всё равно уходим на экран входа */ }
}

// ------------------------------------------------------------
// Диалог входа
// ------------------------------------------------------------
export function initAuthScreen(onOk) {
  onSuccess = onOk;
  $("auth-form").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  $("auth-back").addEventListener("click", resetToLogin);
  // Правка логина в шаге пароля/регистрации отменяет их: решение «админ / нет
  // такого» относилось к прежнему тексту.
  $("auth-login").addEventListener("input", () => {
    if (step !== "login") resetToLogin();
  });
}

function resetToLogin() {
  step = "login";
  $("auth-pass-wrap").classList.add("hidden");
  $("auth-pass").value = "";
  $("auth-back").classList.add("hidden");
  $("auth-submit").textContent = "Войти";
  setMsg("");
}

function setMsg(text, kind) {
  const el = $("auth-msg");
  el.textContent = text || "";
  el.className = "auth-msg" + (kind ? " auth-msg-" + kind : "");
}

async function submit() {
  const login = $("auth-login").value.trim();
  if (!login) { setMsg("Введите логин.", "err"); $("auth-login").focus(); return; }

  const btn = $("auth-submit");
  btn.disabled = true;
  try {
    if (step === "register") return await doRegister(login);
    const password = step === "password" ? $("auth-pass").value : undefined;
    const data = await fetchJSON("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(password === undefined ? { login } : { login, password }),
    });
    handleLogin(data, login);
  } catch (e) {
    setMsg(e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

function handleLogin(data, login) {
  if (data.status === "ok") return succeed(data.user);

  if (data.status === "password_required") {
    step = "password";
    $("auth-pass-wrap").classList.remove("hidden");
    $("auth-back").classList.remove("hidden");
    $("auth-submit").textContent = "Войти";
    setMsg("Это учётная запись администратора — введите пароль.");
    $("auth-pass").focus();
    return;
  }
  if (data.status === "bad_password") {
    setMsg("Неверный пароль.", "err");
    $("auth-pass").focus();
    $("auth-pass").select();
    return;
  }
  if (data.status === "not_found") {
    step = "register";
    $("auth-pass-wrap").classList.add("hidden");
    $("auth-back").classList.remove("hidden");
    $("auth-submit").textContent = "Зарегистрировать";
    setMsg(`Логин «${esc(login)}» не найден. Зарегистрировать нового пользователя?`);
    return;
  }
  setMsg("Непонятный ответ сервера.", "err");
}

async function doRegister(login) {
  try {
    const data = await fetchJSON("/api/auth/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login }),
    });
    succeed(data.user);
  } catch (e) {
    setMsg(e.message, "err");
    $("auth-submit").disabled = false;
  }
}

function succeed(user) {
  resetToLogin();
  $("auth-login").value = "";
  hideAuth();
  if (onSuccess) onSuccess(user);
}
