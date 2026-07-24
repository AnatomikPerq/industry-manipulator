/* ============================================================
   Экран 2: одна сессия - файлы, запуск, консоль, ход разбора.

   Состояние сессии живёт на сервере, в session.json, а не во вкладке:
   вкладку можно закрыть, сессия останется в очереди и досчитается.
   ============================================================ */

import { DOC_TYPES, S, isBusy, resetSession, statusBadge } from "./state.js";
import { cancelSession } from "./list.js";
import { showReport } from "./report.js";
import { renderLLM } from "./llm.js";
import { $, askNotifyPermission, classifyLog, esc, fetchJSON, fmtSize, logLine,
         setStatus, showView } from "./util.js";

// Как режим прогона называется по-русски. Одна таблица на весь модуль: подпись
// нужна и в строке статуса, и в консоли, и раньше «полный/без ИИ» стояло в двух
// местах отдельными тернарниками - третий режим разъехался бы с ними молча.
// Ключи обязаны совпадать с теми, что принимает server.py::_api_enqueue.
const MODE_LABELS = {
  scripts: "без ИИ",
  full: "полный",
  visual: "визуальный",
  full_visual: "полный + визуальный",
};

// Поллинг ОТКРЫТОЙ сессии. Живёт здесь, а не в роутере: заводит его этот
// экран, и гасить его умеет только он.
let sessionTimer = null;

export function stopSessionPolling() {
  if (sessionTimer) { clearInterval(sessionTimer); sessionTimer = null; }
}

// ------------------------------------------------------------
// Экран 2: одна сессия
// ------------------------------------------------------------
export async function openSession(id) {
  resetSession(id);
  $("console").innerHTML = "";
  $("report-section").classList.remove("show");
  $("llm-panel").classList.remove("show");
  showView("session");

  try {
    await loadSession();
  } catch (e) {
    alert("Сессия не открывается: " + e.message);
    location.hash = "#/";
    return;
  }

  // лог и отчёт восстанавливаем с сервера: он на диске, а не в памяти вкладки
  await pumpLog();
  if (S.meta.status === "done") await showReport();
  if (isBusy()) startSessionPolling();
}

