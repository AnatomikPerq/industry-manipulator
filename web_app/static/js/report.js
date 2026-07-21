/* ============================================================
   Отчёт: две таблицы находок и фрагменты чертежа.

   Таблиц именно ДВЕ, и это не украшение: у сверки «таблица подключений против
   схемы» место находки описывается клеммой, штифтом и маркировкой провода, а у
   находок по спецификации и сборочному чертежу - позиционным обозначением,
   артикулом и количеством. Одной таблицей их не показать: колонки не
   совмещаются.
   ============================================================ */

import { S } from "./state.js";
import { $, esc, fetchJSON, joinNonEmpty, logLine } from "./util.js";

// ------------------------------------------------------------
// Отчёт (таблица в стиле примеров)
// ------------------------------------------------------------
export async function showReport() {
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
export function downloadReportPdf() {
  if (!S.id) return;
  window.open(`/api/sessions/${encodeURIComponent(S.id)}/report.pdf`, "_blank");
}

export const SEV_LABELS = { critical: "критич.", high: "высокий", medium: "средний", low: "низкий", info: "инфо" };
export const SEV_ORDER = ["critical", "high", "medium", "low", "info"];

export function renderReport(data) {
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
export const FRAGMENT_DOC_TYPES = ["scheme", "assembly", "netlist"];

export function fragmentTargets(err) {
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

export function fragmentButton(err) {
  if (!fragmentTargets(err).length) return "";
  return `<button class="frag-btn" data-frag="${err.__i}"
            title="Показать это место на чертеже">фрагмент</button>`;
}

export async function openFragments(index) {
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

export function closeFragments() {
  $("frag-modal").classList.remove("show");
  $("frag-body").innerHTML = "";
}

export function renderWiringTable(errors) {
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

export function renderBundleTable(errors) {
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

export function sectionTitle(text, n) {
  return `<h3 class="report-group">${esc(text)} <span class="report-group-n">${n}</span></h3>`;
}

export function renderBundleRow(err, num) {
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
export function refOf(err, docType) {
  return (err.refs || []).find((r) => r.doc_type === docType) || null;
}

export function renderRow(err, num) {
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
