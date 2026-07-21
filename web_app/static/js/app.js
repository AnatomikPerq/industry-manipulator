/* ============================================================
   Анализатор проектной документации - точка входа фронтенда.

   Два экрана на hash-роутинге, без фреймворка и без сборки:
     #/        - список сессий (общий для всех, обновляется сам)
     #/s/<id>  - одна сессия: файлы, запуск, консоль, отчёт

   Разложено по модулям (ES modules, грузятся браузером как есть):
     util.js    - DOM, форматы, запрос к API, консоль, тосты
     state.js   - состояние вкладки и подпись статуса
     list.js    - экран списка сессий
     session.js - экран одной сессии
     report.js  - таблицы находок и фрагменты чертежа
   Здесь остались роутинг, инициализация, наблюдатель завершения и
   привязка событий - то, что связывает экраны между собой.
   ============================================================ */

import { DOC_TYPES, S, setDocTypes } from "./state.js";
import { createSession, deleteSession, cancelSession, refreshSessions,
         toggleNewSession } from "./list.js";
import { cancelCurrent, closeRunMenu, enqueue, openSession, renameSession,
         stopSessionPolling, toggleRunMenu, uploadFiles } from "./session.js";
import { closeFragments, downloadReportPdf, showReport } from "./report.js";
import { $, esc, fetchJSON, fmtCtx, joinNonEmpty, logLine, showToast, showView }
  from "./util.js";

// поллинг открытой сессии и списка живёт в своих модулях; здесь только
// таймеры, которые роутер обязан гасить при смене экрана
let listTimer = null;
let watchTimer = null;

async function init() {
  try {
    const cfg = await fetchJSON("/api/config");
    // ИМЕННО setDocTypes, а не присваивание. DOC_TYPES - импортированная
    // константа-массив, и он МУТИРУЕТСЯ на месте (state.js): модули, которые
    // импортировали его один раз, обязаны видеть те же данные. Присваивание
    // здесь роняло init() с «Assignment to constant variable» ещё до
    // renderDocTypes() - и список принимаемых документов оставался пуст, а
    // выпадающий список типов файла - без единого пункта. Ошибка при этом
    // выглядела как отказ сервера («не удалось загрузить конфигурацию»),
    // хотя сервер отвечал исправно.
    setDocTypes(cfg.doc_types);
    $("version-badge").textContent = cfg.version;
    $("meta-version").textContent = cfg.version;
    $("footer-version").textContent = cfg.version;
    renderDocTypes();
  } catch (e) {
    logLine("Не удалось загрузить конфигурацию: " + e.message, "err");
  }
  bindEvents();
  window.addEventListener("hashchange", route);
  startFinishWatcher();
  await route();
}

// ------------------------------------------------------------
// Уведомления о завершении анализа - ЛЮБОЙ сессии, не только открытой.
//
// Прогон живёт на сервере, и вкладка про него ничего не знает, пока не
// спросит. Открытая сессия поллится своим таймером, список - своим, но оба
// живут только на своём экране. Этот наблюдатель работает ВСЕГДА: раз в
// несколько секунд сравнивает статусы всех сессий с прошлым разом и, увидев
// переход "считалась -> завершилась", показывает тост в углу и системное
// уведомление (если вкладка не в фокусе). Так можно уйти в другую сессию или
// свернуть браузер и всё равно узнать, что чужой сорокаминутный прогон готов.
// ------------------------------------------------------------

const FINISHED = { done: 1, error: 1, cancelled: 1, interrupted: 1 };
let watchedStatuses = null;   // id -> статус на прошлом тике (null = первый тик)

function startFinishWatcher() {
  if (watchTimer) clearInterval(watchTimer);
  watchTimer = setInterval(async () => {
    let data;
    try { data = await fetchJSON("/api/sessions"); }
    catch { return; }
    const now = new Map();
    for (const s of data.sessions || []) now.set(s.id, s);

    // Первый тик - только запомнить: сессии, завершившиеся до открытия
    // вкладки, новостью не являются.
    if (watchedStatuses !== null) {
      for (const [id, s] of now) {
        const was = watchedStatuses.get(id);
        const active = was === "queued" || was === "running";
        if (active && FINISHED[s.status]) notifyFinished(s);
      }
    }
    watchedStatuses = new Map([...now].map(([id, s]) => [id, s.status]));
  }, 3000);
}

function notifyFinished(s) {
  const what = {
    done: s.n_findings != null ? `завершён. Замечаний: ${s.n_findings}` : "завершён",
    error: "завершился с ошибкой",
    cancelled: "отменён",
    interrupted: "прерван перезапуском сервера",
  }[s.status] || "завершён";
  const text = `Анализ «${s.name}» ${what}`;
  const kind = s.status === "done" ? "ok" : "err";
  showToast(text, kind, s.id);

  // Системное уведомление - только когда вкладка не на глазах: смотрящему на
  // страницу хватает тоста, а дублировать его баннером поверх других окон незачем.
  if ("Notification" in window && Notification.permission === "granted"
      && !document.hasFocus()) {
    const n = new Notification("Анализатор документации", { body: text });
    n.onclick = () => {
      window.focus();
      location.hash = "#/s/" + encodeURIComponent(s.id);
      n.close();
    };
  }
}