export async function loadSession() {
  const meta = await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}`);
  S.meta = meta;
  S.files = (meta.files || []).map((f) => ({
    name: f.name, size: f.size, type: f.detected_type || "", bundle: f.bundle || "",
    // path, а не name: имена частей, нарезанных из альбома, повторяются от
    // шкафа к шкафу (у каждого свой «Общий вид»), и по имени сервер открыл бы
    // или удалил не тот файл
    path: f.path, generated: !!f.generated,
  }));
  $("crumb-name").textContent = meta.name;
  $("crumb-status").outerHTML = statusBadge(meta).replace(
    "<span ", '<span id="crumb-status" ');
  renderFiles();
  renderSessionStatus();
  renderLLM();
}


// Путь документа, который скрипты разбирают прямо сейчас (null - никакой).
export function currentDocPath() {
  if (!S.meta || S.meta.status !== "running") return null;
  return (S.meta.progress || {}).path || null;
}

// Что именно считается прямо сейчас: стадия и, для скриптов, текущий лист.
// Для стадии ИИ листа нет и быть не может - агент сам решает, какой файл
// открыть и в каком порядке, и рисовать ему прогресс-бар значило бы врать.
export function progressText(m) {
  const p = m.progress || {};
  const bits = [];
  if (p.doc_total > 1 && p.doc_index) bits.push(`документ ${p.doc_index} из ${p.doc_total}`);
  if (p.document) bits.push(p.document);
  if (p.stage) bits.push(p.stage);
  if (p.page && p.page_total) bits.push(`лист ${p.page} из ${p.page_total}`);
  return bits.join(" · ");
}

export function renderSessionStatus() {
  const m = S.meta;
  if (!m) return;
  if (m.status === "queued") {
    setStatus(m.queue_position
      ? `Ожидает свободного обработчика, ${m.queue_position}-я в очереди. Можно закрыть вкладку.`
      : "Принята к исполнению. Можно закрыть вкладку.", true);
    showCancel(true);
  } else if (m.status === "running") {
    const mode = MODE_LABELS[m.mode] || "полный";
    let text;
    if (m.stage === "очередь к ИИ") {
      // Главное, что здесь надо сказать: скрипты УЖЕ отработали. Иначе
      // ожидание выглядит так, будто ничего не сделано.
      text = m.llm_position
        ? `Скрипты отработали. Ожидание очереди к серверу ИИ, ${m.llm_position}-я.`
        : "Скрипты отработали. Ожидание очереди к серверу ИИ.";
    } else if (m.stage === "ИИ") {
      // У стадии зрения прогресс ЕСТЬ (порядок листов и тайлов выбирает
      // пайплайн), у текстовых агентов его нет и быть не может - там модель
      // сама решает, какой файл открыть. Поэтому не «или-или», а «если есть».
      const detail = progressText(m);
      text = detail ? `Анализ нейросетями: ${detail}` : "Анализ нейросетями…";
    } else {
      const detail = progressText(m);
      text = detail ? `Работают скрипты: ${detail}` : `Идёт анализ… (режим: ${mode})`;
    }
    setStatus(text, true);
    showCancel(true);
  } else {
    showCancel(false);
    if (m.status === "done") {
      setStatus(`Анализ завершён. Замечаний: ${m.n_findings ?? "?"}`, false, "ok");
    } else if (m.status === "error") {
      setStatus("Анализ завершился с ошибкой: " + (m.error || ""), false, "err");
    } else if (m.status === "cancelled") {
      setStatus("Сессия отменена", false, "err");
    } else if (m.status === "interrupted") {
      setStatus("Прогон прерван перезапуском сервера — запустите заново", false, "err");
    } else {
      setStatus("Готово к запуску", false);
    }
  }
  updateRunEnabled();
  updateLogButton();
}

// Кнопка «Лог LM Studio» активна, только когда прогон с участием ИИ уже начался:
// транскрипт пишется на стадиях зрения/агентов, а в режиме «только скрипты» его
// нет вовсе (там сервер ИИ не трогается).
export function updateLogButton() {
  const btn = $("btn-llm-log");
  if (!btn) return;
  const m = S.meta;
  const aiMode = m && ["full", "visual", "full_visual"].includes(m.mode);
  const started = m && !["draft", "queued"].includes(m.status);
  btn.disabled = !(aiMode && started);
}

// Лог тянем по смещению: сервер отдаёт только строки, которых у нас ещё нет.
export async function pumpLog() {
  let data;
  try { data = await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/log?since=${S.logNext}`); }
  catch { return null; }
  for (const line of data.lines) logLine(line, classifyLog(line));
  S.logNext = data.next;
  return data;
}

export function startSessionPolling() {
  if (sessionTimer) clearInterval(sessionTimer);
  sessionTimer = setInterval(async () => {
    const data = await pumpLog();
    if (!data) return;
    const wasStatus = S.meta.status;
    const wasDoc = currentDocPath();
    Object.assign(S.meta, {
      status: data.status, error: data.error,
      n_findings: data.n_findings, queue_position: data.queue_position,
      stage: data.stage, progress: data.progress, llm_position: data.llm_position,
    });
    if (data.status !== wasStatus) {
      $("crumb-status").outerHTML = statusBadge(S.meta).replace(
        "<span ", '<span id="crumb-status" ');
    }
    renderSessionStatus();
    // Список файлов трогаем, только когда сменился разбираемый документ: раз в
    // секунду перестраивать его целиком незачем, а на альбоме в нём полсотни
    // строк с выпадающими списками.
    const doc = currentDocPath();
    if (doc !== wasDoc) {
      // Документа может не быть в списке вовсе: части альбома создаёт сама
      // нарезка, уже ВНУТРИ прогона, а список загружен до его начала (и
      // перед каждым прогоном прошлая нарезка стирается). Тогда список надо
      // перечитать с сервера, иначе подсвечивать нечего - и пользователь
      // вдобавок не видит, на что вообще разрезали его альбом.
      if (doc && !S.files.some((f) => f.path === doc)) await loadSession();
      else renderFiles();
    }
    if (!isBusy()) {
      clearInterval(sessionTimer);
      sessionTimer = null;
      // перечитываем целиком: за прогон появились части альбома, а подсветку
      // и блокировку управления надо снять
      try { await loadSession(); } catch { renderFiles(); }
      if (data.status === "done") await showReport();
    }
  }, 1000);
}

