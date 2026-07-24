/* ============================================================
   Лёгкий рендерер Markdown + базового LaTeX для обычного чата.

   ЗАЧЕМ СВОЙ, А НЕ БИБЛИОТЕКА. Проект офлайновый и без сборщика: подключить
   marked/KaTeX с CDN нельзя, а вендорить KaTeX со шрифтами (мегабайты) - тяжело
   и противоречит духу проекта («только стандартная библиотека»). Поэтому здесь
   компактный рендерер: полный ходовой Markdown (заголовки, списки, таблицы,
   код, жирный/курсив, ссылки, цитаты) и БАЗОВЫЙ LaTeX ($...$, $$...$$, \(...\),
   \[...\]) через HTML/Unicode - дроби, степени/индексы, корни, греческие буквы,
   ходовые символы. Настоящего математического движка здесь нет и не заявлено.

   БЕЗОПАСНОСТЬ. Вход - ответ модели, ему доверять нельзя. Поэтому ПЕРВЫМ делом
   весь текст экранируется (esc), и только потом по нему проходят преобразования,
   выдающие заведомо безопасные теги. Код, инлайн-код и математика вынимаются в
   «заглушки» ДО разметки, чтобы * _ ` внутри них не толковались как Markdown, и
   возвращаются в самом конце. У ссылок пропускаем только безопасные схемы.
   ============================================================ */

import { esc } from "./util.js";

// ------------------------------------------------------------
// LaTeX -> HTML/Unicode (базовый)
// ------------------------------------------------------------

// Команды-символы: \alpha -> α и т.п. Заведомо неполно - только ходовое.
const TEX_SYMBOLS = {
  // строчные греческие
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε",
  zeta: "ζ", eta: "η", theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ",
  lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", pi: "π", varpi: "ϖ", rho: "ρ",
  varrho: "ϱ", sigma: "σ", varsigma: "ς", tau: "τ", upsilon: "υ", phi: "φ",
  varphi: "ϕ", chi: "χ", psi: "ψ", omega: "ω",
  // прописные греческие
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π",
  Sigma: "Σ", Upsilon: "Υ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  // операторы и отношения
  times: "×", cdot: "·", div: "÷", pm: "±", mp: "∓", ast: "∗", star: "⋆",
  leq: "≤", le: "≤", geq: "≥", ge: "≥", neq: "≠", ne: "≠", approx: "≈",
  equiv: "≡", cong: "≅", sim: "∼", propto: "∝", ll: "≪", gg: "≫",
  // множества и логика
  in: "∈", notin: "∉", ni: "∋", subset: "⊂", supset: "⊃", subseteq: "⊆",
  supseteq: "⊇", cup: "∪", cap: "∩", emptyset: "∅", varnothing: "∅",
  forall: "∀", exists: "∃", nexists: "∄", neg: "¬", land: "∧", lor: "∨",
  // стрелки
  rightarrow: "→", to: "→", leftarrow: "←", gets: "←", leftrightarrow: "↔",
  Rightarrow: "⇒", implies: "⇒", Leftarrow: "⇐", Leftrightarrow: "⇔",
  iff: "⇔", mapsto: "↦", uparrow: "↑", downarrow: "↓",
  // разное
  infty: "∞", partial: "∂", nabla: "∇", sum: "∑", prod: "∏", int: "∫",
  oint: "∮", angle: "∠", perp: "⊥", parallel: "∥", degree: "°",
  deg: "°", prime: "′", cdots: "⋯", ldots: "…", dots: "…", vdots: "⋮",
  ddots: "⋱", hbar: "ℏ", ell: "ℓ", Re: "ℜ", Im: "ℑ", aleph: "ℵ",
  circ: "∘", bullet: "•", oplus: "⊕", otimes: "⊗", wedge: "∧", vee: "∨",
  langle: "⟨", rangle: "⟩", lceil: "⌈", rceil: "⌉", lfloor: "⌊", rfloor: "⌋",
};

// Функции-имена, которые в математике набираются прямым шрифтом (\sin, \log ...).
const TEX_FUNCS = new Set([
  "sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh",
  "log", "ln", "lg", "exp", "lim", "max", "min", "sup", "inf", "arg",
  "det", "dim", "ker", "gcd", "arcsin", "arccos", "arctan", "mod",
]);

