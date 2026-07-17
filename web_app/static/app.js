/* ============================================================
   Анализатор EPLAN-схем - фронтенд
   ============================================================ */

const $ = (id) => document.getElementById(id);
let DOC_TYPES = [];          // список принимаемых типов (с бэкенда)
let FILES = [];              // [{name, size, type}] - выбранные пользователем
let statusTimer = null;

// ------------------------------------------------------------
// Инициализация
// ------------------------------------------------------------
async function init() {
  try {
    const cfg = await fetchJSON("/api/config");
    DOC_TYPES = cfg.doc_types;
    $("version-badge").textContent = cfg.version;
    $("meta-version").textContent = cfg.version;
    $("footer-version").textContent = cfg.version;
    renderDocTypes(cfg);
  } catch (e) {
    logLine("Не удалось загрузить конфигурацию: " + e.message, "err");
  }
  await refreshFiles();
  bindEvents();
  await restoreState();   // восстановить консоль/статус/отчёт после перезагрузки
}

function renderDocTypes(cfg) {
  $("doc-types").innerHTML = DOC_TYPES.map((t) => `
    <li>
      <span class="dot"></span>
      <div>
        <div class="t-title">${esc(t.title)}</div>
        <div class="t-hint">${esc(t.hint)}</div>
      </div>
    </li>`).join("");
}

// После перезагрузки страницы состояние анализа хранится на сервере (в памяти
// процесса): восстанавливаем консоль, статус и, если анализ уже завершён, отчёт.
// Если анализ ещё идёт - подхватываем опрос статуса, не теряя лог.
async function restoreState() {
  let s;
  try { s = await fetchJSON("/api/status"); } catch { return; }
  if (s.stage === "idle") return;

  $("console").innerHTML = "";
  for (const line of s.log) logLine(line, classifyLog(line));

  if (s.running) {
    setStatus(`Идёт анализ… (режим: ${s.mode === "scripts" ? "без ИИ" : "полный"})`, true);
    resumePolling(s.log.length);
  } else if (s.stage === "cancelled") {
    setStatus("Прошлый анализ был отменён", false, "err");
  } else if (s.stage === "error") {
    setStatus("Прошлый анализ завершился с ошибкой", false, "err");
  } else if (s.stage === "done") {
    setStatus(`Анализ завершён. Замечаний: ${s.n_findings ?? "?"}`, false, "ok");
    await showReport();
  }
}

// ------------------------------------------------------------
// Файлы: загрузка, отображение, выбор типа
// ------------------------------------------------------------
async function refreshFiles() {
  try {
    const data = await fetchJSON("/api/files");
    FILES = data.files.map((f) => ({
      name: f.name, size: f.size, type: f.detected_type || "",
      bundle: f.bundle || "",
    }));
  } catch (e) {
    FILES = [];
  }
  renderFiles();
}

function renderFiles() {
  const list = $("file-list");
  const empty = $("file-empty");
  const note = $("file-note");
  if (FILES.length === 0) {
    list.innerHTML = "";
    empty.style.display = "block";
    note.classList.remove("show");
    updateRunEnabled();
    return;
  }
  empty.style.display = "none";
  note.classList.add("show");
  list.innerHTML = FILES.map((f, i) => {
    const opts = ['<option value="">— укажите тип —</option>']
      .concat(DOC_TYPES.map((t) =>
        `<option value="${t.key}" ${f.type === t.key ? "selected" : ""}>${esc(t.title)}</option>`))
      .join("");
    // Связку показываем, ТОЛЬКО если файл лежит в подпапке: в обычном случае
    // все загруженные документы - один проект, и подпись "проект" на каждой
    // строке ничего не сообщает (об этом сказано один раз под списком).
    const bundle = f.bundle
      ? `<span class="fi-bundle" title="Отдельная связка (подпапка): ${esc(f.bundle)}">${esc(f.bundle)}</span>`
      : "";
    return `
      <li class="file-item">
        <span class="fi-icon">▤</span>
        <span class="fi-name" title="${esc(f.name)}">${esc(f.name)}</span>
        ${bundle}
        <span class="fi-size">${fmtSize(f.size)}</span>
        <select data-idx="${i}" class="${f.type ? "" : "unset"}">${opts}</select>
      </li>`;
  }).join("");

  list.querySelectorAll("select").forEach((sel) => {
    sel.addEventListener("change", (e) => {
      const idx = +e.target.dataset.idx;
      FILES[idx].type = e.target.value;
      e.target.classList.toggle("unset", !e.target.value);
      updateRunEnabled();
      saveType(FILES[idx].name, e.target.value);
    });
  });
  updateRunEnabled();
}

// Сохраняет выбор типа на сервере сразу при изменении, чтобы пометка пережила
// перезагрузку страницы (раньше тип жил только в памяти вкладки браузера).
async function saveType(name, type) {
  try {
    await fetchJSON("/api/set-type", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, type }),
    });
  } catch (e) {
    logLine(`Не удалось сохранить тип для ${name}: ${e.message}`, "warn");
  }
}

// Принимаемые расширения. Должны совпадать с ALLOWED_SUFFIXES в server.py:
// .xlsx нужен спецификации (СО) - единственному документу связки не в PDF.
const ACCEPTED_EXT = [".pdf", ".xlsx", ".xlsm"];