// ------------------------------------------------------------
// Файлы сессии: загрузка, отображение, выбор типа
// ------------------------------------------------------------
export function renderFiles() {
  const list = $("file-list");
  const empty = $("file-empty");
  const note = $("file-note");
  if (S.files.length === 0) {
    list.innerHTML = "";
    empty.style.display = "block";
    note.classList.remove("show");
    updateRunEnabled();
    return;
  }
  empty.style.display = "none";
  note.classList.add("show");
  const locked = isBusy();
  list.innerHTML = S.files.map((f, i) => {
    const opts = ['<option value="">— укажите тип —</option>']
      .concat(DOC_TYPES.map((t) =>
        `<option value="${t.key}" ${f.type === t.key ? "selected" : ""}>${esc(t.title)}</option>`))
      .join("");
    // Связку показываем, ТОЛЬКО если файл лежит в подпапке: в обычном случае
    // все документы сессии - один проект, и подпись "проект" на каждой строке
    // ничего не сообщает (об этом сказано один раз под списком).
    const bundle = f.bundle
      ? `<span class="fi-bundle" title="Отдельная связка (подпапка): ${esc(f.bundle)}">${esc(f.bundle)}</span>`
      : "";
    // Часть, нарезанная из альбома, а не загруженная руками. Без этой пометки
    // пользователь видит полтора десятка файлов, которых он не загружал, и
    // не понимает, откуда они и можно ли их трогать.
    const gen = f.generated
      ? `<span class="fi-gen" title="Этот документ вырезан из загруженного альбома. Удалять по одному не нужно — при следующем запуске альбом будет нарезан заново">из альбома</span>`
      : "";
    // Документ, который скрипты разбирают прямо сейчас. Сверяем по пути, а не
    // по имени: в альбоме у каждого шкафа своё «Общий вид», и по имени
    // подсветилось бы сразу несколько строк.
    const active = currentDocPath() === f.path;
    const activeTag = active ? `<span class="fi-active" title="Этот документ анализируется прямо сейчас">разбирается</span>` : "";
    // Имя - ссылка: открыть исходный документ в соседней вкладке. Это первое,
    // что делает инженер, увидев замечание, и раньше ради этого приходилось
    // искать файл в проводнике.
    const href = `/api/sessions/${encodeURIComponent(S.id)}/file?path=${encodeURIComponent(f.path)}`;
    return `
      <li class="file-item${active ? " analyzing" : ""}">
        <span class="fi-icon">▤</span>
        <a class="fi-name" href="${href}" target="_blank" rel="noopener"
           title="Открыть «${esc(f.name)}» в новой вкладке">${esc(f.name)}</a>
        ${activeTag}${bundle}${gen}
        <span class="fi-size">${fmtSize(f.size)}</span>
        <select data-idx="${i}" class="${f.type ? "" : "unset"}" ${locked ? "disabled" : ""}>${opts}</select>
        <button class="fi-del" data-del="${i}" title="Удалить файл из сессии"
                ${locked ? "disabled" : ""}>✕</button>
      </li>`;
  }).join("");

  list.querySelectorAll("select").forEach((sel) => {
    sel.addEventListener("change", (e) => {
      const idx = +e.target.dataset.idx;
      S.files[idx].type = e.target.value;
      e.target.classList.toggle("unset", !e.target.value);
      updateRunEnabled();
      saveType(S.files[idx], e.target.value);
    });
  });
  list.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", () => deleteFile(S.files[+btn.dataset.del]));
  });
  updateRunEnabled();
}

// Тип сохраняем на сервере сразу при изменении: пометка живёт в session.json и
// переживает и перезагрузку страницы, и переход в список сессий и обратно.
// Адресуем файл ПУТЁМ, как просмотр и удаление: у частей альбома имена
// повторяются от шкафа к шкафу, и по имени пометка легла бы сразу на все.
export async function saveType(file, type) {
  try {
    await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/set-type`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: file.path, type }),
    });
  } catch (e) {
    logLine(`Не удалось сохранить тип для ${file.name}: ${e.message}`, "warn");
  }
}

export async function deleteFile(file) {
  try {
    await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/file-delete`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: file.path }),
    });
    await loadSession();
    logLine("Файл удалён из сессии: " + file.name, "warn");
  } catch (e) {
    logLine("Не удалось удалить файл: " + e.message, "err");
  }
}

// Принимаемые расширения. Должны совпадать с ALLOWED_SUFFIXES в sessions.py:
// .xlsx нужен спецификации (СО) - единственному документу связки не в PDF.
export const ACCEPTED_EXT = [".pdf", ".xlsx", ".xlsm"];