// Микропробелы и служебные команды, которые просто выкидываем.
const TEX_DROP = /\\(?:left|right|,|;|:|!|quad|qquad|displaystyle|textstyle|limits|nolimits)\b|\\(?=[\s])/g;

// Читает аргумент в фигурных скобках, начиная с позиции открывающей «{».
// Возвращает [содержимое, индекс_после_закрывающей]. Учитывает вложенность.
function readGroup(s, open) {
  let depth = 0;
  for (let i = open; i < s.length; i++) {
    if (s[i] === "{") depth++;
    else if (s[i] === "}") { depth--; if (depth === 0) return [s.slice(open + 1, i), i + 1]; }
  }
  return [s.slice(open + 1), s.length];      // незакрытая скобка - берём остаток
}

// Один аргумент команды: либо {группа}, либо один следующий символ (\sqrt2).
function readArg(s, i) {
  while (i < s.length && s[i] === " ") i++;
  if (s[i] === "{") return readGroup(s, i);
  if (s[i] === "\\") {                        // \sqrt\alpha - команда как аргумент
    let j = i + 1;
    while (j < s.length && /[a-zA-Z]/.test(s[j])) j++;
    return [s.slice(i, j), j];
  }
  if (i < s.length) return [s[i], i + 1];
  return ["", i];
}

// Рекурсивно превращает кусок TeX в HTML. Вход УЖЕ экранирован (esc), поэтому
// генерируем только теги, безопасные по построению (sup/sub/span с фикс-классами).
function texToHtml(src) {
  let out = "";
  let i = 0;
  while (i < src.length) {
    const ch = src[i];

    if (ch === "\\") {
      // имя команды
      let j = i + 1;
      while (j < src.length && /[a-zA-Z]/.test(src[j])) j++;
      const name = src.slice(i + 1, j);

      if (name === "frac" || name === "dfrac" || name === "tfrac") {
        const [num, a] = readArg(src, j);
        const [den, b] = readArg(src, a);
        out += `<span class="tex-frac"><span class="tex-num">${texToHtml(num)}</span>`
             + `<span class="tex-den">${texToHtml(den)}</span></span>`;
        i = b; continue;
      }
      if (name === "sqrt") {
        let k = j, index = "";
        if (src[k] === "[") {                 // \sqrt[3]{x} - степень корня
          const close = src.indexOf("]", k);
          if (close !== -1) { index = src.slice(k + 1, close); k = close + 1; }
        }
        const [rad, a] = readArg(src, k);
        const idx = index ? `<span class="tex-root-idx">${texToHtml(index)}</span>` : "";
        out += `${idx}<span class="tex-sqrt">√<span class="tex-rad">${texToHtml(rad)}</span></span>`;
        i = a; continue;
      }
      if (name === "text" || name === "mathrm" || name === "mathbf"
          || name === "mathit" || name === "mathsf" || name === "operatorname"
          || name === "boldsymbol" || name === "bm") {
        const [arg, a] = readArg(src, j);
        const cls = (name === "mathbf" || name === "boldsymbol" || name === "bm")
          ? "tex-bf" : (name === "mathit" ? "tex-it" : "tex-rm");
        out += `<span class="${cls}">${texToHtml(arg)}</span>`;
        i = a; continue;
      }
      if (name === "hat" || name === "bar" || name === "vec"
          || name === "tilde" || name === "dot" || name === "overline") {
        const [arg, a] = readArg(src, j);
        const acc = { hat: "̂", tilde: "̃", bar: "̄",
                      overline: "̄", vec: "⃗", dot: "̇" }[name];
        out += `<span class="tex-accent">${texToHtml(arg)}${acc}</span>`;
        i = a; continue;
      }
      if (TEX_FUNCS.has(name)) { out += `<span class="tex-rm">${name}</span>`; i = j; continue; }
      if (Object.prototype.hasOwnProperty.call(TEX_SYMBOLS, name)) {
        out += TEX_SYMBOLS[name]; i = j; continue;
      }
      if (name.length) { out += name; i = j; continue; }   // неизвестная команда - как текст
      out += "\\"; i++; continue;                          // одинокий обратный слэш
    }

    if (ch === "^" || ch === "_") {
      const [arg, a] = readArg(src, i + 1);
      const tag = ch === "^" ? "sup" : "sub";
      out += `<${tag}>${texToHtml(arg)}</${tag}>`;
      i = a; continue;
    }

    if (ch === "{") { const [g, a] = readGroup(src, i); out += texToHtml(g); i = a; continue; }
    if (ch === "}") { i++; continue; }

    out += ch; i++;
  }
  return out;
}

