/* ============================================================
   Выбор моделей нейросетей для сессии.

   Зачем это в интерфейсе. Модели на сервере меняются чаще, чем проект: одну
   выгрузили, другую поставили, третья не влезла в память. До V2.0 выбор жил
   только в config.yaml, и человеку, у которого не загрузилась модель агента,
   оставалось править YAML на сервере и перезапускать - при том, что рядом на
   том же LM Studio стояла рабочая.

   Настройка ХРАНИТСЯ В СЕССИИ, а не в общем конфиге (см. SessionStore.set_llm):
   иначе один пользователь молча перенастраивал бы прогоны всех остальных, а
   разобраться потом, чем считался позавчерашний отчёт, было бы нечем. Перед
   запуском выбор уезжает в config.yaml сессии и остаётся в её папке навсегда.
   ============================================================ */

import { S, isBusy } from "./state.js";
import { $, esc, fetchJSON, logLine } from "./util.js";

// Список моделей сервера. Читается один раз на вкладку: он не меняется от
// сессии к сессии, а лезть на сервер ИИ при каждом открытии сессии - лишняя
// секунда ожидания на экране, который открывают часто.
let catalog = null;

const FIELDS = [
  ["agent_1", "llm-agent1"],
  ["agent_2", "llm-agent2"],
  ["vision", "llm-vision"],
];

async function loadCatalog() {
  if (catalog) return catalog;
  try {
    catalog = await fetchJSON("/api/models");
  } catch (e) {
    // Сервер ИИ недоступен - настройку всё равно показываем: уже выбранное
    // видеть надо, и снять свой выбор пользователь должен мочь всегда.
    catalog = { models: [], error: e.message, defaults: {} };
  }
  return catalog;
}

function optionLabel(m) {
  const bits = [];
  if (m.params) bits.push(m.params);
  if (m.vision) bits.push("зрение");
  if (!m.loaded) bits.push("не загружена");
  return m.display_name + (bits.length ? ` — ${bits.join(", ")}` : "");
}

function fillSelect(select, chosen, def, onlyVision) {
  const models = (catalog.models || []).filter((m) => !onlyVision || m.vision);
  const defLabel = def ? `из config.yaml — ${def}` : "из config.yaml";
  let html = `<option value="">${esc(defLabel)}</option>`;
  for (const m of models) {
    const sel = m.key === chosen ? " selected" : "";
    html += `<option value="${esc(m.key)}"${sel}>${esc(optionLabel(m))}</option>`;
  }
  // Модель, выбранную раньше, но исчезнувшую с сервера, обязаны показать:
  // молча подставить вместо неё «как в конфиге» значило бы соврать о том,
  // чем пойдёт прогон.
  if (chosen && !models.some((m) => m.key === chosen)) {
    html += `<option value="${esc(chosen)}" selected>${esc(chosen)} — нет на сервере</option>`;
  }
  select.innerHTML = html;
}

/** Перерисовать панель по S.meta.llm. Зовётся при каждой загрузке сессии. */
export async function renderLLM() {
  const box = $("llm-box");
  if (!box) return;
  await loadCatalog();

  const llm = (S.meta && S.meta.llm) || {};
  const def = catalog.defaults || {};
  const busy = isBusy();

  for (const [key, id] of FIELDS) {
    fillSelect($(id), llm[key] || "", def[key], key === "vision");
    $(id).disabled = busy;
  }

  const count = String(llm.agents_count || def.agents_count || 1);
  $("llm-count").value = count;
  $("llm-count").disabled = busy;
  $("llm-single").value = llm.single_agent || def.single_agent || "agent_1";
  $("llm-single").disabled = busy;

  // При одном агенте второй не участвует - гасим его строку, чтобы выбор в
  // ней не читался как «эта модель тоже будет работать».
  const single = count === "1";
  $("llm-single-wrap").style.display = single ? "" : "none";
  $("llm-agent2").closest("label").classList.toggle("llm-off", single);
  $("llm-agent2").disabled = busy || single;

  const notes = [];
  if (catalog.error) notes.push("Список моделей не получен: " + catalog.error);
  const chosen = FIELDS.map(([k]) => llm[k] || def[k]).filter(Boolean);
  const missing = (catalog.models || []).length
    ? chosen.filter((k) => {
        const m = catalog.models.find((x) => x.key === k);
        return m && !m.loaded;
      })
    : [];
  if (missing.length) {
    notes.push("Не загружены на сервере: " + [...new Set(missing)].join(", ")
      + ". LM Studio попробует загрузить их при первом обращении; если памяти "
      + "не хватит, прогон упадёт на стадии ИИ.");
  }
  $("llm-note").textContent = notes.join(" ");

  $("llm-summary").textContent = summaryText(llm, def);
}

function summaryText(llm, def) {
  const count = llm.agents_count || def.agents_count || 1;
  const single = llm.single_agent || def.single_agent || "agent_1";
  const shorten = (s) => (s || "").split("/").pop() || "по умолчанию";
  const agent = count === 1
    ? shorten(llm[single] || def[single])
    : `${shorten(llm.agent_1 || def.agent_1)} + ${shorten(llm.agent_2 || def.agent_2)}`;
  return `агентов ${count}: ${agent}; зрение: ${shorten(llm.vision || def.vision)}`;
}

/** Сохранить один изменённый пункт. */
export async function saveLLM(patch) {
  try {
    const res = await fetchJSON(`/api/sessions/${encodeURIComponent(S.id)}/set-llm`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (S.meta) S.meta.llm = res.llm || {};
    await renderLLM();
  } catch (e) {
    logLine("Не удалось сохранить выбор моделей: " + e.message, "err");
  }
}

/** Навесить обработчики. Зовётся один раз при инициализации приложения. */
export function bindLLM() {
  if (!$("llm-box")) return;
  for (const [key, id] of FIELDS) {
    $(id).addEventListener("change", (e) => saveLLM({ [key]: e.target.value || null }));
  }
  $("llm-count").addEventListener("change", (e) =>
    saveLLM({ agents_count: Number(e.target.value) }));
  $("llm-single").addEventListener("change", (e) =>
    saveLLM({ single_agent: e.target.value }));
}