// Разрешение на системные уведомления спрашивается в момент запуска анализа:
// это клик пользователя (браузер не даст спросить без него), и именно тогда
// уведомление становится нужным - прогон долгий, вкладку закроют.

async function route() {
  stopTimers();
  const m = location.hash.match(/^#\/s\/([^/]+)/);
  if (m) {
    await openSession(decodeURIComponent(m[1]));
  } else {
    showView("list");
    await refreshSessions();
    listTimer = setInterval(refreshSessions, 2000);
  }
}


function stopTimers() {
  // Роутер гасит таймеры ОБОИХ экранов: поллинг сессии заводит session.js и
  // гасит тоже он, а список - здесь. Наблюдатель завершения (watchTimer) не
  // трогаем: он единственный, кто работает всегда, на любом экране.
  if (listTimer) { clearInterval(listTimer); listTimer = null; }
  stopSessionPolling();
}

function renderDocTypes() {
  $("doc-types").innerHTML = DOC_TYPES.map((t) => `
    <li>
      <span class="dot"></span>
      <div>
        <div class="t-title">${esc(t.title)}</div>
        <div class="t-hint">${esc(t.hint)}</div>
      </div>
    </li>`).join("");
}

// ------------------------------------------------------------
// Проверка серверов и моделей ИИ
// ------------------------------------------------------------
async function checkLLM() {
  const panel = $("llm-panel");
  panel.classList.add("show");
  panel.innerHTML = `<div class="hint-text">Проверка серверов ИИ…</div>`;
  logLine("Проверка серверов и моделей ИИ…", "");
  try {
    const data = await fetchJSON("/api/check-llm");
    if (data.error) throw new Error(data.error);
    panel.innerHTML = data.servers.map(renderServer).join("");
    logLine("Проверка ИИ завершена.", "ok");
  } catch (e) {
    panel.innerHTML = `<div class="llm-server"><span class="pill pill-down">● недоступно</span> ${esc(e.message)}</div>`;
    logLine("Проверка ИИ: " + e.message, "err");
  }
}

function renderServer(srv) {
  const head = `
    <div class="srv-head">
      ${srv.reachable
        ? `<span class="pill pill-up">● сервер доступен</span>`
        : `<span class="pill pill-down">● сервер недоступен</span>`}
      <span class="mono">${esc(srv.base_url)}</span>
    </div>`;
  if (!srv.reachable) {
    return `<div class="llm-server">${head}<div class="hint-text">${esc(srv.error || "нет ответа")}</div></div>`;
  }
  const models = srv.models.length === 0
    ? `<div class="hint-text">Сервер не вернул ни одной модели.</div>`
    : srv.models.map((m) => {
        const loadedTag = m.loaded === true
          ? `<span class="tag tag-loaded">загружена</span>`
          : m.loaded === false ? `<span class="tag tag-idle">не загружена</span>` : "";
        const wantTag = m.wanted ? `<span class="tag tag-wanted">нужна проекту</span>` : "";
        const meta = joinNonEmpty([m.params, m.max_context ? "ctx " + fmtCtx(m.max_context) : null], " · ");
        return `
          <div class="model-row">
            <span class="m-name">${m.wanted ? "<b>" : ""}${esc(m.display_name)}${m.wanted ? "</b>" : ""}
              ${meta ? `<span class="hint-text" style="display:inline">— ${esc(meta)}</span>` : ""}</span>
            ${wantTag}${loadedTag}
          </div>`;
      }).join("");
  return `<div class="llm-server">${head}${models}</div>`;
}

// ------------------------------------------------------------
// События
// ------------------------------------------------------------
function bindEvents() {
  $("btn-new-session").addEventListener("click", () => toggleNewSession(true));
  $("btn-create-cancel").addEventListener("click", () => toggleNewSession(false));
  $("btn-create").addEventListener("click", createSession);
  $("new-session-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") createSession();
    if (e.key === "Escape") toggleNewSession(false);
  });
  $("btn-rename").addEventListener("click", renameSession);

  const dz = $("dropzone");
  const input = $("file-input");
  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", (e) => { uploadFiles(e.target.files); input.value = ""; });
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });

  $("run-btn").addEventListener("click", () => enqueue("full"));
  $("run-toggle").addEventListener("click", (e) => { e.stopPropagation(); toggleRunMenu(); });
  $("run-menu").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => enqueue(b.dataset.mode)));
  document.addEventListener("click", closeRunMenu);

  $("cancel-btn").addEventListener("click", cancelCurrent);
  $("btn-check-llm").addEventListener("click", checkLLM);
  $("btn-show-report").addEventListener("click", showReport);
  $("btn-clear-console").addEventListener("click", () => { $("console").innerHTML = ""; });
  $("btn-report-pdf").addEventListener("click", downloadReportPdf);

  $("frag-close").addEventListener("click", closeFragments);
  $("frag-modal").addEventListener("click", (e) => {
    if (e.target === $("frag-modal")) closeFragments();   // клик по фону
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeFragments();
  });
}


init();