export async function uploadFiles(fileList) {
  if (!S.id) return;
  const form = new FormData();
  let n = 0;
  for (const f of fileList) {
    const name = f.name.toLowerCase();
    if (!ACCEPTED_EXT.some((ext) => name.endsWith(ext))) continue;
    if (f.name.startsWith("~$")) continue;   // временный файл открытой книги Excel
    form.append("files", f, f.name);
    n++;
  }
  if (n === 0) {
    logLine("Среди выбранного нет подходящих файлов: нужен PDF (схема, чертёж) "
            + "или XLSX (спецификация).", "warn");
    return;
  }

  setStatus("Загрузка файлов…", true);
  try {
    // через fetchJSON: он один умеет отличать ошибку-JSON от страницы HTML,
    // которой отвечает send_error. Content-Type не ставим - его с границей
    // multipart проставит сам браузер по FormData.
    const data = await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/upload`,
                                 { method: "POST", body: form });
    logLine(`Загружено файлов: ${data.saved.length}` +
      (data.skipped.length ? `, пропущено: ${data.skipped.length}` : ""), "ok");
    await loadSession();
    setStatus("Файлы загружены. Укажите тип каждого и поставьте сессию в очередь.", false);
  } catch (e) {
    logLine("Ошибка загрузки: " + e.message, "err");
    setStatus("Ошибка загрузки", false);
  }
}

// Кнопка запуска активна, только когда все файлы имеют указанный тип
export function updateRunEnabled() {
  const ready = S.files.length > 0 && S.files.every((f) => f.type) && !isBusy();
  $("run-btn").disabled = !ready;
  $("run-toggle").disabled = !ready;
}

// ------------------------------------------------------------
// Постановка в очередь и отмена
// ------------------------------------------------------------
export async function enqueue(mode) {
  closeRunMenu();
  askNotifyPermission();
  $("report-section").classList.remove("show");
  logLine(`--- Сессия ставится в очередь (режим: ${MODE_LABELS[mode] || mode}) ---`, "ok");
  try {
    const data = await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/enqueue`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    S.meta.status = "queued";
    S.meta.mode = mode;
    S.meta.queue_position = data.queue_position;
    renderFiles();          // на время прогона файлы менять нельзя
    renderSessionStatus();
    startSessionPolling();
  } catch (e) {
    logLine("Не удалось поставить в очередь: " + e.message, "err");
    setStatus("Ошибка запуска", false, "err");
  }
}

// Отмена: сервер снимает сессию с очереди либо убивает процесс пайплайна
// целиком, со всеми потомками - кнопку блокируем лишь на время самого запроса.
export async function cancelCurrent() {
  const btn = $("cancel-btn");
  btn.disabled = true;
  btn.textContent = "Останавливаем…";
  logLine("--- Запрошена отмена ---", "warn");
  await cancelSession(S.id, null);
  try { await loadSession(); } catch { /* сессию могли удалить */ }
  await pumpLog();
  btn.textContent = "Отменить";
}

export function showCancel(on) {
  const btn = $("cancel-btn");
  if (!btn) return;
  btn.style.display = on ? "" : "none";
  if (on) { btn.disabled = false; btn.textContent = "Отменить"; }
}

export async function renameSession() {
  const name = prompt("Новое название сессии:", S.meta ? S.meta.name : "");
  if (!name) return;
  try {
    await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/rename`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    await loadSession();
  } catch (e) {
    alert("Не удалось переименовать: " + e.message);
  }
}


// Меню выбора режима у кнопки запуска - часть этого экрана.
export function toggleRunMenu() {
  const menu = $("run-menu");
  const opened = menu.classList.toggle("open");
  // Меню выпадает ВНИЗ и с появлением визуальных режимов выросло вдвое (288 px
  // против 134). Открытое у нижнего края экрана, оно оставляет последний пункт
  // за сгибом - а пользователь не знает, что там что-то есть, и не догадается
  // прокрутить. Подтягиваем меню в видимую область целиком.
  //
  // Прокрутка - СИНХРОННО и БЕЗ behavior: "smooth". Оба откладывания были
  // проверены и оба молча не срабатывали: плавную прокрутку браузер вправе
  // проигнорировать (например, при включённом «уменьшении анимации»), а
  // колбэк requestAnimationFrame не вызывается в неотрисовываемой вкладке.
  // Класс .open применяется тут же, размеры у меню появляются сразу -
  // откладывать нечего.
  if (opened) menu.scrollIntoView({ block: "nearest" });
}
export function closeRunMenu() { $("run-menu").classList.remove("open"); }
