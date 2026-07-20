/* ============================================================
   Анализатор проектной документации - фронтенд

   Два экрана на hash-роутинге, без фреймворка и сборки:
     #/        - список сессий (общий для всех, обновляется сам)
     #/s/<id>  - одна сессия: файлы, запуск, консоль, отчёт

   Состояние сессии живёт на сервере, в session.json, а не во вкладке:
   вкладку можно закрыть, сессия останется в очереди и досчитается.
   ============================================================ */

const $ = (id) => document.getElementById(id);
let DOC_TYPES = [];          // список принимаемых типов (с бэкенда)

// Текущая открытая сессия. files - её файлы, logNext - номер строки лога,
// на которой мы остановились (лог тянем порциями, а не целиком).
let S = { id: null, meta: null, files: [], logNext: 0 };
let sessionTimer = null;     // поллинг открытой сессии
let listTimer = null;        // автообновление списка сессий

const STATUS_LABELS = {
  draft: "черновик", queued: "в очереди", running: "выполняется",
  done: "готово", error: "ошибка", cancelled: "отменена",
  interrupted: "прервана",
};

// ------------------------------------------------------------
// Инициализация и роутинг
// ------------------------------------------------------------
async function init() {
  try {
    const cfg = await fetchJSON("/api/config");
    DOC_TYPES = cfg.doc_types;
    $("version-badge").textContent = cfg.version;
    $("meta-version").textContent = cfg.version;
    $("footer-version").textContent = cfg.version;
    renderDocTypes();
  } catch (e) {
    logLine("Не удалось загрузить конфигурацию: " + e.message, "err");
  }
  bindEvents();
  window.addEventListener("hashchange", route);
  await route();
}

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

function showView(which) {
  $("view-list").classList.toggle("show", which === "list");
  $("view-session").classList.toggle("show", which === "session");
}