// Публично: один математический фрагмент (без делимитеров) -> HTML.
export function renderMath(tex, display) {
  const cleaned = tex.replace(TEX_DROP, "");
  const inner = texToHtml(cleaned);
  const cls = display ? "tex tex-block" : "tex tex-inline";
  return `<span class="${cls}">${inner}</span>`;
}

// ------------------------------------------------------------
// Markdown
// ------------------------------------------------------------

// Разрешённые схемы у ссылок: чужой javascript:/data: в ответе модели пускать
// в href нельзя. Неизвестная схема глушится, относительные/якоря - разрешены.
function safeHref(url) {
  const u = url.trim();
  if (/^(https?:|mailto:|tel:|#|\/)/i.test(u)) return u;
  if (/^[a-z][a-z0-9+.-]*:/i.test(u)) return "#";   // неизвестная схема - глушим
  return u;
}

// Инлайн-разметка внутри строки: жирный, курсив, зачёркнутый, ссылки. Вход уже
// экранирован и с вынутыми код/математикой, так что спецсимволов тут нет.
function inlineMd(s) {
  // ссылки [текст](url)
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g,
    (_, text, url) => `<a href="${esc(safeHref(url))}" target="_blank" rel="noopener">${text}</a>`);
  // автоссылки - голый http(s)
  s = s.replace(/(^|[\s(])((?:https?:\/\/)[^\s<)]+)/g,
    (_, pre, url) => `${pre}<a href="${esc(safeHref(url))}" target="_blank" rel="noopener">${url}</a>`);
  // жирный + курсив
  s = s.replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>");
  s = s.replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_]+?)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_\w])_([^_\s][^_]*?)_(?![_\w])/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+?)~~/g, "<del>$1</del>");
  return s;
}

// Ячейки строки таблицы Markdown (| a | b |).
function tableCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}
function isTableSep(line) {
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
}

// Заглушки для вынутых код/математики: символы приватной зоны Unicode, которых
// в тексте чата не бывает, поэтому спутать их с настоящим содержимым (или с
// числом в тексте) нельзя.
const TOK_OPEN = String.fromCharCode(0xE000);
const TOK_CLOSE = String.fromCharCode(0xE001);
const BLOCK_TOKEN_RE = new RegExp(`^${TOK_OPEN}(\\d+)${TOK_CLOSE}$`);

