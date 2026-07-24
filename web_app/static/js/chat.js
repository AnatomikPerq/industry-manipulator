/* ============================================================
   Обычный чат с нейросетью - отдельный экран (#/chat).

   Это НЕ анализ документации: живой диалог с моделью, минуя очередь и пайплайн
   (см. web_app/chats.py). У пользователя всегда ровно один активный чат;
   «Новый чат» архивирует старый на диск и открывает пустой - списка прошлых
   диалогов у пользователя нет, так и задумано.

   Модель выбирается из ЗАГРУЖЕННЫХ в память LM Studio (loaded), а не просто
   скачанных: общаться можно только с тем, что реально поднято. Список берём у
   того же /api/models, что и выбор моделей сессии.

   Ответ приходит ПОТОКОМ (ndjson-события): fetch отдаёт тело как поток, читаем
   его построчно и дорисовываем пузырь ответа по мере генерации.
   ============================================================ */

import { $, esc, fetchJSON, showView } from "./util.js";

let chat = null;          // активный чат {id, model, messages:[...]}
let models = [];          // модели сервера (с флагами loaded/vision)
let catalogError = null;  // почему список моделей не получен, если не получен
let pending = [];         // приложенные, но ещё не отправленные файлы
let sending = false;      // идёт ли сейчас обмен с моделью
let abort = null;         // прерывание текущего потока (кнопка «Стоп», уход с экрана)
let bound = false;        // события навешиваем один раз

// ------------------------------------------------------------
// Вход на экран
// ------------------------------------------------------------
export async function openChat() {
  showView("chat");
  bindChat();
  pending = [];
  renderPending();
  await Promise.all([loadChat(), loadModels()]);
  renderModelSelect();
  renderMessages();
  setTps(null);
  const input = $("chat-input");
  if (input) input.focus();
}

// Уход с экрана (роутер) - оборвать незаконченный поток, иначе он продолжал бы
// дорисовывать в невидимый уже пузырь.
export function stopChat() {
  if (abort) { abort.abort(); abort = null; }
}

async function loadChat() {
  try {
    const data = await fetchJSON("/api/chat");
    chat = data.chat || { model: null, messages: [] };
  } catch (e) {
    chat = { model: null, messages: [] };
  }
}

async function loadModels() {
  try {
    const data = await fetchJSON("/api/models");
    models = data.models || [];
    catalogError = data.error || null;
  } catch (e) {
    models = [];
    catalogError = e.message;
  }
}

// ------------------------------------------------------------
// Выбор модели (только загруженные)
// ------------------------------------------------------------
function renderModelSelect() {
  const sel = $("chat-model");
  const note = $("chat-model-note");
  if (!sel) return;
  const loaded = models.filter((m) => m.loaded);

  if (!loaded.length && !chat.model) {
    sel.innerHTML = `<option value="">— нет загруженных моделей —</option>`;
    note.textContent = catalogError
      ? "Список моделей не получен: " + catalogError
      : "В LM Studio не загружено ни одной модели. Загрузите модель в LM Studio "
        + "(именно загрузите в память, а не просто скачайте) и обновите страницу.";
    return;
  }

  let html = `<option value="">— выберите модель —</option>`;
  for (const m of loaded) {
    const s = m.key === chat.model ? " selected" : "";
    const bits = [];
    if (m.params) bits.push(m.params);
    if (m.vision) bits.push("зрение");
    const tail = bits.length ? ` — ${esc(bits.join(", "))}` : "";
    html += `<option value="${esc(m.key)}"${s}>${esc(m.display_name)}${tail}</option>`;
  }
  // Модель была выбрана, но с сервера исчезла (выгрузили) - показать честно,
  // а не подменять молча: иначе непонятно, чем идёт разговор.
  if (chat.model && !loaded.some((m) => m.key === chat.model)) {
    html += `<option value="${esc(chat.model)}" selected>${esc(chat.model)} — не загружена</option>`;
  }
  sel.innerHTML = html;

  // Подсказка про зрение: выбрана модель без зрения - картинки она не поймёт.
  const chosen = models.find((m) => m.key === chat.model);
  if (chat.model && chosen && !chosen.vision) {
    note.textContent = "Эта модель не умеет обрабатывать изображения — "
      + "картинки отправлять ей бессмысленно, но текст и файлы она примет.";
  } else if (catalogError) {
    note.textContent = "Список моделей мог обновиться не полностью: " + catalogError;
  } else {
    note.textContent = "";
  }
}

async function onModelChange(e) {
  const model = e.target.value || null;
  try {
    const res = await fetchJSON("/api/chat/set-model", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    });
    chat.model = res.model || null;
  } catch (err) {
    chat.model = model;   // локально запомним, сервер поправит при следующем заходе
  }
  renderModelSelect();
}

