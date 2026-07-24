/* ============================================================
   Настройки ИИ — модалка администратора (раздел сессий).

   Две вкладки:
     • «Модели LM Studio» — список моделей сервера со статусом; загрузка (с
       выбором параметров: контекст, TTL, flash attention и т.д.) и выгрузка.
     • «Конфигурация нейросетей» — глобальный конфиг: какие модели использует
       программа, адреса и лимиты. Правки уходят в config.local.yaml (см.
       web_app/config_admin.py) и действуют со следующего прогона.

   Всё под правами администратора: эндпоинты /api/lmstudio/* и /api/admin/config
   отвечают 403 обычному пользователю, а сама кнопка ему не показывается.
   ============================================================ */

import { $, esc, fetchJSON, fmtCtx, fmtSize } from "./util.js";

let models = [];
let view = null;          // ответ /api/admin/config: {effective, local}
let bound = false;
let working = false;      // идёт длинная операция (load/unload) — не даём кликать

// ------------------------------------------------------------
// Открытие/закрытие
// ------------------------------------------------------------
export function openLlmAdmin() {
  $("llm-admin-modal").classList.add("show");
  switchTab("models");
  loadModels();
  loadConfig();
}
function closeLlmAdmin() { $("llm-admin-modal").classList.remove("show"); }

export function initLlmAdmin() {
  if (bound) return;
  bound = true;
  $("llm-admin-close").addEventListener("click", closeLlmAdmin);
  $("llm-admin-modal").addEventListener("click", (e) => {
    if (e.target === $("llm-admin-modal")) closeLlmAdmin();   // клик по фону
  });
  $("llm-admin-modal").querySelectorAll(".llm-tab").forEach((b) =>
    b.addEventListener("click", () => switchTab(b.dataset.tab)));
  $("llm-models-refresh").addEventListener("click", loadModels);
  // делегирование: кнопки «Загрузить»/«Выгрузить» рисуются динамически
  $("llm-admin-models").addEventListener("click", onModelAction);
  $("llm-config-save").addEventListener("click", saveConfig);
}

function switchTab(tab) {
  $("llm-admin-modal").querySelectorAll(".llm-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  $("llm-tab-models").classList.toggle("hidden", tab !== "models");
  $("llm-tab-config").classList.toggle("hidden", tab !== "config");
}

// ------------------------------------------------------------
// Вкладка «Модели LM Studio»
// ------------------------------------------------------------
async function loadModels() {
  const box = $("llm-admin-models");
  box.innerHTML = `<div class="hint-text">Загрузка списка…</div>`;
  $("llm-models-msg").textContent = "";
  try {
    const data = await fetchJSON("/api/lmstudio/models");
    models = data.models || [];
    if (data.error) $("llm-models-msg").textContent = "Сервер ИИ: " + data.error;
    renderModels();
  } catch (e) {
    box.innerHTML = `<div class="l-err">${esc(e.message)}</div>`;
  }
}

function renderModels() {
  const box = $("llm-admin-models");
  if (!models.length) {
    box.innerHTML = `<div class="hint-text">На сервере нет моделей.</div>`;
    return;
  }
  box.innerHTML = models.map(modelRow).join("");
}

function modelRow(m) {
  const tags = [];
  if (m.type && m.type !== "llm") tags.push(tag(m.type, "type"));
  if (m.vision) tags.push(tag("зрение", "vis"));
  if (m.reasoning) tags.push(tag("раздумья", "rsn"));
  const meta = [
    m.params,
    m.max_context ? "ctx " + fmtCtx(m.max_context) : null,
    m.size_bytes ? fmtSize(m.size_bytes) : null,
  ].filter(Boolean).join(" · ");

  let action;
  if (m.loaded) {
    // instance_id для выгрузки — из loaded_instances (обычно один)
    const inst = (m.loaded_instances && m.loaded_instances[0]) || { id: m.key };
    const ctx = inst.context_length ? ` (ctx ${fmtCtx(inst.context_length)})` : "";
    action = `<span class="tag tag-loaded">загружена${esc(ctx)}</span>
      <button class="btn btn-cancel llm-unload" data-unload="${esc(inst.id)}">Выгрузить</button>`;
  } else {
    action = `<button class="btn btn-primary llm-load" data-load="${esc(m.key)}">Загрузить</button>`;
  }

  return `
    <div class="llm-model-row${m.loaded ? " is-loaded" : ""}">
      <div class="llm-model-main">
        <span class="llm-model-name">${esc(m.display_name)}</span>
        ${tags.join("")}
        ${meta ? `<span class="hint-text">${esc(meta)}</span>` : ""}
      </div>
      <div class="llm-model-actions">${action}</div>
    </div>`;
}

function tag(text, kind) {
  return `<span class="tag tag-${kind}">${esc(text)}</span>`;
}

function loadParams() {
  const num = (id) => {
    const v = $(id).value.trim();
    return v === "" ? undefined : Number(v);
  };
  const p = {};
  const ctx = num("lp-context"); if (ctx !== undefined) p.context_length = ctx;
  const ttl = num("lp-ttl"); if (ttl !== undefined) p.ttl_seconds = ttl;
  const batch = num("lp-batch"); if (batch !== undefined) p.eval_batch_size = batch;
  const exp = num("lp-experts"); if (exp !== undefined) p.num_experts = exp;
  if ($("lp-flash").checked) p.flash_attention = true;
  if ($("lp-kv").checked) p.offload_kv_cache_to_gpu = true;
  return p;
}

async function onModelAction(e) {
  const loadBtn = e.target.closest("[data-load]");
  const unloadBtn = e.target.closest("[data-unload]");
  if (working || (!loadBtn && !unloadBtn)) return;

  working = true;
  const msg = $("llm-models-msg");
  try {
    if (loadBtn) {
      // Загрузка большой модели идёт долго — сервер держит запрос открытым.
      msg.textContent = `Загрузка «${loadBtn.dataset.load}»… это может занять минуту-другую.`;
      setModelsDisabled(true);
      await fetchJSON("/api/lmstudio/load", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: loadBtn.dataset.load, params: loadParams() }),
      });
      msg.textContent = "Модель загружена.";
    } else {
      msg.textContent = "Выгрузка…";
      setModelsDisabled(true);
      await fetchJSON("/api/lmstudio/unload", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance_id: unloadBtn.dataset.unload }),
      });
      msg.textContent = "Модель выгружена.";
    }
    await loadModels();
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
    setModelsDisabled(false);
  } finally {
    working = false;
  }
}