async function uploadFiles(fileList) {
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
    const res = await fetch("/api/upload", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    logLine(`Загружено файлов: ${data.saved.length}` +
      (data.skipped.length ? `, пропущено: ${data.skipped.length}` : ""), "ok");
    await refreshFiles();
    setStatus("Файлы загружены. Укажите тип каждого и запустите анализ.", false);
  } catch (e) {
    logLine("Ошибка загрузки: " + e.message, "err");
    setStatus("Ошибка загрузки", false);
  }
}

// Кнопка запуска активна, только когда все файлы имеют указанный тип
function updateRunEnabled() {
  const ready = FILES.length > 0 && FILES.every((f) => f.type);
  const busy = statusTimer !== null;
  $("run-btn").disabled = !ready || busy;
  $("run-toggle").disabled = !ready || busy;
}

// ------------------------------------------------------------
// Запуск анализа
// ------------------------------------------------------------
async function startAnalysis(mode) {
  closeRunMenu();
  const types = {};
  FILES.forEach((f) => { types[f.name] = f.type; });

  $("report-section").classList.remove("show");
  logLine(`--- Старт анализа (режим: ${mode === "scripts" ? "без ИИ" : "полный"}) ---`, "ok");
  setStatus("Анализ запущен…", true);

  try {
    const res = await fetch("/api/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, types }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    pollStatus();
  } catch (e) {
    logLine("Не удалось запустить анализ: " + e.message, "err");
    setStatus("Ошибка запуска", false);
  }
}

function pollStatus() { resumePolling(0); }

function resumePolling(startLen) {
  if (statusTimer) clearInterval(statusTimer);
  updateRunEnabled();
  showCancel(true);
  let lastLen = startLen || 0;
  statusTimer = setInterval(async () => {
    let s;
    try { s = await fetchJSON("/api/status"); }
    catch { return; }

    // дописываем только новые строки лога
    if (s.log.length > lastLen) {
      for (let i = lastLen; i < s.log.length; i++) logLine(s.log[i], classifyLog(s.log[i]));
      lastLen = s.log.length;
    }

    if (s.running) {
      const note = s.cancel_requested ? " — останавливаем…" : "";
      setStatus(`Идёт анализ… (режим: ${s.mode === "scripts" ? "без ИИ" : "полный"})${note}`, true);
    } else {
      clearInterval(statusTimer);
      statusTimer = null;
      showCancel(false);
      updateRunEnabled();
      if (s.stage === "cancelled") {
        setStatus("Анализ отменён", false, "err");
      } else if (s.stage === "error") {
        setStatus("Анализ завершился с ошибкой", false, "err");
      } else {
        setStatus(`Анализ завершён. Замечаний: ${s.n_findings ?? "?"}`, false, "ok");
        await showReport();
      }
    }
  }, 1000);
}

// Отмена анализа. Сервер убивает процесс пайплайна мгновенно (весь процесс
// целиком, со всеми потомками) - кнопку блокируем лишь на время самого запроса.
async function cancelAnalysis() {
  const btn = $("cancel-btn");
  btn.disabled = true;
  btn.textContent = "Останавливаем…";
  logLine("--- Запрошена отмена анализа ---", "warn");
  try {
    await fetchJSON("/api/cancel", { method: "POST" });
  } catch (e) {
    logLine("Не удалось отменить: " + e.message, "err");
    btn.disabled = false;
    btn.textContent = "Отменить анализ";
  }
}

function showCancel(on) {
  const btn = $("cancel-btn");
  if (!btn) return;
  btn.style.display = on ? "" : "none";
  if (on) { btn.disabled = false; btn.textContent = "Отменить анализ"; }
}

// ------------------------------------------------------------
// Отчёт (таблица в стиле примеров)
// ------------------------------------------------------------
async function showReport() {
  let data;
  try { data = await fetchJSON("/api/report"); }
  catch (e) { logLine("Отчёт недоступен: " + e.message, "warn"); return; }
  renderReport(data);
  $("report-section").classList.add("show");
  $("report-section").scrollIntoView({ behavior: "smooth", block: "start" });
}

const SEV_LABELS = { critical: "критич.", high: "высокий", medium: "средний", low: "низкий", info: "инфо" };
const SEV_ORDER = ["critical", "high", "medium", "low", "info"];

function renderReport(data) {
  const errors = data.errors || [];
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
      <td class="finding">${esc(err.finding || "")}</td>
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
      <td class="finding">${esc(err.finding || "")}</td>
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
  const dz = $("dropzone");
  const input = $("file-input");
  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", (e) => { uploadFiles(e.target.files); input.value = ""; });
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });

  $("run-btn").addEventListener("click", () => startAnalysis("full"));
  $("run-toggle").addEventListener("click", (e) => { e.stopPropagation(); toggleRunMenu(); });
  $("run-menu").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => startAnalysis(b.dataset.mode)));
  document.addEventListener("click", closeRunMenu);

  $("cancel-btn").addEventListener("click", cancelAnalysis);
  $("btn-check-llm").addEventListener("click", checkLLM);
  $("btn-show-report").addEventListener("click", showReport);
  $("btn-clear-console").addEventListener("click", () => { $("console").innerHTML = ""; });
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

init();
