/* ============================================================
   Состояние вкладки и то, что читают оба экрана.

   S - ОТКРЫТАЯ СЕССИЯ. Объект мутируется на месте, а не
   переприсваивается: так его видят все модули, импортировавшие
   его один раз. Настоящее состояние сессии живёт на сервере, в
   session.json; здесь лишь то, что нужно нарисовать текущий кадр.
   ============================================================ */

import { esc } from "./util.js";

// Текущая открытая сессия. files - её файлы, logNext - номер строки лога,
// на которой мы остановились (лог тянем порциями, а не целиком).
export const S = { id: null, meta: null, files: [], logNext: 0 };

// Текущий вошедший пользователь: {login, canonical, is_admin} либо null.
// Мутируется на месте, как и S: модули импортируют объект один раз.
export const USER = { login: null, canonical: null, is_admin: false };

export function setUser(u) {
  Object.assign(USER, {
    login: u ? u.login : null,
    canonical: u ? u.canonical : null,
    is_admin: !!(u && u.is_admin),
  });
}

export function resetSession(id) {
  Object.assign(S, { id, meta: null, files: [], logNext: 0, report: null });
}

// Список принимаемых типов документов - приходит с бэкенда (/api/config),
// единым источником для выпадающих списков.
export const DOC_TYPES = [];

export function setDocTypes(types) {
  DOC_TYPES.length = 0;
  DOC_TYPES.push(...(types || []));
}

export function isBusy() {
  return S.meta && (S.meta.status === "queued" || S.meta.status === "running");
}

// Подпись статуса. Живёт здесь, а не на одном из экранов: её рисуют оба -
// карточка в списке и «хлебная крошка» открытой сессии, и расходиться они
// не должны.
export const STATUS_LABELS = {
  draft: "черновик", queued: "в очереди", running: "выполняется",
  done: "готово", error: "ошибка", cancelled: "отменена",
  interrupted: "прервана",
};

export function statusBadge(s) {
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