function setModelsDisabled(on) {
  $("llm-admin-models").querySelectorAll("button").forEach((b) => { b.disabled = on; });
}

// ------------------------------------------------------------
// Вкладка «Конфигурация нейросетей»
// ------------------------------------------------------------
async function loadConfig() {
  $("llm-config-msg").textContent = "";
  try {
    view = await fetchJSON("/api/admin/config");
    renderConfigForm();
  } catch (e) {
    $("llm-config-form").innerHTML = `<div class="l-err">${esc(e.message)}</div>`;
  }
}

// Есть ли поле среди ЛОКАЛЬНЫХ переопределений (для пометки «· локально»).
function inLocal(path) {
  let node = view && view.local;
  for (const key of path.split(".")) {
    if (!node || typeof node !== "object" || !(key in node)) return false;
    node = node[key];
  }
  return node !== undefined && node !== null;
}

function field(label, path, value, type = "text") {
  const v = value === null || value === undefined ? "" : value;
  const local = inLocal(path) ? `<span class="cfg-local" title="Переопределено локально">· локально</span>` : "";
  const attrs = `data-path="${path}" data-type="${type}" data-initial="${esc(String(v))}"`;
  const step = type === "temp" ? ` step="0.05" min="0" max="2"` : (type === "number" ? ` min="1"` : "");
  const kind = (type === "number" || type === "temp") ? "number" : "text";
  return `
    <label class="cfg-field">
      <span class="cfg-label">${esc(label)} ${local}</span>
      <input type="${kind}"${step} value="${esc(String(v))}" ${attrs}>
    </label>`;
}

function selectField(label, path, value, options) {
  const local = inLocal(path) ? `<span class="cfg-local" title="Переопределено локально">· локально</span>` : "";
  const opts = options.map((o) =>
    `<option value="${esc(String(o.v))}"${String(o.v) === String(value) ? " selected" : ""}>${esc(o.t)}</option>`).join("");
  return `
    <label class="cfg-field">
      <span class="cfg-label">${esc(label)} ${local}</span>
      <select data-path="${path}" data-type="select" data-initial="${esc(String(value ?? ""))}">${opts}</select>
    </label>`;
}