// ------------------------------------------------------------
// Сообщения
// ------------------------------------------------------------
function renderMessages() {
  const box = $("chat-messages");
  if (!box) return;
  box.innerHTML = "";
  const msgs = (chat && chat.messages) || [];
  if (!msgs.length) {
    box.appendChild(emptyPlaceholder());
    return;
  }
  for (const m of msgs) box.appendChild(messageEl(m));
  scrollBottom();
}

function emptyPlaceholder() {
  const d = document.createElement("div");
  d.className = "chat-empty";
  d.textContent = "Начните диалог: выберите загруженную модель и напишите "
    + "сообщение. Можно приложить файлы и фото.";
  return d;
}

// DOM-узел одного сообщения. Строим узлами, а не innerHTML: текст ответа модели
// дорисовывается потоком через textContent, и любая разметка внутри него должна
// остаться текстом, а не исполниться.
function messageEl(msg) {
  const row = document.createElement("div");
  row.className = "chat-msg " + (msg.role === "user" ? "user" : "bot");

  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";

  for (const node of fileEls(msg.files || [])) bubble.appendChild(node);

  const txt = document.createElement("div");
  txt.className = "chat-text";
  txt.textContent = msg.content || "";
  if (msg.content) bubble.appendChild(txt);

  row.appendChild(bubble);
  return row;
}

function fileEls(files) {
  const out = [];
  for (const f of files) {
    const url = "/api/chat/file?path=" + encodeURIComponent(f.path);
    if (f.kind === "image") {
      const img = document.createElement("img");
      img.className = "chat-img";
      img.src = url;
      img.alt = f.name || "";
      out.push(img);
    } else {
      const a = document.createElement("a");
      a.className = "chat-file";
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "📎 " + (f.name || "файл");
      out.push(a);
    }
  }
  return out;
}

function scrollBottom() {
  const box = $("chat-messages");
  if (box) box.scrollTop = box.scrollHeight;
}

// ------------------------------------------------------------
// Раздумья «думающей» модели и счётчик скорости
// ------------------------------------------------------------
// Сворачиваемый блок рассуждения над ответом. Появляется, только когда пришли
// reasoning-события: у обычной (не думающей) модели его нет вовсе. Во время
// генерации раскрыт, по завершении сворачивается - как в привычных чатах.
function ensureThinkBody(bubble) {
  let think = bubble.querySelector(".chat-think");
  if (!think) {
    think = document.createElement("details");
    think.className = "chat-think";
    think.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "Рассуждение модели";
    const body = document.createElement("div");
    body.className = "chat-think-body";
    think.append(summary, body);
    bubble.insertBefore(think, bubble.firstChild);   // над текстом ответа
  }
  return think.querySelector(".chat-think-body");
}

function setTps(tps) {
  const el = $("chat-tps");
  if (!el) return;
  if (tps == null) { el.textContent = ""; el.classList.add("hidden"); return; }
  el.textContent = `${tps} ток/с`;
  el.classList.remove("hidden");
}

// ------------------------------------------------------------
// Приложенные файлы (до отправки)
// ------------------------------------------------------------
async function attachFiles(fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  try {
    const data = await fetchJSON("/api/chat/upload", { method: "POST", body: fd });
    pending.push(...(data.files || []));
    renderPending();
    if ((data.skipped || []).length) {
      const names = data.skipped.map((s) => s.name).join(", ");
      $("chat-attach-note").textContent = "Не приняты: " + names;
    }
  } catch (e) {
    $("chat-attach-note").textContent = "Не удалось приложить файл: " + e.message;
  }
}

function renderPending() {
  const box = $("chat-pending");
  if (!box) return;
  box.innerHTML = "";
  box.style.display = pending.length ? "flex" : "none";
  pending.forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "chat-chip";
    chip.textContent = (f.kind === "image" ? "🖼 " : "📎 ") + f.name;
    const x = document.createElement("button");
    x.className = "chat-chip-x";
    x.textContent = "×";
    x.title = "Убрать";
    x.addEventListener("click", () => { pending.splice(i, 1); renderPending(); });
    chip.appendChild(x);
    box.appendChild(chip);
  });
}

// ------------------------------------------------------------
// Отправка и потоковый приём ответа
// ------------------------------------------------------------
function setSending(on) {
  sending = on;
  const btn = $("chat-send");
  btn.textContent = on ? "Стоп" : "Отправить";
  btn.classList.toggle("btn-cancel", on);
  $("chat-attach").disabled = on;
  $("chat-model").disabled = on;
}