function stopTimers() {
  if (listTimer) { clearInterval(listTimer); listTimer = null; }
  if (sessionTimer) { clearInterval(sessionTimer); sessionTimer = null; }
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
// Экран 1: список сессий
// ------------------------------------------------------------
async function refreshSessions() {
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
  const nRunning = (data.running || []).length;
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

function renderSessionCard(s) {
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

function statusBadge(s) {
  let label = STATUS_LABELS[s.status] || s.status;
  if (s.status === "queued" && s.queue_position) {
    label = `в очереди, ${s.queue_position}-я`;
  } else if (s.status === "running" && s.stage === "очередь к ИИ") {
    label = s.llm_position ? `ждёт ИИ, ${s.llm_position}-я` : "ждёт ИИ";
  } else if (s.status === "running" && s.stage) {
    label = s.stage === "ИИ" ? "анализ ИИ" : "работают скрипты";
  }
  return `<span class="status-badge st-${esc(s.status)}">${esc(label)}</span>`;
}

function toggleNewSession(on) {
  $("new-session").classList.toggle("hidden", !on);
  if (on) { $("new-session-name").value = ""; $("new-session-name").focus(); }
}

async function createSession() {
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

async function deleteSession(id) {
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
async function cancelSession(id, after) {
  try {
    await fetchJSON(`/api/sessions/${encodeURIComponent(id)}/cancel`, { method: "POST" });
  } catch (e) {
    alert("Не удалось отменить: " + e.message);
  }
  if (after) await after();
}

// ------------------------------------------------------------
// Экран 2: одна сессия
// ------------------------------------------------------------
async function openSession(id) {
  S = { id, meta: null, files: [], logNext: 0 };
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

async function loadSession() {
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
}

function isBusy() {
  return S.meta && (S.meta.status === "queued" || S.meta.status === "running");
}

// Что именно считается прямо сейчас: стадия и, для скриптов, текущий лист.
// Для стадии ИИ листа нет и быть не может - агент сам решает, какой файл
// открыть и в каком порядке, и рисовать ему прогресс-бар значило бы врать.
function progressText(m) {
  const p = m.progress || {};
  const bits = [];
  if (p.doc_total > 1 && p.doc_index) bits.push(`документ ${p.doc_index} из ${p.doc_total}`);
  if (p.document) bits.push(p.document);
  if (p.stage) bits.push(p.stage);
  if (p.page && p.page_total) bits.push(`лист ${p.page} из ${p.page_total}`);
  return bits.join(" · ");
}

function renderSessionStatus() {
  const m = S.meta;
  if (!m) return;
  if (m.status === "queued") {
    setStatus(m.queue_position
      ? `Ожидает свободного обработчика, ${m.queue_position}-я в очереди. Можно закрыть вкладку.`
      : "Принята к исполнению. Можно закрыть вкладку.", true);
    showCancel(true);
  } else if (m.status === "running") {
    const mode = m.mode === "scripts" ? "без ИИ" : "полный";
    let text;
    if (m.stage === "очередь к ИИ") {
      // Главное, что здесь надо сказать: скрипты УЖЕ отработали. Иначе
      // ожидание выглядит так, будто ничего не сделано.
      text = m.llm_position
        ? `Скрипты отработали. Ожидание очереди к серверу ИИ, ${m.llm_position}-я.`
        : "Скрипты отработали. Ожидание очереди к серверу ИИ.";
    } else if (m.stage === "ИИ") {
      text = "Анализ нейросетями…";
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
}

// Лог тянем по смещению: сервер отдаёт только строки, которых у нас ещё нет.
async function pumpLog() {
  let data;
  try { data = await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/log?since=${S.logNext}`); }
  catch { return null; }
  for (const line of data.lines) logLine(line, classifyLog(line));
  S.logNext = data.next;
  return data;
}

function startSessionPolling() {
  if (sessionTimer) clearInterval(sessionTimer);
  sessionTimer = setInterval(async () => {
    const data = await pumpLog();
    if (!data) return;
    const wasStatus = S.meta.status;
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
    if (!isBusy()) {
      clearInterval(sessionTimer);
      sessionTimer = null;
      if (data.status === "done") await showReport();
    }
  }, 1000);
}

// ------------------------------------------------------------
// Файлы сессии: загрузка, отображение, выбор типа
// ------------------------------------------------------------
function renderFiles() {
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
    // Имя - ссылка: открыть исходный документ в соседней вкладке. Это первое,
    // что делает инженер, увидев замечание, и раньше ради этого приходилось
    // искать файл в проводнике.
    const href = `/api/sessions/${encodeURIComponent(S.id)}/file?path=${encodeURIComponent(f.path)}`;
    return `
      <li class="file-item">
        <span class="fi-icon">▤</span>
        <a class="fi-name" href="${href}" target="_blank" rel="noopener"
           title="Открыть «${esc(f.name)}» в новой вкладке">${esc(f.name)}</a>
        ${bundle}${gen}
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
      saveType(S.files[idx].name, e.target.value);
    });
  });
  list.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", () => deleteFile(S.files[+btn.dataset.del]));
  });
  updateRunEnabled();
}

// Тип сохраняем на сервере сразу при изменении: пометка живёт в session.json и
// переживает и перезагрузку страницы, и переход в список сессий и обратно.
async function saveType(name, type) {
  try {
    await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/set-type`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, type }),
    });
  } catch (e) {
    logLine(`Не удалось сохранить тип для ${name}: ${e.message}`, "warn");
  }
}

async function deleteFile(file) {
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
const ACCEPTED_EXT = [".pdf", ".xlsx", ".xlsm"];

async function uploadFiles(fileList) {
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
    const res = await fetch(`/api/sessions/${encodeURIComponent(S.id)}/upload`,
                            { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
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
function updateRunEnabled() {
  const ready = S.files.length > 0 && S.files.every((f) => f.type) && !isBusy();
  $("run-btn").disabled = !ready;
  $("run-toggle").disabled = !ready;
}

// ------------------------------------------------------------
// Постановка в очередь и отмена
// ------------------------------------------------------------
async function enqueue(mode) {
  closeRunMenu();
  $("report-section").classList.remove("show");
  logLine(`--- Сессия ставится в очередь (режим: ${mode === "scripts" ? "без ИИ" : "полный"}) ---`, "ok");
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
async function cancelCurrent() {
  const btn = $("cancel-btn");
  btn.disabled = true;
  btn.textContent = "Останавливаем…";
  logLine("--- Запрошена отмена ---", "warn");
  await cancelSession(S.id, null);
  try { await loadSession(); } catch { /* сессию могли удалить */ }
  await pumpLog();
  btn.textContent = "Отменить";
}

function showCancel(on) {
  const btn = $("cancel-btn");
  if (!btn) return;
  btn.style.display = on ? "" : "none";
  if (on) { btn.disabled = false; btn.textContent = "Отменить"; }
}

async function renameSession() {
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

// ------------------------------------------------------------
// Отчёт (таблица в стиле примеров)
// ------------------------------------------------------------
async function showReport() {
  let data;
  try { data = await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/report`); }
  catch (e) { logLine("Отчёт недоступен: " + e.message, "warn"); return; }
  renderReport(data);
  $("report-section").classList.add("show");
  $("report-section").scrollIntoView({ behavior: "smooth", block: "start" });
}

// Отчёт одним PDF: его пересылают, показывают на совещании и подшивают к
// проекту. Собирает сервер (report_pdf.py) - в начало кладётся описание того,
// что и как проверялось.
function downloadReportPdf() {
  if (!S.id) return;
  window.open(`/api/sessions/${encodeURIComponent(S.id)}/report.pdf`, "_blank");
}

const SEV_LABELS = { critical: "критич.", high: "высокий", medium: "средний", low: "низкий", info: "инфо" };
const SEV_ORDER = ["critical", "high", "medium", "low", "info"];

function renderReport(data) {
  const errors = data.errors || [];
  // Запоминаем отчёт и помечаем каждую находку её номером в общем списке:
  // ниже находки раскладываются по двум таблицам, и после фильтрации номер
  // строки уже не совпадает с номером находки, а кнопке «фрагмент» нужен
  // именно он.
  S.report = data;
  errors.forEach((e, i) => { e.__i = i; });
  $("report-summary").textContent = data.summary || "Анализ завершён.";

  // статистика по важности
  const counts = {};
  errors.forEach((e) => { counts[e.severity] = (counts[e.severity] || 0) + 1; });
  const stats = [`<div class="stat"><div class="s-num">${errors.length}</div><div class="s-lbl">всего</div></div>`];
  SEV_ORDER.forEach((sev) => {
    if (counts[sev]) stats.push(
      `<div class="stat sev-${sev}"><div class="s-num">${counts[sev]}</div><div class="s-lbl">${SEV_LABELS[sev]}</div></div>`);
  });
  $("report-stats").innerHTML = stats.join("");

  if (errors.length === 0) {
    $("report-body").innerHTML = `<div class="no-issues">✓ Замечаний не найдено.</div>`;
    return;
  }

  // Находки бывают двух совершенно разных родов, и одной таблицей их не показать:
  // у сверки "таблица подключений <-> схема" место находки описывается клеммой,
  // штифтом и маркировкой провода, а у находок по спецификации и сборочному
  // чертежу - позиционным обозначением, артикулом и количеством. Поэтому таблицы
  // две, и находка попадает в ту, чьи колонки для неё осмысленны (по составу
  // документов в refs, а не по scope: внутренняя ошибка спецификации - тоже
  // "элементная" находка, и колонки клемм ей пусты).
  const isBundle = (e) => (e.refs || []).some(
    (r) => r.doc_type === "spec" || r.doc_type === "assembly");
  const bundleErrors = errors.filter(isBundle);
  const wiringErrors = errors.filter((e) => !isBundle(e));

  const parts = [];
  if (bundleErrors.length) parts.push(renderBundleTable(bundleErrors));
  if (wiringErrors.length) parts.push(renderWiringTable(wiringErrors));
  $("report-body").innerHTML = parts.join("");

  $("report-body").querySelectorAll("[data-frag]").forEach((btn) => {
    btn.addEventListener("click", () => openFragments(+btn.dataset.frag));
  });
}

// ------------------------------------------------------------
// Фрагмент чертежа у находки
//
// Прочитав «обозначение QF1 есть на схеме, но его нет в спецификации», инженер
// первым делом лезет в PDF смотреть это место. Показываем его сразу: сервер
// ищет обозначение в исходном документе и отдаёт вырезанный кусок листа
// с обведённым попаданием (см. analyzer_to_errors/fragment.py).
// ------------------------------------------------------------

// Документы, у которых есть листы. Спецификация приходит книгой Excel -
// показывать в ней нечего, и кнопку для неё рисовать не надо.
const FRAGMENT_DOC_TYPES = ["scheme", "assembly", "netlist"];

function fragmentTargets(err) {
  const seen = new Set();
  const out = [];
  for (const ref of err.refs || []) {
    if (!FRAGMENT_DOC_TYPES.includes(ref.doc_type) || !ref.document) continue;
    // Порядок ключей тот же, что в fragment.needles_from_ref: от точного
    // (артикул) к общему (обозначение клеммника).
    const q = ["article", "designator", "marking", "kks", "terminal_block"]
      .map((k) => ref[k]).filter((v) => v !== null && v !== undefined && v !== "");
    if (!q.length) continue;
    const key = `${ref.document}|${ref.sheet}|${q[0]}`;
    if (seen.has(key)) continue;      // дубль клеммы даёт два одинаковых ref'а
    seen.add(key);
    out.push({ document: ref.document, sheet: ref.sheet, q, doc_type: ref.doc_type });
  }
  return out;
}

function fragmentButton(err) {
  if (!fragmentTargets(err).length) return "";
  return `<button class="frag-btn" data-frag="${err.__i}"
            title="Показать это место на чертеже">фрагмент</button>`;
}

async function openFragments(index) {
  const err = ((S.report || {}).errors || [])[index];
  if (!err) return;
  const targets = fragmentTargets(err);

  $("frag-title").textContent = err.type || "Место находки";
  $("frag-sub").textContent = err.finding || "";
  $("frag-body").innerHTML = `<div class="hint-text">Готовим фрагменты…</div>`;
  $("frag-modal").classList.add("show");

  // Два ref'а находки нередко приводят к ОДНОМУ И ТОМУ ЖЕ куску листа: у
  // «изделие пропало с парного листа» второй ref указывает на лист, где
  // изделия нет, и поиск честно возвращает тот же лист, что и первый.
  // Картинку показываем один раз, но пометку «на листе N не найдено» с
  // отброшенного дубля ПЕРЕНОСИМ на неё: в ней вся суть такой находки.
  const blocks = [];
  const byKey = new Map();      // ключ картинки -> её место в blocks
  const missing = new Map();    // ключ картинки -> листы, где ключа не нашлось
  for (const t of targets) {
    const params = new URLSearchParams();
    params.set("document", t.document);
    if (t.sheet !== null && t.sheet !== undefined) params.set("sheet", t.sheet);
    t.q.forEach((v) => params.append("q", v));
    const url = `/api/sessions/${encodeURIComponent(S.id)}/fragment?${params}`;
    const asked = `${esc(t.document)}${t.sheet != null ? `, лист ${esc(t.sheet)}` : ""}`
                + ` — ищем ${esc(t.q[0])}`;
    try {
      const res = await fetch(url);
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || res.statusText);
      }
      // Подпись называет лист, который РЕАЛЬНО на картинке. Если на листе из
      // находки ключа не оказалось и показан другой - говорим об этом прямо:
      // на находках «изделие пропало с парного листа» именно отсутствие и
      // есть суть, и молчаливая подмена листа выглядела бы её опровержением.
      const page = res.headers.get("X-Fragment-Page");
      const fallback = res.headers.get("X-Fragment-Fallback") === "1";
      const key = `${t.document}|${page}|${t.q[0]}`;

      if (fallback && t.sheet != null) {
        if (!missing.has(key)) missing.set(key, new Set());
        missing.get(key).add(String(t.sheet));
      }
      if (byKey.has(key)) continue;        // ту же картинку второй раз не рисуем

      const src = URL.createObjectURL(await res.blob());
      byKey.set(key, blocks.length);
      blocks.push({
        key,
        head: `${esc(t.document)}, лист ${esc(page)} — ${esc(t.q[0])}`,
        page,
        html: `<a href="${src}" target="_blank" rel="noopener">
                 <img src="${src}" alt="Фрагмент чертежа"></a>`,
      });
    } catch (e) {
      blocks.push({ key: null, head: asked,
                    html: `<div class="frag-fail">${esc(e.message)}</div>` });
    }
  }

  const html = blocks.map((b) => {
    const gone = b.key ? missing.get(b.key) : null;
    const warn = gone && gone.size
      ? `<span class="frag-warn">на ${gone.size > 1 ? "листах" : "листе"} `
        + `${esc([...gone].join(", "))} не найдено — показан лист ${esc(b.page)},`
        + ` где оно есть</span>`
      : "";
    return `<div class="frag-item"><div class="frag-head">${b.head}${warn}</div>
              ${b.html}</div>`;
  }).join("");

  $("frag-body").innerHTML = html ||
    `<div class="frag-fail">У этой находки нет документа с листами.</div>`;
}