function serverFields(prefix, s) {
  return [
    field("Модель", `${prefix}.model`, s.model),
    field("Адрес сервера (base_url)", `${prefix}.base_url`, s.base_url),
    field("API-ключ", `${prefix}.api_key`, s.api_key),
    field("Температура", `${prefix}.temperature`, s.temperature, "temp"),
    field("Лимит ответа (max_tokens)", `${prefix}.max_tokens`, s.max_tokens, "number"),
    field("Контекст (context_window)", `${prefix}.context_window`, s.context_window, "number"),
  ].join("");
}

function renderConfigForm() {
  const e = view.effective;
  const s = e.llm_servers;
  const agentOpts = [{ v: "agent_1", t: "Агент 1" }, { v: "agent_2", t: "Агент 2" }];

  $("llm-config-form").innerHTML = `
    <fieldset class="cfg-group">
      <legend>Агент 1</legend>
      <div class="cfg-grid">${serverFields("llm_servers.agent_1", s.agent_1)}</div>
    </fieldset>
    <fieldset class="cfg-group">
      <legend>Агент 2</legend>
      <div class="cfg-grid">${serverFields("llm_servers.agent_2", s.agent_2)}</div>
    </fieldset>
    <fieldset class="cfg-group">
      <legend>Модель зрения</legend>
      <div class="cfg-grid">
        ${selectField("Сервер (use_agent)", "llm_servers.vision.use_agent", s.vision.use_agent, agentOpts)}
        ${field("Модель зрения (пусто — как у агента)", "llm_servers.vision.model", s.vision.model)}
        ${field("Температура", "llm_servers.vision.temperature", s.vision.temperature, "temp")}
        ${field("Лимит ответа (max_tokens)", "llm_servers.vision.max_tokens", s.vision.max_tokens, "number")}
      </div>
    </fieldset>
    <fieldset class="cfg-group">
      <legend>Число агентов и мерджер</legend>
      <div class="cfg-grid">
        ${selectField("Агентов", "agents.count", e.agents.count,
          [{ v: 1, t: "1 — одна модель" }, { v: 2, t: "2 — две модели, отчёты сшивает третья" }])}
        ${selectField("Единственный агент (при 1)", "agents.single_agent", e.agents.single_agent, agentOpts)}
        ${selectField("Сшиватель (merger)", "llm_servers.merger.use_agent", s.merger.use_agent, agentOpts)}
      </div>
    </fieldset>
    <fieldset class="cfg-group">
      <legend>Стадия зрения (рендер)</legend>
      <div class="cfg-grid">
        ${field("Высота прописной, px (cap_px)", "vision.cap_px", e.vision.cap_px, "number")}
        ${field("Бюджет пикселей на тайл (max_tile_pixels)", "vision.max_tile_pixels", e.vision.max_tile_pixels, "number")}
      </div>
    </fieldset>`;
}

function setPath(obj, path, value) {
  const parts = path.split(".");
  let node = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    node[parts[i]] = node[parts[i]] || {};
    node = node[parts[i]];
  }
  node[parts[parts.length - 1]] = value;
}

// Собираем ТОЛЬКО изменённые поля: так config.local.yaml не распухает копией
// всего конфига, а config.yaml продолжает давать значения по умолчанию для
// нетронутого. Сравниваем со значением, показанным при открытии (data-initial).
function collectChanges() {
  const changes = {};
  $("llm-config-form").querySelectorAll("[data-path]").forEach((el) => {
    const path = el.dataset.path;
    const type = el.dataset.type;
    const initial = el.dataset.initial ?? "";
    const raw = el.value;
    if (String(raw) === String(initial)) return;      // не менялось

    if (type === "number" || type === "temp") {
      if (String(raw).trim() === "") return;          // числовое пустым не шлём
      setPath(changes, path, Number(raw));
    } else if (type === "select") {
      // count — число, остальные селекты — строки
      setPath(changes, path, path.endsWith("count") ? Number(raw) : raw);
    } else {
      setPath(changes, path, raw);                    // строка (пустая = сброс)
    }
  });
  return changes;
}

async function saveConfig() {
  const msg = $("llm-config-msg");
  const changes = collectChanges();
  if (!Object.keys(changes).length) {
    msg.textContent = "Изменений нет.";
    msg.className = "llm-admin-msg";
    return;
  }
  try {
    view = await fetchJSON("/api/admin/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
    renderConfigForm();
    msg.textContent = "Сохранено. Действует со следующего запуска анализа.";
    msg.className = "llm-admin-msg ok";
  } catch (e) {
    msg.textContent = "Не удалось сохранить: " + e.message;
    msg.className = "llm-admin-msg err";
  }
}