// Блочный разбор: строки -> HTML (заголовки, списки, цитаты, таблицы, hr, абзацы).
function blocksMd(text) {
  const lines = text.split("\n");
  const html = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // пустая строка - разделитель блоков
    if (!line.trim()) { i++; continue; }

    // вынутый блок (код/блочная математика) стоит отдельной строкой - отдаём
    // его как есть, БЕЗ обёртки в <p> (иначе <pre> оказался бы внутри абзаца)
    if (BLOCK_TOKEN_RE.test(line.trim())) { html.push(line.trim()); i++; continue; }

    // горизонтальная черта
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { html.push("<hr>"); i++; continue; }

    // заголовок ATX
    const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (h) {
      const level = h[1].length;
      html.push(`<h${level}>${inlineMd(h[2].trim())}</h${level}>`);
      i++; continue;
    }

    // цитата. Внимание: текст УЖЕ экранирован, поэтому «>» здесь выглядит как
    // «&gt;» - по нему и опознаём, иначе цитаты не находятся вовсе.
    if (/^\s*&gt;/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*&gt;/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*&gt;\s?/, ""));
        i++;
      }
      html.push(`<blockquote>${blocksMd(quote.join("\n"))}</blockquote>`);
      continue;
    }

    // таблица: строка с | и следующая - разделитель
    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const header = tableCells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(tableCells(lines[i])); i++;
      }
      const thead = "<tr>" + header.map((c) => `<th>${inlineMd(c)}</th>`).join("") + "</tr>";
      const tbody = rows.map((r) =>
        "<tr>" + header.map((_, k) => `<td>${inlineMd(r[k] || "")}</td>`).join("") + "</tr>").join("");
      html.push(`<table class="md-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table>`);
      continue;
    }

    // списки (маркированный и нумерованный)
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        let content = lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "");
        i++;
        // строки-продолжения одного пункта (с отступом, без нового маркёра)
        while (i < lines.length && lines[i].trim()
               && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])
               && /^\s+/.test(lines[i])) {
          content += "\n" + lines[i].trim(); i++;
        }
        items.push(`<li>${inlineMd(content)}</li>`);
      }
      html.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }

    // абзац: копим строки до пустой/до начала другого блока
    const para = [];
    while (i < lines.length && lines[i].trim()
           && !/^\s*(#{1,6})\s+/.test(lines[i])
           && !/^\s*&gt;/.test(lines[i])
           && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])
           && !/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i])
           && !BLOCK_TOKEN_RE.test(lines[i].trim())
           && !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))) {
      para.push(lines[i]); i++;
    }
    html.push(`<p>${inlineMd(para.join("\n")).replace(/\n/g, "<br>")}</p>`);
  }
  return html.join("\n");
}

// ------------------------------------------------------------
// Точка входа
// ------------------------------------------------------------

// Превращает Markdown-текст модели в безопасный HTML. Порядок важен: сначала
// экранируем всё, затем вынимаем код и математику в заглушки (чтобы их
// содержимое не толковалось как Markdown), размечаем остальное и возвращаем
// заглушки на место.
export function renderMarkdown(raw) {
  if (raw == null) return "";
  let text = esc(String(raw)).replace(/\r\n?/g, "\n");

  const store = [];
  const stash = (htmlOut, block) => {
    const token = TOK_OPEN + store.length + TOK_CLOSE;
    store.push(htmlOut);
    // Блочные вставки (код, блочная математика) выносим на отдельную строку,
    // чтобы blocksMd отдал их без обёртки в <p> (см. BLOCK_TOKEN_RE).
    return block ? `\n\n${token}\n\n` : token;
  };

  // 1) блоки кода ```lang\n...```
  text = text.replace(/```([^\n`]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const cls = lang.trim() ? ` class="lang-${esc(lang.trim())}"` : "";
    return stash(`<pre class="md-pre"><code${cls}>${code.replace(/\n$/, "")}</code></pre>`, true);
  });

  // 2) блочная математика $$...$$ и \[...\]
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_, m) => stash(renderMath(m, true), true));
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (_, m) => stash(renderMath(m, true), true));

  // 3) инлайн-код `...`
  text = text.replace(/`([^`\n]+?)`/g, (_, code) => stash(`<code class="md-code">${code}</code>`));

  // 4) инлайн-математика \(...\) и $...$ (последнее с защитой от «цен»:
  //    содержимое не должно выглядеть как «5,00»)
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (_, m) => stash(renderMath(m, false)));
  text = text.replace(/\$([^\n$]+?)\$/g, (whole, m) => {
    if (/^[\s\d.,]+$/.test(m)) return whole;      // «$5,00» - это цена, не математика
    return stash(renderMath(m, false));
  });

  // 5) остальное - обычный Markdown
  let out = blocksMd(text);

  // 6) возвращаем заглушки (в несколько проходов: заглушка могла попасть внутрь
  //    другой - например инлайн-код в ячейке таблицы уже подставлен)
  const reTok = new RegExp(`${TOK_OPEN}(\\d+)${TOK_CLOSE}`, "g");
  for (let pass = 0; pass < 3 && out.includes(TOK_OPEN); pass++) {
    out = out.replace(reTok, (_, n) => store[+n] ?? "");
  }
  return out;
}