function closeFragments() {
  $("frag-modal").classList.remove("show");
  $("frag-body").innerHTML = "";
}

function renderWiringTable(errors) {
  const rows = errors.map((e, i) => renderRow(e, i + 1)).join("");
  return `
    ${sectionTitle("Таблица подключений и схема", errors.length)}
    <div class="table-wrap">
      <table class="report">
        <thead>
          <tr>
            <th class="grp-meta" rowspan="2">№</th>
            <th class="grp-meta" rowspan="2">Вид</th>
            <th class="grp-meta" rowspan="2">Важность</th>
            <th class="grp-source" colspan="6">Таблица подключений</th>
            <th class="grp-install" colspan="5">Монтажная документация (схема)</th>
            <th class="grp-out" colspan="2">Вывод</th>
          </tr>
          <tr>
            <th class="sub-source">Лист / строка</th>
            <th class="sub-source">Шкаф</th>
            <th class="sub-source">Клемма / штифт</th>
            <th class="sub-source">Маркировка</th>
            <th class="sub-source">KKS</th>
            <th class="sub-source">Проводник</th>
            <th class="sub-install">Лист</th>
            <th class="sub-install">Клемма / штифт</th>
            <th class="sub-install">Маркировка</th>
            <th class="sub-install">KKS</th>
            <th class="sub-install">Проводник</th>
            <th>Что найдено</th>
            <th>Что требуется</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderBundleTable(errors) {
  const rows = errors.map((e, i) => renderBundleRow(e, i + 1)).join("");
  return `
    ${sectionTitle("Связка: спецификация, сборочный чертёж, схема", errors.length)}
    <div class="table-wrap">
      <table class="report">
        <thead>
          <tr>
            <th class="grp-meta" rowspan="2">№</th>
            <th class="grp-meta" rowspan="2">Вид</th>
            <th class="grp-meta" rowspan="2">Важность</th>
            <th class="grp-meta" rowspan="2">Позиция</th>
            <th class="grp-source" colspan="3">Спецификация (СО)</th>
            <th class="grp-install" colspan="2">Сборочный чертёж (СБ)</th>
            <th class="grp-scheme" colspan="2">Схема (Э3)</th>
            <th class="grp-out" colspan="2">Вывод</th>
          </tr>
          <tr>
            <th class="sub-source">Строка</th>
            <th class="sub-source">Артикул</th>
            <th class="sub-source">Кол-во</th>
            <th class="sub-install">Лист</th>
            <th class="sub-install">Артикул</th>
            <th class="sub-scheme">Лист</th>
            <th class="sub-scheme">Артикул</th>
            <th>Что найдено</th>
            <th>Что требуется</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function sectionTitle(text, n) {
  return `<h3 class="report-group">${esc(text)} <span class="report-group-n">${n}</span></h3>`;
}

function renderBundleRow(err, num) {
  const sp = refOf(err, "spec");
  const asm = refOf(err, "assembly");
  const sc = refOf(err, "scheme");
  const sev = err.severity || "info";

  const cell = (v, mono) => v === null || v === undefined || v === ""
    ? `<td class="empty-cell">—</td>`
    : `<td class="${mono ? "mono" : ""}">${esc(String(v))}</td>`;

  // позиционное обозначение - ключ сверки; берём из любого ref'а, где оно есть
  const designator = (err.refs || []).map((r) => r.designator).find((d) => d) || null;

  return `
    <tr data-sev="${sev}">
      <td>${num}</td>
      <td><span class="kind-badge">${esc(err.kind || "")}</span></td>
      <td><span class="sev-badge sev-${sev}">${SEV_LABELS[sev] || sev}</span></td>
      ${cell(designator, true)}
      ${cell(sp ? sp.row : null)}
      ${cell(sp ? sp.article : null, true)}
      ${cell(sp && sp.quantity != null ? sp.quantity : null)}
      ${cell(asm && asm.sheet != null ? "лист " + asm.sheet : null)}
      ${cell(asm ? asm.article : null, true)}
      ${cell(sc && sc.sheet != null ? "лист " + sc.sheet : null)}
      ${cell(sc ? sc.article : null, true)}
      <td class="finding">${esc(err.finding || "")}${fragmentButton(err)}</td>
      <td class="action">${esc(err.action || "")}</td>
    </tr>`;
}

// находит ref по типу документа
function refOf(err, docType) {
  return (err.refs || []).find((r) => r.doc_type === docType) || null;
}

function renderRow(err, num) {
  const nl = refOf(err, "netlist");
  const sc = refOf(err, "scheme");
  const sev = err.severity || "info";

  const cell = (v, mono) => v === null || v === undefined || v === ""
    ? `<td class="empty-cell">—</td>`
    : `<td class="${mono ? "mono" : ""}">${esc(String(v))}</td>`;

  const termPin = (r) => r ? joinNonEmpty([r.terminal_block, r.pin], " / ") : null;
  const lineRow = (r) => {
    if (!r) return null;
    return joinNonEmpty([
      r.sheet != null ? "лист " + r.sheet : null,
      r.row != null ? "стр. " + r.row : null,
    ], ", ");
  };

  return `
    <tr data-sev="${sev}">
      <td>${num}</td>
      <td><span class="kind-badge">${esc(err.kind || "")}</span></td>
      <td><span class="sev-badge sev-${sev}">${SEV_LABELS[sev] || sev}</span></td>
      ${cell(lineRow(nl))}
      ${cell(nl ? nl.cabinet : null, true)}
      ${cell(termPin(nl), true)}
      ${cell(nl ? nl.marking : null, true)}
      ${cell(nl ? nl.kks : null, true)}
      ${cell(nl ? nl.conductor : null, true)}
      ${cell(sc ? (sc.sheet != null ? "лист " + sc.sheet : null) : null)}
      ${cell(termPin(sc), true)}
      ${cell(sc ? sc.marking : null, true)}
      ${cell(sc ? sc.kks : null, true)}
      ${cell(sc ? sc.conductor : null, true)}
      <td class="finding">${esc(err.finding || "")}${fragmentButton(err)}</td>
      <td class="action">${esc(err.action || "")}</td>
    </tr>`;
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
// Консоль / статус
// ------------------------------------------------------------
function logLine(text, cls) {
  const el = $("console");
  const span = document.createElement("div");
  if (cls) span.className = "l-" + cls;
  span.textContent = text;
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}
function classifyLog(line) {
  const l = line.toLowerCase();
  if (l.includes("[error]") || l.includes("ошибка") || l.includes("!!!")) return "err";
  if (l.includes("[warning]") || l.includes("пропущен")) return "warn";
  if (l.includes("готово") || l.includes("сохранён") || l.includes("===")) return "ok";
  return "";
}
function setStatus(text, busy, cls) {
  const bar = $("status-bar");
  bar.classList.remove("hidden");
  $("status-text").textContent = text;
  $("status-text").className = cls === "err" ? "dot-err" : cls === "ok" ? "dot-ok" : "";
  $("status-spinner").style.display = busy ? "block" : "none";
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

function toggleRunMenu() { $("run-menu").classList.toggle("open"); }
function closeRunMenu() { $("run-menu").classList.remove("open"); }

// ------------------------------------------------------------
// Утилиты
// ------------------------------------------------------------
async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function joinNonEmpty(arr, sep) {
  const v = arr.filter((x) => x !== null && x !== undefined && x !== "");
  return v.length ? v.join(sep) : null;
}
function fmtSize(b) {
  if (b < 1024) return b + " Б";
  if (b < 1024 * 1024) return (b / 1024).toFixed(0) + " КБ";
  return (b / 1024 / 1024).toFixed(1) + " МБ";
}
function fmtCtx(n) { return n >= 1000 ? Math.round(n / 1000) + "K" : String(n); }
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit",
    year: "2-digit", hour: "2-digit", minute: "2-digit" });
}

init();