async function sendMessage() {
  if (sending) return;
  const input = $("chat-input");
  const text = input.value.trim();
  const files = pending.slice();
  if (!text && !files.length) return;
  if (!chat.model) {
    $("chat-model-note").textContent = "Сначала выберите модель для чата.";
    $("chat-model").focus();
    return;
  }

  // Оптимистично рисуем сообщение пользователя и пустой пузырь ответа.
  chat.messages.push({ role: "user", content: text, files });
  const box = $("chat-messages");
  const ph = box.querySelector(".chat-empty");
  if (ph) ph.remove();
  box.appendChild(messageEl(chat.messages[chat.messages.length - 1]));
  input.value = "";
  autoResize();
  pending = [];
  renderPending();
  $("chat-attach-note").textContent = "";

  const botRow = messageEl({ role: "assistant", content: "", files: [] });
  const bubble = botRow.querySelector(".chat-bubble");
  const txt = document.createElement("div");
  txt.className = "chat-text streaming";
  bubble.appendChild(txt);
  box.appendChild(botRow);
  scrollBottom();

  setSending(true);
  setTps(null);
  abort = new AbortController();
  let acc = "";
  let thinkBody = null;   // тело блока рассуждения — создаётся по первому reasoning
  try {
    const res = await fetch("/api/chat/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, files }), signal: abort.signal,
    });
    if (!res.ok) {
      const body = await res.text();
      let msg;
      try { msg = JSON.parse(body).error; } catch { msg = body || `HTTP ${res.status}`; }
      throw new Error(msg);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === "delta") {
          acc += ev.text;
          txt.textContent = acc;
          scrollBottom();
        } else if (ev.type === "reasoning") {
          if (!thinkBody) thinkBody = ensureThinkBody(bubble);
          thinkBody.textContent += ev.text;
          scrollBottom();
        } else if (ev.type === "stats") {
          setTps(ev.tps);
        } else if (ev.type === "error") {
          markError(txt, acc, ev.error);
          acc = txt.textContent;
        }
      }
    }
    if (!acc.trim() && !txt.classList.contains("chat-err")) {
      markError(txt, "", "модель вернула пустой ответ");
    }
    // История на диске уже дописана сервером; локально она у нас и так есть.
    chat.messages.push({ role: "assistant", content: acc, files: [] });
  } catch (e) {
    if (e.name === "AbortError") {
      // Пользователь нажал «Стоп»: частичный ответ сервер сохранил, оставляем
      // как есть; если не пришло ничего - убираем пустой пузырь.
      if (!acc.trim()) botRow.remove();
      else chat.messages.push({ role: "assistant", content: acc, files: [] });
    } else {
      markError(txt, acc, e.message);
    }
  } finally {
    txt.classList.remove("streaming");
    // Рассуждение по завершении сворачиваем: интересен ответ, а протокол
    // размышления пусть остаётся под спойлером, как в привычных чатах.
    const think = bubble.querySelector(".chat-think");
    if (think) think.open = false;
    abort = null;
    setSending(false);
    input.focus();
  }
}

function markError(txt, partial, message) {
  txt.classList.add("chat-err");
  txt.textContent = (partial ? partial + "\n\n" : "")
    + "⚠ Ошибка: " + message;
  scrollBottom();
}

async function newChat() {
  if (chat && chat.messages && chat.messages.length
      && !confirm("Начать новый чат? Текущий будет закрыт (он сохранится в архиве на диске, но здесь вы его больше не увидите).")) {
    return;
  }
  try {
    const data = await fetchJSON("/api/chat/new", { method: "POST" });
    chat = data.chat;
    pending = [];
    renderPending();
    renderModelSelect();
    renderMessages();
    $("chat-input").focus();
  } catch (e) {
    alert("Не удалось начать новый чат: " + e.message);
  }
}

function autoResize() {
  const el = $("chat-input");
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 200) + "px";
}

// ------------------------------------------------------------
// Навесить обработчики - один раз
// ------------------------------------------------------------
function bindChat() {
  if (bound) return;
  bound = true;

  $("chat-model").addEventListener("change", onModelChange);
  $("chat-new").addEventListener("click", newChat);

  $("chat-send").addEventListener("click", () => {
    if (sending) { if (abort) abort.abort(); }
    else sendMessage();
  });

  const input = $("chat-input");
  input.addEventListener("input", autoResize);
  input.addEventListener("keydown", (e) => {
    // Enter отправляет, Shift+Enter - перенос строки (как в привычных чатах).
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!sending) sendMessage();
    }
  });

  const fileInput = $("chat-file-input");
  $("chat-attach").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", (e) => {
    attachFiles(e.target.files);
    e.target.value = "";
  });
}
