import { config } from "../config.js";
import { api, appearancesFromRoutes } from "./api.js";
import { CASE_ZONES, caseZoneById, caseZoneMetrics, caseZoneName } from "./zones.js";
import { session, allowedPages, canEdit, canManageSite, initAuth, login, logout, demoRole, useWorker, loadAuthConfig, demoLoginEnabled, demoLogin, savedDemoRole } from "./auth.js";
import { landingHtml, demoBarHtml, guideHtml } from "./landing.js";
import { forgetDevice, requireSecureContext } from "./device.js";
import { createView } from "./view.js";
import { createCameraLive } from "./camera.js";

const root = document.querySelector("#app");
const paint = createView(root);
// Живое видео камеры на входе: в рабочем режиме кадры с рамками от сервера, в демо — только заглушка без запросов.
const cameraLive = createCameraLive({ enabled: config.mode === "api",
  fetchFrame: options => api.cameraFrame(options), fetchStatus: options => api.cameraStatus(options) });
let initialized = false;
let entryError = "";
let polling = false;
let gateSearchTimer;
const nowDate = () => new Intl.DateTimeFormat("sv-SE", { timeZone: config.timeZone || "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
const state = {
  page: "overview", employees: [], groups: [], attempts: [], dashboard: null, analytics: null, shiftReport: null, personReport: null, site: null, selectedRouteId: null,
  worker: null, workerId: "", workerPassage: null, gateEmployeeId: "", direction: "entry", challenge: null, passResult: null,
  cameraStream: null, faceBlob: null, faceFrames: [], faceUrl: null, employeeError: null, locationWatch: null, locationLastSent: 0,
  locationStatus: "Геолокация выключена", workerNotice: "", assistant: null, assistantInfo: null, busy: false, toast: null, loadError: null, mobileMenu: false, landingBusy: "", guideOpen: false,
  filters: { from: nowDate(), to: nowDate(), hourFrom: "0", hourTo: "23", employeeIds: "" },
};

const pageMeta = {
  overview: ["Обстановка", "Ситуация на объекте в реальном времени"],
  employees: ["Сотрудники", "Управление людьми и эталонными фотографиями"],
  gate: ["Проходная", "Вход и выход с подтверждением личности"],
  worker: ["Телефон рабочего", "Подтверждение прохода и геолокация смены"],
  analytics: ["Аналитика", "Появления работников, посещаемость зон и отчёты"],
  assistant: ["Помощник", "Вопросы и сводки по данным системы"],
  journal: ["Журнал проходов", "Все успешные и отклонённые попытки"],
  guide: ["Как проверить кейс", "Пошагово: что нажать, чтобы проверить все пункты кейса"],
};
const nav = [
  ["overview", "grid", "Обстановка"], ["employees", "users", "Сотрудники"],
  ["gate", "camera", "Проходная"], ["worker", "phone", "Телефон рабочего"],
  ["analytics", "chart", "Аналитика"], ["assistant", "spark", "Помощник"],
  ["journal", "list", "Журнал"], ["guide", "check", "Как проверить кейс"],
];

function esc(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]); }
const validDate = value => Boolean(value) && !Number.isNaN(Date.parse(value));
function dateTime(value) { return validDate(value) ? new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: config.timeZone || "Europe/Moscow" }).format(new Date(value)) : "—"; }
function time(value) { return validDate(value) ? new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: config.timeZone || "Europe/Moscow" }).format(new Date(value)) : "—"; }
function duration(value) {
  if (!validDate(value)) return "—";
  const mins = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60000));
  return `${Math.floor(mins / 60)} ч ${String(mins % 60).padStart(2, "0")} мин`;
}
function hoursMinutes(total) {
  const mins = Math.max(0, Math.round(Number(total) || 0));
  return mins >= 60 ? `${Math.floor(mins / 60)} ч ${String(mins % 60).padStart(2, "0")} мин` : `${mins} мин`;
}
function statusBadge(status) { return status === "inside" ? '<span class="badge badge-green">На объекте</span>' : '<span class="badge badge-muted">Вне объекта</span>'; }
function resultBadge(outcome) { if (outcome === "pending" || outcome === "error") return `<span class="badge badge-amber">${outcome === "pending" ? "Ожидание" : "Ошибка"}</span>`; return outcome === "allowed" ? '<span class="badge badge-green">Разрешён</span>' : '<span class="badge badge-red">Отказ</span>'; }
function dirName(direction) { return direction === "entry" ? "Вход" : direction === "exit" ? "Выход" : "Проход"; }
function initials(name) { return String(name || "?").split(" ").slice(0, 2).map(x => x[0] || "").join(""); }
function notify(message, kind = "info") { state.toast = { message, kind }; render(); setTimeout(() => { if (state.toast?.message === message) { state.toast = null; render(); } }, 5000); }
function errorMessage(error) {
  if (error?.name === "NotAllowedError") return "Подтверждение на устройстве отменено или время ожидания истекло. Повторите попытку.";
  if (error?.name === "SecurityError") return "Подтверждение устройства доступно только по HTTPS или на localhost.";
  if (error?.name === "InvalidStateError") return "Это устройство уже зарегистрировано. Обновите страницу или обратитесь к администратору.";
  if (error instanceof SyntaxError) return "Не удалось разобрать введённые данные. Проверьте скобки, запятые и кавычки.";
  // Английские и технические тексты браузера не показываем: человеку нужна понятная фраза.
  const message = String(error?.message || "").trim();
  return /[А-Яа-яЁё]/.test(message) ? message : "Не удалось выполнить действие. Повторите попытку.";
}

const ROLE_NAMES = { admin: "Администратор", manager: "Руководитель", controller: "Контролёр", worker: "Рабочий" };
const userLabel = name => String(name || "").replace(/\b(admin|manager|controller|worker)\b/g, role => ROLE_NAMES[role]);
function plural(n, forms) { const a = Math.abs(n) % 100, b = a % 10; return a > 10 && a < 20 ? forms[2] : b > 1 && b < 5 ? forms[1] : b === 1 ? forms[0] : forms[2]; }
const num = value => value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);
const fmtNum = value => new Intl.NumberFormat("ru-RU").format(value);

// Линейные иконки одного набора вместо символов шрифта: одинаково выглядят на любом телефоне.
const ICON_PATHS = {
  grid: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.6"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18 14.4c2 .8 3.5 2.7 3.5 5.6"/>',
  badge: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><circle cx="9" cy="11" r="2.2"/><path d="M5.8 16c.6-1.6 1.8-2.3 3.2-2.3s2.6.7 3.2 2.3"/><path d="M14.5 10h4M14.5 13.5h3"/>',
  camera: '<path d="M4 8.5A1.5 1.5 0 0 1 5.5 7H8l1.2-2h5.6L16 7h2.5A1.5 1.5 0 0 1 20 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5v-9Z"/><circle cx="12" cy="13" r="3.2"/>',
  phone: '<rect x="7" y="3" width="10" height="18" rx="2.5"/><path d="M11 17.5h2"/>',
  phoneoff: '<rect x="7" y="3" width="10" height="18" rx="2.5"/><path d="M11 17.5h2"/><path d="M4 4l16 16"/>',
  chart: '<path d="M4 4v16h16"/><path d="M8.5 16v-4M12.5 16V8M16.5 16v-6"/>',
  spark: '<path d="M11 3l1.9 5.3L18 10l-5.1 1.7L11 17l-1.9-5.3L4 10l5.1-1.7L11 3Z"/><path d="M18.5 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8Z"/>',
  list: '<path d="M8.5 6H20M8.5 12H20M8.5 18H20"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  door: '<path d="M10 4H6.5A1.5 1.5 0 0 0 5 5.5v13A1.5 1.5 0 0 0 6.5 20H10"/><path d="M13 8l4 4-4 4M17 12H9"/>',
  shield: '<path d="M12 3l7 3v5.5c0 4.2-2.9 7.6-7 9.5-4.1-1.9-7-5.3-7-9.5V6l7-3Z"/><path d="M9.5 9.5l5 5M14.5 9.5l-5 5"/>',
  lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.4M4 4v3.6h3.6"/><path d="M4 13a8 8 0 0 0 14.3 4.4M20 20v-3.6h-3.6"/>',
};
const ico = name => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICON_PATHS[name] || ""}</svg>`;
function emptyState(icon, title, text, action = "") {
  return `<div class="empty-state"><div class="empty-icon">${ico(icon)}</div><b>${esc(title)}</b><p>${esc(text)}</p>${action}</div>`;
}
// Карточка-метрика: число крупно, подпись, цветной акцент. Нет данных — понятная надпись, а не «null» или тире.
function metricCard({ tone, icon, label, value, suffix = "", note = "", meter = null, tag = "div", attrs = "" }) {
  const n = num(value);
  const fill = meter === null ? null : Math.round(Math.min(1, Math.max(0, meter)) * 100);
  return `<${tag} class="stat-card tone-${tone}" ${attrs}><span class="stat-icon">${ico(icon)}</span><span class="stat-label">${esc(label)}</span>${n === null ? '<strong class="stat-unknown">Нет данных</strong>' : `<strong>${fmtNum(n)}${esc(suffix)}</strong>`}${fill === null ? "" : `<span class="stat-meter" aria-hidden="true"><i style="width:${fill}%"></i></span>`}<small>${esc(note)}</small></${tag}>`;
}
const TAB_LABELS = { guide: "Проверка", worker: "Телефон" };

async function refresh(page = state.page) {
  try {
    if (!allowedPages().includes(page)) return;
    if (page === "overview") { state.dashboard = await api.dashboard(); state.dashboardAt = Date.now(); }
    if (page === "employees") {
      [state.employees, state.groups] = config.mode === "demo" ? [await api.employees(), []] : await Promise.all([api.employees(), api.groups()]);
    }
    if (page === "worker" && config.mode === "demo") state.employees = await api.employees();
    if (page === "gate") state.employees = await api.gatePeople();
    if (page === "journal") state.attempts = await api.attempts();
    if (page === "assistant") state.assistantInfo = await api.assistantStatus().catch(() => ({ failed: true, switchable: false }));
    if (page === "analytics") {
      const availablePeople = config.mode === "demo" ? await api.employees() : await api.reportPeople();
      [state.site, state.analytics, state.shiftReport] = await Promise.all([api.site(), api.analytics(state.filters), api.shiftReport(state.filters)]);
      state.employees = config.mode === "demo" ? availablePeople : [...new Map([
        ...state.employees, ...availablePeople,
        ...state.analytics.routes.map(route => ({ id: route.employeeId, name: route.employeeName })),
      ].map(person => [String(person.id), person])).values()];
    }
    if (page === "worker") {
      state.worker = await api.me();
      if (!state.workerId && config.mode === "demo") state.workerId = state.worker.demoEmployees?.[0]?.id || "";
      if (config.mode === "api") state.workerId = state.worker.employee?.id || "";
      if (config.mode === "demo") state.workerPassage = await api.workerPassage(state.workerId);
    }
    state.loadError = null;
  } catch (error) { state.loadError = errorMessage(error); }
  render();
}

function shell(content) {
  const [title, subtitle] = pageMeta[state.page];
  const visibleNav = nav.filter(([id]) => allowedPages().includes(id));
  const demo = config.mode === "demo";
  // Больше одного раздела — нижние вкладки; больше пяти — четыре вкладки и «Ещё» с полным списком.
  const multi = visibleNav.length > 1, more = visibleNav.length > 5;
  const tabs = more ? visibleNav.slice(0, 4) : visibleNav;
  const moreActive = more && !tabs.some(([id]) => id === state.page);
  return `<div class="app-shell page-${state.page} ${multi ? "has-tabs" : "no-tabs"}">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">П</div><div><strong>ПРОХОДНАЯ</strong><small>Контроль объекта</small></div></div>
      <div class="side-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
      <nav class="side-nav">${visibleNav.map(([id, icon, label]) => `<button class="nav-link ${state.page === id ? "active" : ""}" data-page="${id}"><span class="nav-icon">${ico(icon)}</span>${label}</button>`).join("")}</nav>
      <div class="sidebar-foot"><div class="connection-dot"></div><div><b>${demo ? "Деморежим" : "Система работает"}</b><small>${demo ? "Данные только в этом браузере" : "Данные обновляются автоматически"}</small></div></div>
    </aside>
    <div class="main-column"><header class="topbar">${more ? `<button class="mobile-menu-button" data-action="toggle-menu" aria-label="Открыть разделы" aria-expanded="${state.mobileMenu}">${ico("menu")}</button>` : ""}<div class="topbar-brand"><span class="brand-mark">П</span><strong>ПРОХОДНАЯ</strong></div><div class="breadcrumbs">ОБЪЕКТ <span>/</span> ${esc(title.toUpperCase())}</div><div class="top-actions"><span class="today">${new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(new Date())}</span><span class="user-name">${esc(userLabel(session.name))}</span><button class="button button-outline" data-action="logout">Выйти</button></div></header>
      <main class="main-content"><div class="page-heading"><div><div class="eyebrow">СИСТЕМА КОНТРОЛЯ ДОСТУПА</div><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div>${state.page === "overview" ? `<button class="button button-outline" data-action="refresh">${ico("refresh")}<span>Обновить</span></button>` : ""}</div>
        ${demo ? '<div class="demo-banner"><strong>Демонстрационный режим</strong><span>Данные и результаты прохода здесь условные и хранятся только в вашем браузере. Настоящую проверку телефона и лица выполняет сервер.</span></div>' : ""}
        ${session.dev ? '<div class="demo-banner"><strong>Тестовый вход</strong><span>Вы вошли без пароля на этом компьютере. Это только для проверки и не подтверждает безопасность прохода.</span></div>' : ""}
        ${demo ? `<label class="demo-role-switch"><span>Роль в демо</span><select id="demo-role">${["admin", "manager", "controller", "worker"].map(r => `<option value="${r}" ${r === (session.kind === "worker" ? "worker" : session.roles[0]) ? "selected" : ""}>${ROLE_NAMES[r]}</option>`).join("")}</select></label>` : ""}
        ${state.loadError ? `<div class="error-banner" role="alert"><span>${esc(state.loadError)}</span><button class="button button-outline" data-action="refresh">Повторить</button></div>` : ""}
        ${content}</main></div>
    ${multi ? `<nav class="mobile-nav" aria-label="Основные разделы">${tabs.map(([id, icon, label]) => `<button class="${state.page === id ? "active" : ""}" data-page="${id}"><span>${ico(icon)}</span><small>${TAB_LABELS[id] || label}</small></button>`).join("")}${more ? `<button class="${moreActive ? "active" : ""}" data-action="toggle-menu"><span>${ico("menu")}</span><small>Ещё</small></button>` : ""}</nav>` : ""}
    ${more && state.mobileMenu ? `<div class="mobile-menu-backdrop" data-action="close-menu"></div><div class="mobile-menu-panel" role="dialog" aria-label="Разделы приложения"><div class="mobile-menu-title"><b>Разделы</b><button data-action="close-menu" aria-label="Закрыть">×</button></div>${visibleNav.map(([id, icon, label]) => `<button class="mobile-menu-item ${state.page === id ? "active" : ""}" data-page="${id}"><span>${ico(icon)}</span>${label}</button>`).join("")}<p class="mobile-menu-user">Вы вошли как ${esc(userLabel(session.name))}</p></div>` : ""}
    ${state.toast ? `<div class="toast ${state.toast.kind}" role="status">${esc(state.toast.message)}</div>` : ""}
  </div>`;
}

function eventRows(events, limit) {
  if (!events?.length) return emptyState("list", "Событий пока нет", "Каждая попытка пройти, удачная или нет, появится здесь.");
  return `<div class="event-list ${limit ? "" : "event-list-full"}">${events.slice(0, limit || events.length).map(event => `<div class="event-row"><div class="event-icon ${event.outcome}">${event.outcome === "allowed" ? "✓" : "×"}</div><div class="event-main"><b>${esc(event.employeeName || "Неизвестный человек")}</b><span>${dirName(event.direction)} · ${esc(event.reason)}</span></div><div class="event-side">${resultBadge(event.outcome)}<small>${dateTime(event.at)}</small></div></div>`).join("")}</div>`;
}

function overview() {
  const d = state.dashboard;
  if (!d) return '<div class="panel loading">Загружаем обстановку…</div>';
  const inside = d.inside.length, total = num(d.totalEmployees);
  const cards = [
    metricCard({ tone: "green", icon: "users", label: "На объекте сейчас", value: inside,
      note: total === null ? "по открытым сменам" : `из ${fmtNum(total)} ${plural(total, ["сотрудника", "сотрудников", "сотрудников"])}`, meter: total ? inside / total : null }),
    total === null ? "" : metricCard({ tone: "blue", icon: "badge", label: "Всего сотрудников", value: total,
      note: total === 0 ? "список пока пуст" : `${fmtNum(Math.max(0, total - inside))} вне объекта` }),
    metricCard({ tone: "teal", icon: "door", label: "Проходов сегодня", value: d.allowedToday, suffix: d.todayCapped ? "+" : "",
      note: num(d.allowedToday) === null ? "сервер не отдал журнал за сегодня" : "входов и выходов разрешено" }),
    metricCard({ tone: "red", icon: "shield", label: "Отказов сегодня", value: d.deniedToday, suffix: d.todayCapped ? "+" : "", tag: "button", attrs: 'type="button" data-page="journal"',
      note: num(d.deniedToday) === null ? "сервер не отдал журнал за сегодня" : d.deniedToday > 0 ? "открыть журнал →" : "отказов не было" }),
  ].filter(Boolean);
  const nobodyInside = total === 0
    ? emptyState("users", "Пока никого нет", canEdit() ? "В системе нет ни одного сотрудника. Добавьте первого на вкладке «Сотрудники»." : "Заведите человека у администратора — после этого он появится в списках.",
      canEdit() ? '<button class="button button-primary" data-page="employees">Перейти к сотрудникам</button>' : "")
    : emptyState("door", "На объекте пока никого нет", "Сотрудники появятся здесь сразу после входа через проходную.");
  return `<div class="stats-grid">${cards.join("")}</div>${cameraLive.html()}<div class="two-columns"><section class="panel"><div class="panel-heading"><div><h2>Кто на объекте</h2><p>Сотрудники с открытой сменой</p></div><span class="count-pill">${inside}</span></div>
    ${inside ? `<div class="people-list">${d.inside.map(x => `<div class="person-row"><div class="person-avatar">${esc(initials(x.name))}</div><div><b>${esc(x.name)}</b><small>${esc(x.role || x.company || "Сотрудник")}${x.lastZone ? ` · ${esc(x.lastZone)}` : ""}</small></div><div class="person-time"><b>${time(x.enteredAt)}</b><small>${duration(x.enteredAt)}</small></div></div>`).join("")}</div>` : nobodyInside}
  </section><section class="panel"><div class="panel-heading"><div><h2>Последние проходы</h2><p>Входы, выходы и отказы</p></div><button class="text-button" data-page="journal">Весь журнал →</button></div>${eventRows(d.recentAttempts, 6)}</section></div>`;
}

const EMPLOYEE_FILTERS = {
  all: () => true,
  inside: x => x.status === "inside",
  phone: x => Boolean(x.enrolled),
};
function employees() {
  const list = state.employees, total = list.length;
  const count = key => list.filter(EMPLOYEE_FILTERS[key]).length;
  const inside = count("inside"), filter = EMPLOYEE_FILTERS[state.employeeFilter] ? state.employeeFilter : "all";
  const query = String(state.employeeQuery || "").trim().toLocaleLowerCase("ru");
  const shown = list.filter(x => EMPLOYEE_FILTERS[filter](x) && (!query || `${x.name} ${x.company} ${x.role}`.toLocaleLowerCase("ru").includes(query)));
  const filterCard = (key, tone, icon, label, value, note) => metricCard({ tone, icon, label, value, note, tag: "button", attrs: `type="button" data-action="employee-filter" data-value="${key}" aria-pressed="${filter === key}"` });
  const withPhone = count("phone");
  const stats = `<div class="stats-grid employee-stats">${[
    filterCard("all", "blue", "badge", "Всего сотрудников", total, total ? "заведены в системе" : "список пока пуст"),
    filterCard("inside", "green", "users", "На объекте", inside, `${fmtNum(total - inside)} вне объекта`),
    filterCard("phone", "teal", "phone", "Телефон привязан", withPhone, total && total > withPhone ? `ещё ${fmtNum(total - withPhone)} без телефона` : total ? "у всех привязан" : "список пока пуст"),
  ].join("")}</div>`;
  const rows = shown.map(x => `<tr><td class="c-person"><div class="table-person"><span class="person-avatar">${esc(initials(x.name))}</span><b>${esc(x.name)}</b></div></td><td class="c-org"><span class="org-main">${esc(x.company || "Без группы")}</span><span class="org-role">${esc(x.role || "должность не указана")}</span></td><td class="c-badges"><div class="badge-stack">${x.blocked ? '<span class="badge badge-red">Заблокирован</span>' : statusBadge(x.status)}${x.enrolled ? '<span class="badge badge-green">Телефон привязан</span>' : '<span class="badge badge-amber">Телефон не привязан</span>'}</div></td><td class="c-actions"><button class="text-button" data-action="edit-employee" data-id="${esc(x.id)}">Изменить</button><button class="text-button" data-action="enroll-code" data-id="${esc(x.id)}">Код для телефона</button>${config.mode === "demo" ? `<button class="text-button" data-action="revoke-device" data-id="${esc(x.id)}">Отозвать ключ</button>` : ""}<button class="text-button danger" data-action="deactivate" data-id="${esc(x.id)}" ${x.blocked ? "disabled" : ""}>Деактивировать</button></td></tr>`).join("");
  const body = !total
    ? emptyState("users", "Пока нет сотрудников", "Добавьте первого человека: понадобятся ФИО и чёткое фото лица.", '<button class="button button-primary" data-action="new-employee">＋ Добавить сотрудника</button>')
    : !shown.length
      ? emptyState("search", "Никого не найдено", "Измените запрос или сбросьте фильтр.", '<button class="button button-outline" data-action="employee-reset">Сбросить поиск и фильтр</button>')
      : `<div class="table-wrap"><table class="employee-table"><thead><tr><th>Сотрудник</th><th>Группа и должность</th><th>Статус и телефон</th><th>Действия</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  const enroll = state.enrollCode;
  return `<div class="content-toolbar"><div class="search-field"><span>${ico("search")}</span><input id="employee-search" type="search" value="${esc(state.employeeQuery || "")}" placeholder="Поиск по имени, группе, должности" aria-label="Поиск сотрудников" autocomplete="off"></div><button class="button button-primary" data-action="new-employee">＋ Добавить сотрудника</button></div>
    ${stats}
    ${enroll ? `<section class="panel enroll-card" id="enroll-card"><div class="enroll-main"><div class="eyebrow">ОДНОРАЗОВЫЙ КОД ДЛЯ ТЕЛЕФОНА${enroll.personName ? ` · ${esc(enroll.personName.toUpperCase())}` : ""}</div><strong class="enroll-code">${esc(enroll.code)}</strong><p>Действует до ${dateTime(enroll.expiresAt)}. Передайте код лично: сам по себе он не является пропуском.</p>${enroll.enrollUrl ? `<p>Ссылка для телефона: <a href="${esc(enroll.enrollUrl)}" target="_blank" rel="noopener noreferrer">${esc(enroll.enrollUrl)}</a></p>` : ""}<p class="hint-line">Откройте ссылку на телефоне рабочего или введите код на его экране в разделе «Моё устройство».</p></div><div class="enroll-actions">${globalThis.navigator?.clipboard ? '<button class="button button-outline" data-action="copy-enroll">Скопировать код</button>' : ""}<button class="button button-outline" data-action="dismiss-enroll">Скрыть</button></div></section>` : ""}
    <section class="panel table-panel"><div class="panel-heading"><div><h2>Список сотрудников</h2><p>${query || filter !== "all" ? `Найдено ${fmtNum(shown.length)} из ${fmtNum(total)}` : "Фото и статус поступают с сервера"}</p></div><span class="count-pill">${shown.length}</span></div>${body}</section>
    ${config.mode === "api" ? `<details class="panel group-panel"><summary>＋ Новая группа <small>бригада или подрядчик</small></summary><form id="group-form" class="filters-form"><label>Название<input name="name" required placeholder="Например: Монолит"></label><label>Родительская группа<select name="parentId"><option value="">Нет</option>${state.groups.map(group => `<option value="${esc(group.id)}">${esc(group.name)}</option>`).join("")}</select></label><button class="button button-outline">Добавить группу</button></form></details>` : ""}
    <dialog id="employee-dialog" class="modal"><form id="employee-form" class="modal-form"><div class="modal-heading"><div><div class="eyebrow">АДМИНИСТРИРОВАНИЕ</div><h2 id="employee-dialog-title">Новый сотрудник</h2></div><button type="button" class="icon-button" data-action="close-dialog" aria-label="Закрыть">×</button></div><input type="hidden" name="employeeId"><label>ФИО <input name="name" required placeholder="Иванов Иван Иванович" autocomplete="off"></label><div class="form-grid">${config.mode === "api" ? `<label>Группа <select name="groupId"><option value="">Без группы</option>${state.groups.map(group => `<option value="${esc(group.id)}">${esc(group.name)}</option>`).join("")}</select></label>` : '<label>Организация <input name="company" placeholder="Название подрядчика"></label>'}<label>Должность <input name="role" placeholder="Специальность" autocomplete="off"></label></div><label class="upload-box"><span class="upload-icon">▧</span><b>Эталонное фото</b><small>Один человек в кадре, лицо видно чётко.</small><input name="referencePhoto" type="file" accept="image/jpeg,image/png,image/webp" id="photo-input"><span id="file-name" class="file-name">Выбрать файл</span></label>${state.employeeError ? `<div class="error-banner" role="alert">${esc(state.employeeError)}</div>` : ""}<div class="modal-actions"><button type="button" class="button button-outline" data-action="close-dialog">Отмена</button><button class="button button-primary" type="submit" ${state.busy ? "disabled" : ""}>Сохранить</button></div></form></dialog>`;
}

function gateOptions() {
  return `<option value="">Выберите сотрудника</option>${state.employees.map(x => `<option value="${esc(x.id)}" ${x.id === state.gateEmployeeId ? "selected" : ""}>${esc(x.name)}${/^\d+$/.test(String(x.id)) ? ` · №${esc(x.id)}` : ""}${x.blocked ? " · заблокирован" : !x.enrolled ? " · телефон не привязан" : ""}</option>`).join("")}`;
}

function targetedGate() {
  const c = state.challenge, result = state.passResult;
  const expired = c && Date.parse(c.expiresAt) <= Date.now();
  const confirmed = c?.status === "confirmed" && !expired;
  return `<div class="gate-layout"><div class="gate-main">${cameraLive.html()}
    <section class="panel"><div class="step-heading"><span class="step-number">01</span><div><h2>Сотрудник и операция</h2><p>Найдите сотрудника по ФИО или номеру пропуска</p></div></div>
    <p class="hint-line">Выберите человека → он нажимает на телефоне «Я на проходной — получить код» → введите его 6 цифр → снимите лицо.</p>
    <label>Поиск по пропуску<input id="gate-person-search" type="search" placeholder="ФИО или номер пропуска" autocomplete="off"></label>
    <label>Сотрудник<select id="gate-person-select">${gateOptions()}</select></label>
    <div class="segment"><button class="${state.direction === "entry" ? "selected" : ""}" data-action="direction" data-value="entry">↗ Вход на объект</button><button class="${state.direction === "exit" ? "selected" : ""}" data-action="direction" data-value="exit">↙ Выход с объекта</button></div>
    <button class="button button-primary wide" data-action="create-challenge" ${state.busy || !state.gateEmployeeId ? "disabled" : ""}>Отправить запрос работнику</button></section>
    <section class="panel"><div class="step-heading"><span class="step-number">02</span><div><h2>Код с телефона рабочего</h2><p>Рабочий подтверждает запрос в своём профиле и сообщает вам шесть цифр</p></div></div>
    ${c ? `<div class="challenge-box"><div><b>${esc(c.personName || "Сотрудник")}</b><span>${dirName(c.direction)} · до ${time(c.expiresAt)}</span></div><span class="badge ${confirmed ? "badge-green" : expired ? "badge-red" : "badge-amber"}">${confirmed ? "Код подтверждён" : expired ? "Срок истёк" : "Ожидание кода"}</span></div>
    ${!confirmed && !expired ? `<form id="gate-code-form"><label>Код рабочего<input name="code" inputmode="numeric" autocomplete="off" pattern="[0-9]{6}" minlength="6" maxlength="6" placeholder="000000" required></label><button class="button button-primary wide" ${state.busy ? "disabled" : ""}>Проверить код</button></form>` : ""}` : '<div class="empty compact">Выберите сотрудника и отправьте запрос.</div>'}</section>
    <section class="panel"><div class="step-heading"><span class="step-number">03</span><div><h2>Проверка личности по лицу</h2><p>После проверки кода снимите лицо выбранного сотрудника</p></div></div>
    <div class="camera-box">${state.faceUrl ? `<img src="${state.faceUrl}" alt="Кадр для сравнения с эталоном">` : '<video id="camera-video" autoplay playsinline muted></video><div class="camera-placeholder" id="camera-placeholder">Камера ещё не запущена</div>'}</div>
    <p class="capture-settings">Снимем 3 кадра подряд. Держите лицо в кадре и не двигайтесь.</p><div class="camera-actions"><button class="button button-outline" data-action="start-camera" ${!confirmed || state.busy ? "disabled" : ""}>Включить камеру</button><button class="button button-outline" data-action="capture" ${state.cameraStream && confirmed && !state.busy ? "" : "disabled"}>Снять серию</button></div></section>
    <button class="button button-primary wide decision-button" data-action="decide-pass" ${confirmed && state.faceBlob && !state.busy ? "" : "disabled"}>Проверить лицо и получить решение</button>
    ${result ? `<div class="result-card ${result.outcome}" role="status"><strong>${result.outcome === "allowed" ? "Проход разрешён" : "Отказ в проходе"}</strong><span>${esc(result.employeeName || "Сотрудник")} · ${dirName(result.direction)}</span><p>${esc(result.reason)}</p></div>` : ""}</div>
    <aside class="gate-aside"><div class="info-card"><h3>Порядок прохода</h3><ol><li>Выберите человека по пропуску</li><li>Отправьте запрос на его телефон</li><li>Введите код, который назовёт рабочий</li><li>Снимите лицо и дождитесь решения</li></ol><a href="?worker=1" target="_blank" rel="noopener noreferrer">Открыть профиль рабочего ↗</a></div>${config.mode === "demo" ? '<div class="note-card"><b>Демонстрация</b><p>Код и решение имитируются. Подпись телефона, сравнение лица, журнал и открытие смены в рабочем режиме проверяет сервер.</p></div>' : '<div class="note-card"><b>Решение принимает сервер</b><p>Код проверяет сервер, лицо сравнивается с эталоном, смену открывает только разрешение системы.</p></div>'}</aside></div>`;
}

function workerCodeCard(p) {
  return `<div class="phone-section"><h3>${dirName(p.direction)} на проходной</h3><p>${esc(p.personName)} · код действует до ${time(p.expiresAt)}</p><div class="worker-pass-code" role="status" aria-label="Код подтверждения">${esc(p.code)}</div><p>Назовите эти шесть цифр контролёру. После ввода кода нужно пройти проверку лица. Смена откроется только после разрешения системы.</p>${config.mode === "api" ? '<button class="text-button" data-action="worker-get-code">Проверить снова</button>' : ""}</div>`;
}

function workerConfirmation() {
  const p = state.workerPassage;
  const live = p && Date.parse(p.expiresAt) > Date.now();
  if (config.mode === "api") {
    if (live && p.code) return workerCodeCard(p);
    const enrolled = Boolean(state.worker?.employee?.enrolled);
    return `<div class="phone-section"><h3>Код для прохода</h3><p>${esc(state.workerNotice || (enrolled ? "Подойдите к проходной. Когда контролёр выберет вас, нажмите кнопку и назовите ему шесть цифр." : "Сначала привяжите устройство в разделе «Моё устройство» ниже."))}</p><button class="button button-primary wide" data-action="worker-get-code" ${!enrolled || state.busy ? "disabled" : ""}>Я на проходной — получить код</button></div>`;
  }
  if (!live) return '<div class="phone-section"><h3>Код для прохода</h3><p>Новых запросов нет. Контролёр выберет вас по пропуску и отправит запрос на этот телефон.</p><button class="button button-outline wide" data-action="refresh">Проверить запросы</button></div>';
  if (p.code) return workerCodeCard(p);
  return `<div class="phone-section"><h3>${dirName(p.direction)} на проходной</h3><p>${esc(p.personName)} · запрос действует до ${time(p.expiresAt)}</p><p>Подтвердите, что вы сейчас на проходной, чтобы получить код.</p><button class="button button-primary wide" data-action="worker-get-code">Я на проходной — получить код</button></div>`;
}

function gate() { return targetedGate(); }

function worker() {
  const selected = state.worker?.employee || state.employees.find(x => x.id === state.workerId);
  const apiMode = config.mode === "api";
  return `<div class="phone-page"><div class="phone-frame"><div class="phone-top"><span>●●●</span><span>ПРОХОДНАЯ</span><span>◕</span></div><div class="phone-content"><div class="phone-welcome">МОЯ СМЕНА<h2>Здравствуйте${selected ? `, ${esc(selected.name.split(" ")[0])}` : ""}</h2><p>${esc(selected?.company || "Ваш пропуск на объект")}</p></div>${config.mode === "demo" ? `<label class="demo-worker-select">Сотрудник для демонстрации<select id="worker-select">${state.employees.map(x => `<option value="${esc(x.id)}" ${x.id === state.workerId ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>` : ""}
    <div class="shift-card"><div class="shift-card-top"><span>${apiMode ? "МОЯ СМЕНА" : "СТАТУС СМЕНЫ"}</span>${apiMode ? "" : selected?.status === "inside" ? "● НА ОБЪЕКТЕ" : "○ ВНЕ ОБЪЕКТА"}</div><strong>${apiMode ? "Статус смены видит контролёр" : selected?.status === "inside" ? "Смена открыта" : "Смена закрыта"}</strong><p>${apiMode ? "Вход и выход вы подтверждаете на проходной: назовите контролёру код." : selected?.status === "inside" ? `Вход в ${time(selected.enteredAt)} · ${duration(selected.enteredAt)} на объекте` : "Подтвердите вход на проходной, чтобы начать смену"}</p></div>
    ${workerConfirmation()}
    <div class="phone-section"><h3>Моё устройство</h3><p>${selected?.enrolled ? "Устройство привязано к профилю" : "Сначала привяжите устройство. Привязка по одноразовому коду, без логина и пароля."}</p>${selected?.enrolled ? "" : '<p class="hint-line">Код выдаёт администратор при заведении человека.</p>'}<label>Код для телефона<input id="enroll-code" autocomplete="off" placeholder="Код от администратора"></label><button class="button button-outline wide" data-action="enroll" ${selected?.enrolled || state.busy ? "disabled" : ""}>${selected?.enrolled ? "✓ Устройство привязано" : "Привязать устройство"}</button>${config.mode === "api" ? `<button class="text-button" data-action="reset-key">Привязать телефон заново</button>` : ""}</div>
    <div class="phone-section"><h3>Геолокация</h3><p>Во время открытой смены отправляйте координаты серверу с интервалом ${Math.round((apiMode ? Math.max(30000, config.locationIntervalMs) : config.locationIntervalMs) / 1000)} сек. ${apiMode ? "Сервер принимает координаты только при открытой смене." : ""}</p><div class="location-status">${esc(state.locationStatus)}</div><button class="button button-outline wide" data-action="toggle-location" ${(apiMode ? selected?.enrolled : selected?.status === "inside") ? "" : "disabled"}>${state.locationWatch === null ? "Включить геолокацию" : "Остановить передачу"}</button></div></div></div><div class="phone-explainer"><div class="eyebrow">ИНТЕРФЕЙС РАБОЧЕГО</div><h2>Личный телефон вместо бумажного пропуска</h2><p>На телефоне рабочий подтверждает одноразовый запрос проходной. После входа его местоположение передаётся в пределах открытой смены.</p><div class="note-card"><b>Для работы камеры, ключа устройства и геолокации</b><p>Открывайте сайт по HTTPS или на localhost. Разрешения браузера выдаются пользователем.</p></div></div></div>`;
}

function zoneMap(site, data, route) {
  const visits = route ? appearancesFromRoutes([route], data.zones).reverse() : [];
  const configured = CASE_ZONES.map(name => site.zones?.find(zone => caseZoneName(zone.name) === name));
  const width = Number(site.width), height = Number(site.height);
  const hasOutline = zone => Array.isArray(zone?.polygon) && zone.polygon.length >= 3 && zone.polygon.every(point =>
    Array.isArray(point) && point.length === 2 && point.every(value => Number.isFinite(Number(value))));
  const hasPlan = Boolean(site.planImageUrl && width > 0 && height > 0);
  if (hasPlan) {
    const polygons = configured.map((zone, index) => !hasOutline(zone) ? "" : `<polygon points="${zone.polygon.map(point => `${Number(point[0])},${Number(point[1])}`).join(" ")}" class="plan-zone ${visits.some(visit => visit.zoneName === CASE_ZONES[index]) ? "visited" : ""}"><title>${esc(CASE_ZONES[index])}</title></polygon>`).join("");
    const points = (route?.points || []).filter(point => point.x != null && point.y != null && Number.isFinite(Number(point.x)) && Number.isFinite(Number(point.y)));
    const path = points.length > 1 ? `<polyline class="plan-route" points="${points.map(point => `${Number(point.x)},${Number(point.y)}`).join(" ")}"/>` : "";
    const dots = points.map((point, index) => `<circle class="plan-route-point" cx="${Number(point.x)}" cy="${Number(point.y)}" r="6"><title>Точка ${index + 1}: ${dateTime(point.at)}</title></circle>`).join("");
    return `<div class="site-plan" style="aspect-ratio:${width}/${height}"><img src="${esc(site.planImageUrl)}" alt="План площадки"><svg viewBox="0 0 ${width} ${height}" aria-label="Зоны и маршрут работника">${polygons}${path}${dots}</svg></div><p class="metric-note">${configured.every(hasOutline) ? "Контуры зон и маршрут нанесены на план объекта." : "Контуры зон заданы не полностью. Их указывает руководитель в блоке «Настройка плана и зон»."}</p>`;
  }
  return `<div class="case-zone-map" role="list" aria-label="Три зоны из материалов кейса">${CASE_ZONES.map((name, index) => {
    const matches = visits.map((visit, step) => ({ visit, step })).filter(item => item.visit.zoneName === name);
    const photo = ["betonomeshalka.jpg", "kotlovan.jpg", "kurilka.jpg"][index];
    return `<div class="case-zone-card ${matches.length ? "visited" : ""}" role="listitem"><img src="src/assets/${photo}" alt="Фото зоны ${esc(name)} из материалов кейса" loading="lazy"><span class="case-zone-number">0${index + 1}</span><b>${esc(name)}</b><small>${route ? matches.length ? `Точки маршрута: ${matches.map(item => item.step + 1).join(", ")}` : "Нет точек работника" : "Зона площадки"}</small></div>`;
  }).join("")}</div><p class="metric-note">Фотографии взяты из материалов кейса. Расположение блоков условное: геопривязка зон не предоставлена. Линия на плане появится после загрузки плана и калибровки сервером.</p>`;
}

function mapView() {
  const site = state.site, data = state.analytics;
  if (!site || !data) return '<div class="panel loading">Загружаем аналитику…</div>';
  const apiMode = config.mode === "api";
  const zones = caseZoneMetrics(data.zones);
  const people = [...new Map([...state.employees, ...data.routes.map(route => ({ id: route.employeeId, name: route.employeeName }))]
    .map(person => [String(person.id), person])).values()].sort((a, b) => a.name.localeCompare(b.name, "ru"));
  const selected = people.find(person => String(person.id) === state.selectedRouteId);
  const route = data.routes.find(item => String(item.employeeId) === state.selectedRouteId);
  const visits = route ? appearancesFromRoutes([route], data.zones).reverse() : [];
  const max = Math.max(1, ...zones.map(zone => zone.count));
  const zoneRows = zones.map(zone => {
    const level = !zone.available ? "unavailable" : zone.count === 0 ? "empty" : zone.level === "low" || zone.count / max < .35 ? "low" : "active";
    const label = level === "unavailable" ? "Нет данных" : level === "empty" ? "Нет посещений" : level === "low" ? "Малая активность" : "Активная зона";
    return `<div class="zone-activity"><div><b>${esc(zone.name)}</b><small class="zone-activity-label ${level}">${label}</small></div><div class="zone-activity-track" aria-hidden="true"><span class="${level}" style="width:${zone.available && zone.count ? Math.max(5, zone.count / max * 100) : 0}%"></span></div><strong>${zone.available ? `${esc(new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(zone.count))} ${apiMode ? "мин" : "точек"}` : ""}</strong></div>`;
  }).join("");
  const report = apiMode
    ? `<section class="report-block"><h2>Табель сотрудника</h2><form id="person-report-form" class="filters-form"><label>Сотрудник <select name="personId" required><option value="">Выберите</option>${people.map(person => `<option value="${esc(person.id)}" ${state.personReport?.person?.id == person.id ? "selected" : ""}>${esc(person.name)}</option>`).join("")}</select></label><button class="button button-outline">Получить отчёт</button></form>${state.personReport ? `<div class="report-row"><b>${esc(state.personReport.person?.fio || "Сотрудник")}</b><span>Дней на объекте: ${Number(state.personReport.days_present || 0)}</span><strong>${hoursMinutes(state.personReport.total_worked_minutes)}</strong></div>${(state.personReport.shifts || []).map(shift => `<div class="report-row"><b>${dateTime(shift.started_at)}</b><span>${dateTime(shift.ended_at)}</span><strong>${hoursMinutes(shift.worked_minutes)}</strong></div>`).join("")}` : '<p class="metric-note">Выберите сотрудника, чтобы увидеть его табель за выбранный период.</p>'}</section>`
    : `<section class="report-block"><h2>Табель по людям</h2>${state.shiftReport?.people.length ? state.shiftReport.people.map(person => `<div class="report-row"><b>${esc(person.employeeName)}</b><span>${person.shiftCount} смен</span><strong>${hoursMinutes(person.workedMinutes)}</strong></div>`).join("") : '<div class="empty compact">Смен за выбранный период нет.</div>'}<h2>Смены</h2>${state.shiftReport?.shifts.length ? state.shiftReport.shifts.map(shift => `<div class="report-row"><b>${esc(shift.employeeName)}</b><span>${dateTime(shift.startedAt)} — ${shift.endedAt ? dateTime(shift.endedAt) : "сейчас"}</span><strong>${hoursMinutes(shift.workedMinutes)}</strong></div>`).join("") : '<div class="empty compact">Смен за выбранный период нет.</div>'}</section>`;
  const settings = canManageSite() && apiMode ? `<details class="panel settings-panel"><summary>Настройка плана и зон <small>для руководителя</small></summary><form id="plan-form" class="filters-form"><label>План площадки<input name="image" type="file" accept="image/png,image/jpeg,image/webp" required></label><button class="button button-outline">Загрузить план</button></form><form id="calibration-form" class="filters-form"><label>Калибровка: три точки (JSON)<textarea name="points" rows="5" required>${esc(JSON.stringify(site.calibration, null, 2))}</textarea></label><button class="button button-outline">Сохранить калибровку</button></form><form id="zones-form" class="filters-form">${CASE_ZONES.map((name, index) => `<label>${esc(name)}: полигон [[x,y],…]<textarea name="polygon${index}" rows="3" required>${esc(JSON.stringify(site.zones.find(zone => caseZoneName(zone.name) === name)?.polygon || []))}</textarea></label>`).join("")}<button class="button button-outline">Сохранить три зоны</button></form></details>` : "";
  return `<div class="map-layout"><section class="panel map-panel"><div class="panel-heading"><div><h2>Схема площадки</h2><p>Бетономешалка, котлован и курилка</p></div></div>${zoneMap(site, data, route)}
    <section id="route-detail" class="route-detail" aria-live="polite">${selected ? `<h2>Карта перемещений: ${esc(selected.name)}</h2>${visits.length ? `<ol class="route-timeline">${visits.map(visit => `<li><b>${esc(visit.zoneName)}</b><time datetime="${esc(visit.at)}">${dateTime(visit.at)}</time></li>`).join("")}</ol>` : '<div class="empty compact">Для этого работника нет точек за выбранный период.</div>'}` : '<div class="empty compact">Нажмите на ФИО работника, чтобы открыть его маршрут.</div>'}</section>
    <section class="report-block"><h2>Посещаемость трёх зон</h2><p class="metric-note">${esc(data.metric)}. «Нет данных» значит, что по зоне нет сведений, а «Нет посещений» — что в зоне никого не было.</p>${zoneRows}</section>${report}</section>
    <aside class="map-side"><section class="panel"><div class="panel-heading"><div><h2>Работники</h2><p>Откройте карту по ФИО</p></div></div><div class="worker-route-cards">${people.length ? people.map(person => { const count = data.routes.find(item => String(item.employeeId) === String(person.id))?.points.length || 0; return `<button type="button" class="worker-route-card ${state.selectedRouteId === String(person.id) ? "active" : ""}" data-action="show-route" data-id="${esc(person.id)}" aria-pressed="${state.selectedRouteId === String(person.id)}" aria-controls="route-detail"><span class="person-avatar small">${esc(initials(person.name))}</span><span><b>${esc(person.name)}</b><small>${count ? `${count} точек за период` : "Нет точек за период"}</small></span><span aria-hidden="true">→</span></button>`; }).join("") : '<div class="empty compact">Сотрудников пока нет.</div>'}</div></section>
    <section class="panel"><div class="panel-heading"><div><h2>Фильтры</h2><p>Для схемы и маршрутов</p></div></div><form id="filters-form" class="filters-form"><div class="form-grid"><label>С даты<input type="date" name="from" value="${esc(state.filters.from)}" required></label><label>По дату<input type="date" name="to" value="${esc(state.filters.to)}" required></label></div><div class="form-grid"><label>Часы с<input type="number" name="hourFrom" min="0" max="23" value="${esc(state.filters.hourFrom)}"></label><label>Часы до<input type="number" name="hourTo" min="0" max="23" value="${esc(state.filters.hourTo)}"></label></div><label>Сотрудники<select name="employeeIds" multiple size="4">${people.map(person => `<option value="${esc(person.id)}" ${state.filters.employeeIds.split(",").includes(String(person.id)) ? "selected" : ""}>${esc(person.name)}</option>`).join("")}</select><small>Без выбора показаны все сотрудники. Можно отметить нескольких.</small></label><button class="button button-primary wide">Показать данные</button></form></section>${settings}</aside></div>`;
}
const ASSISTANT_SUGGESTIONS = ["Кто сейчас на объекте?", "Сводка за сегодня", "Кто вчера отработал меньше 8 часов?", "В какой зоне больше всего людей?", "Отказы за неделю и причины"];
const ASSISTANT_STATUS = {
  ok: ["badge-green", "Ответ по данным системы"],
  no_data: ["badge-muted", "Данных нет"],
  unavailable: ["badge-amber", "Модель недоступна — показаны цифры системы"],
  bad_response: ["badge-amber", "Модель дала некорректный ответ — показаны цифры системы"],
};
const providerLabel = provider => provider === "ollama" ? "локальная модель (Ollama)" : provider === "external" ? "внешняя модель" : "модель";
function factsBlock(facts) {
  if (facts == null || (Array.isArray(facts) && !facts.length) || (typeof facts === "object" && !Array.isArray(facts) && !Object.keys(facts).length)) return "";
  const body = Array.isArray(facts) && facts.every(item => typeof item !== "object") ? `<ul>${facts.map(item => `<li>${esc(item)}</li>`).join("")}</ul>` : `<pre>${esc(typeof facts === "string" ? facts : JSON.stringify(facts, null, 2))}</pre>`;
  return `<details class="assistant-facts"><summary>Цифры системы</summary>${body}</details>`;
}
function assistantAnswer(answer) {
  if (!answer) return "";
  const [badge, label] = ASSISTANT_STATUS[answer.status] || ["badge-muted", "Статус ответа не распознан"];
  const replied = ["ok", "no_data"].includes(answer.status);
  const who = answer.provider || answer.model ? `<small class="assistant-model">${replied ? "Ответила" : "Модель (не ответила)"}: ${esc(providerLabel(answer.provider))}${answer.model ? ` ${esc(answer.model)}` : ""}</small>` : "";
  return `<div class="assistant-answer"><span class="eyebrow">ОТВЕТ СИСТЕМЫ</span> <span class="badge ${badge}">${esc(label)}</span><p>${esc(answer.answer || "Данных для ответа нет.")}</p>${factsBlock(answer.factsUsed)}${who}</div>`;
}
function assistantModelBlock() {
  const info = state.assistantInfo;
  let body;
  if (!info) body = '<p class="muted">Статус модели не загружен.</p>';
  else if (info.demo) body = '<p class="muted">Деморежим: модель не подключена.</p>';
  else if (info.failed) body = '<p class="muted">Статус модели недоступен. Переключение недоступно.</p>';
  else {
    const status = info.provider ? `<p><b>${esc(providerLabel(info.provider))}</b>${info.model ? ` · ${esc(info.model)}` : ""}</p><p><span class="badge ${info.configured ? "badge-green" : "badge-amber"}">${info.configured ? "Настроена" : "Не настроена"}</span> <span class="badge ${info.available ? "badge-green" : "badge-red"}">${info.available ? "Доступна" : "Недоступна"}</span></p>` : "";
    const switcher = info.switchable === false ? '<p class="muted">Переключение недоступно.</p>' : `<div class="segment">${[["ollama", "Локальная"], ["external", "Внешняя"]].map(([id, label]) => `<button class="${info.provider === id ? "selected" : ""}" data-action="set-provider" data-provider="${id}" ${state.busy ? "disabled" : ""}>${label}</button>`).join("")}</div>`;
    body = status + switcher;
  }
  return `<div class="assistant-model-block"><h3>Модель помощника</h3>${body}</div>`;
}
function assistant() {
  return `<div class="assistant-layout"><section class="panel assistant-panel"><div class="assistant-intro"><span class="assistant-mark">✦</span><div><h2>Спросите об объекте</h2><p>Ответы строятся по данным сервера. Если данных нет, помощник должен сказать об этом прямо.</p></div></div><div class="suggestions">${ASSISTANT_SUGGESTIONS.map(q => `<button data-action="suggest-question" data-question="${esc(q)}">${esc(q)} →</button>`).join("")}</div><form id="question-form" class="question-form"><textarea name="question" rows="3" placeholder="Например: кто вчера отработал меньше четырёх часов?" required></textarea><button class="button button-primary" ${state.busy ? "disabled" : ""}>Отправить вопрос</button></form>${state.assistantPending ? '<div class="assistant-answer assistant-thinking" role="status">Помощник готовит ответ…</div>' : assistantAnswer(state.assistant)}</section><aside class="panel summary-panel"><div class="panel-heading"><div><h2>Сводка за период</h2><p>Текст для передачи заказчику</p></div></div><form id="summary-form" class="filters-form"><label>С даты<input type="date" name="from" value="${nowDate()}" required></label><label>По дату<input type="date" name="to" value="${nowDate()}" required></label><button class="button button-outline wide" ${state.busy ? "disabled" : ""}>Сформировать сводку</button></form>${assistantModelBlock()}<div class="note-card"><b>Цифры считает система</b><p>Модель только формулирует ответ. Если данных нет — помощник так и скажет.</p></div></aside></div>`;
}

function journal() {
  const all = state.attempts, filter = ["allowed", "denied"].includes(state.journalFilter) ? state.journalFilter : "all";
  const denied = all.filter(event => event.outcome !== "allowed").length, allowed = all.length - denied;
  const shown = filter === "all" ? all : all.filter(event => (event.outcome === "allowed") === (filter === "allowed"));
  const tab = (key, label, n) => `<button class="${filter === key ? "selected" : ""}" data-action="journal-filter" data-value="${key}" aria-pressed="${filter === key}">${label} <span>${fmtNum(n)}</span></button>`;
  const empty = all.length
    ? emptyState("list", "В этой категории событий нет", "Выберите другой фильтр, чтобы увидеть остальные попытки.")
    : emptyState("list", "Проходов пока не было", "Каждая попытка пройти, удачная или нет, появится в журнале с понятной причиной.");
  return `<section class="panel"><div class="panel-heading"><div><h2>Все попытки прохода</h2><p>Успехи, отказы и понятные причины</p></div><span class="count-pill">${fmtNum(shown.length)}</span></div>${all.length ? `<div class="segment journal-filter" role="group" aria-label="Фильтр журнала">${tab("all", "Все", all.length)}${tab("allowed", "Разрешено", allowed)}${tab("denied", "Отказы", denied)}</div>` : ""}${shown.length ? eventRows(shown) : empty}</section>`;
}

// После каждой перерисовки блок камеры сам решает: опрашивать кадры, встать на паузу или остановиться.
function render() { draw(); cameraLive.sync(root); }

function draw() {
  if (!initialized || !allowedPages().includes(state.page)) {
    if (initialized && session.kind === "guest" && demoLoginEnabled()) { paint(landingHtml({ busyRole: state.landingBusy, error: entryError, guideOpen: state.guideOpen })); return; }
    paint(`<main class="main-content"><h1>Проходная</h1><p>${esc(entryError || (!initialized ? "Подключение…" : session.kind === "operator" ? "Доступ не назначен" : "Выберите способ входа"))}</p><button class="button button-primary" data-action="login">Вход оператора</button><button class="button button-outline" data-action="worker-entry">Телефон рабочего</button></main>`);
    return;
  }
  const content = ({ overview, employees, gate, worker, analytics: mapView, assistant, journal, guide: guideHtml })[state.page]();
  // Демо-стенд: смена роли без выхода и подсказка «Что здесь проверить» над содержимым экрана.
  const demoBar = demoLoginEnabled() && (session.demo || session.kind === "worker") ? demoBarHtml({ current: session.kind === "worker" ? "worker" : session.demo, page: state.page, busy: Boolean(state.landingBusy) }) : "";
  paint(shell(demoBar + content));
  if (state.page === "gate" && state.cameraStream && !state.faceUrl) {
    const video = document.querySelector("#camera-video");
    if (video) {
      if (video.srcObject !== state.cameraStream) video.srcObject = state.cameraStream;
      document.querySelector("#camera-placeholder")?.remove();
    }
  }
}

function stopCamera() { state.cameraStream?.getTracks().forEach(t => t.stop()); state.cameraStream = null; }
function stopLocation() { if (state.locationWatch !== null) navigator.geolocation.clearWatch(state.locationWatch); state.locationWatch = null; }
function clearChallenge() { state.passRequestId = null; state.challenge = null; state.passResult = null; state.faceBlob = null; state.faceFrames = []; if (state.faceUrl) URL.revokeObjectURL(state.faceUrl); state.faceUrl = null; stopCamera(); }
// Сервер мог сразу закрыть сессию отказом (правила, лимит попыток кода, отозванный телефон): показываем итог без камеры.
function refusedAtOnce() {
  const c = state.challenge;
  if (c?.status !== "used") return false;
  state.passResult = { employeeName: c.personName || "", direction: c.direction, outcome: "denied", reason: c.reason || "Сервер отказал в проходе", reasonCode: c.reasonCode };
  state.challenge = null; state.faceBlob = null; state.faceFrames = [];
  return true;
}
// Вход по кнопке роли (стартовый экран, переключатель, шаги «Как проверить кейс»): роль меняется без выхода, потом открывается нужный экран.
async function enterDemoRole(role, page) {
  if (state.landingBusy) return;
  entryError = "";
  if (!(session.kind === "operator" && session.demo === role)) {
    state.landingBusy = role; render();
    try { stopLocation(); clearChallenge(); await demoLogin(role); }
    catch (error) {
      state.landingBusy = "";
      if (session.kind === "guest") { entryError = errorMessage(error); render(); } else notify(errorMessage(error), "error");
      return;
    }
    state.landingBusy = ""; state.enrollCode = null; state.workerPassage = null; state.dashboard = null; state.worker = null; state.workerId = "";
    state.employees = []; state.attempts = []; state.assistant = null; state.assistantInfo = null; state.gateEmployeeId = "";
    // Из режима телефона рабочего (?worker=1, /enroll) уходим на корень, иначе перезагрузка вернёт на телефон.
    if (/(^|[?&])worker=1/.test(location.search) || /^\/enroll\/?$/.test(location.pathname)) history.replaceState(null, "", location.origin + "/");
  }
  state.page = allowedPages().includes(page) ? page : allowedPages()[0];
  state.mobileMenu = false; state.toast = null; state.loadError = null; render(); await refresh();
}
async function askAssistant(question) {
  if (state.busy) return;
  state.assistantPending = true;
  try { await run(async () => { state.assistant = await api.ask(question); }); } finally { state.assistantPending = false; render(); }
}
async function run(action) { if (state.busy) return; state.busy = true; render(); try { await action(); } catch (error) { notify(errorMessage(error), "error"); } finally { state.busy = false; render(); } }

root.addEventListener("click", async (event) => {
  const page = event.target.closest("[data-page]")?.dataset.page;
  if (page) {
    if (!allowedPages().includes(page)) { notify("Недостаточно прав", "error"); return; }
    if (page !== "gate") clearChallenge();
    state.page = page; state.mobileMenu = false; state.toast = null; state.loadError = null; render(); await refresh(); return;
  }
  const button = event.target.closest("[data-action]"); if (!button) return;
  const action = button.dataset.action;
  if (action === "show-route") {
    state.selectedRouteId = button.dataset.id;
    render();
    document.querySelector("#route-detail")?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (action === "demo-login" || action === "demo-switch") { await enterDemoRole(button.dataset.role); return; }
  if (action === "guide-go") { await enterDemoRole(button.dataset.role, button.dataset.go); return; }
  if (action === "toggle-guide") { state.guideOpen = !state.guideOpen; render(); return; }
  if (action === "login") { try { await login(); } catch (e) { entryError = errorMessage(e); render(); } return; }
  if (action === "worker-entry") { const url = new URL(location.href); url.search = "?worker=1"; url.hash = ""; location.assign(url); return; }
  if (action === "logout") { stopLocation(); clearChallenge(); state.enrollCode = null; try { await logout(); } finally { location.assign(location.pathname); } return; }
  if (["new-employee", "edit-employee", "enroll-code", "revoke-device", "deactivate"].includes(action) && !canEdit()) { notify("Недостаточно прав", "error"); return; }
  if (action === "enroll-code" || action === "revoke-device" || action === "deactivate") {
    await run(async () => {
      if (config.mode === "demo") throw new Error("Настоящие коды выдаёт только сервер. В демо этот шаг недоступен.");
      if (action === "enroll-code") state.enrollCode = { ...await api.enrollmentCode(button.dataset.id), personName: state.employees.find(x => x.id === button.dataset.id)?.name || "" };
      if (action === "revoke-device") { if (!confirm("Отвязать телефон этого сотрудника?")) return; await api.revokeDevice(button.dataset.id); }
      if (action === "deactivate") { if (state.employees.find(x => x.id === button.dataset.id)?.status === "inside") throw new Error("Нельзя деактивировать человека с открытой сменой."); if (!confirm("Деактивировать сотрудника?")) return; await api.deactivate(button.dataset.id); }
      await refresh("employees");
      // Код показан в карточке над списком: на телефоне прокручиваем к ней, иначе его не заметят.
      if (action === "enroll-code") document.querySelector("#enroll-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }); return;
  }
  if (action === "toggle-menu") { state.mobileMenu = !state.mobileMenu; render(); return; }
  if (action === "close-menu") { state.mobileMenu = false; render(); return; }
  if (action === "refresh") { await refresh(); return; }
  if (action === "new-employee" || action === "edit-employee") {
    state.employeeError = null;
    render();
    const employee = state.employees.find(x => x.id === button.dataset.id);
    const dialog = document.querySelector("#employee-dialog");
    dialog.querySelector("form").reset();
    dialog.querySelector("#file-name").textContent = "Выбрать файл";
    dialog.querySelector("#photo-input").required = !employee;
    dialog.querySelector("#employee-dialog-title").textContent = employee ? "Изменить сотрудника" : "Новый сотрудник";
    dialog.querySelector('[name="employeeId"]').value = employee?.id || "";
    for (const key of ["name", "role"]) dialog.querySelector(`[name="${key}"]`).value = employee?.[key] || "";
    if (config.mode === "api") dialog.querySelector('[name="groupId"]').value = employee?.groupId ?? "";
    else dialog.querySelector('[name="company"]').value = employee?.company || "";
    dialog.showModal(); return;
  }
  if (action === "close-dialog") { document.querySelector("#employee-dialog")?.close(); return; }
  if (action === "direction") { clearChallenge(); state.direction = button.dataset.value; render(); return; }
  if (action === "create-challenge") {
    await run(async () => {
      clearChallenge(); state.challenge = await api.createChallenge(state.direction, state.gateEmployeeId);
      if (refusedAtOnce()) return;
      notify(config.mode === "api" ? "Проход создан. Попросите рабочего нажать «Получить код» на телефоне и назвать шесть цифр." : "Запрос отправлен работнику. Попросите его назвать код с экрана телефона.", "success");
    }); return;
  }
  if (action === "worker-get-code") {
    await run(async () => {
      if (config.mode === "api") {
        state.workerPassage = await api.workerPassCode();
        state.workerNotice = state.workerPassage ? "" : "Контролёр ещё не создал проход для вас — подойдите к проходной.";
        return;
      }
      if (!state.workerPassage) throw new Error("Запрос больше недоступен.");
      await api.confirmPass(state.workerPassage.id, { demoEmployeeId: state.workerId });
      state.workerPassage = await api.workerPassage(state.workerId);
    }); return;
  }
  if (action === "start-camera") {
    try {
      requireSecureContext("Для камеры откройте приложение по HTTPS или на localhost.");
      stopCamera(); state.faceBlob = null; state.faceFrames = []; if (state.faceUrl) URL.revokeObjectURL(state.faceUrl); state.faceUrl = null;
      state.cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
      render();
    } catch { notify("Не удалось включить камеру. Разрешите доступ в браузере и откройте страницу по HTTPS или localhost.", "error"); }
    return;
  }
  if (action === "capture") {
    await run(async () => {
      const video = document.querySelector("#camera-video");
      if (!video?.videoWidth) throw new Error("Подождите, пока камера начнёт показывать изображение.");
      const canvas = document.createElement("canvas"); canvas.width = video.videoWidth; canvas.height = video.videoHeight;
      const count = 3;
      const frames = [];
      for (let i = 0; i < count; i++) {
        canvas.getContext("2d").drawImage(video, 0, 0);
        const frame = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", .88));
        if (!frame) throw new Error("Не удалось получить кадр с камеры.");
        frames.push(frame);
        if (i < count - 1) await new Promise(resolve => setTimeout(resolve, 300));
      }
      state.faceFrames = config.mode === "api" ? frames : [];
      state.faceBlob = frames.at(-1);
      if (state.faceUrl) URL.revokeObjectURL(state.faceUrl);
      state.faceUrl = URL.createObjectURL(state.faceBlob); stopCamera();
    }); return;
  }
  if (action === "decide-pass") {
    await run(async () => {
      state.passRequestId ||= crypto.randomUUID();
      state.passResult = await api.decidePass(state.challenge.id, state.direction, config.mode === "api" ? state.faceFrames : state.faceBlob, state.passRequestId);
      state.challenge = null; state.faceBlob = null; state.faceFrames = [];
      if (state.faceUrl) URL.revokeObjectURL(state.faceUrl); state.faceUrl = null;
      await refresh("gate");
    }); return;
  }
  if (action === "reset-key") {
    if (state.worker?.employee?.status === "inside") { notify("Сначала завершите смену.", "error"); return; }
    if (!confirm("Продолжайте только после того, как администратор отвяжет прежний телефон и выдаст новый код. Привязка на этом телефоне будет удалена без возможности восстановления.")) return;
    await run(async () => { stopLocation(); await forgetDevice(); state.worker = null; state.workerId = ""; await refresh("worker"); }); return;
  }
  if (action === "enroll") {
    const code = document.querySelector("#enroll-code")?.value.trim();
    await run(async () => {
      if (config.mode === "demo") await api.enroll(state.workerId);
      else { if (!code) throw new Error("Введите код для телефона от администратора."); await api.enroll(code); }
      await refresh("worker"); notify("Устройство привязано.", "success");
    }); return;
  }
  if (action === "toggle-location") {
    if (state.locationWatch !== null) { stopLocation(); state.locationStatus = "Геолокация выключена"; render(); return; }
    try { requireSecureContext("Для геолокации откройте приложение по HTTPS или на localhost."); }
    catch (error) { notify(errorMessage(error), "error"); return; }
    if (!navigator.geolocation) { notify("Этот браузер не поддерживает геолокацию.", "error"); return; }
    const employee = state.worker?.employee || state.employees.find(x => x.id === state.workerId);
    if (config.mode === "demo" && !employee?.shiftId) { notify("Геолокация доступна только во время открытой смены.", "error"); return; }
    state.locationStatus = "Ожидаем координаты…"; render();
    state.locationWatch = navigator.geolocation.watchPosition(async position => {
      if (Date.now() - state.locationLastSent < (config.mode === "api" ? Math.max(30000, config.locationIntervalMs) : config.locationIntervalMs)) return;
      state.locationLastSent = Date.now();
      try {
        await api.sendLocation(employee.shiftId, { latitude: position.coords.latitude, longitude: position.coords.longitude,
          accuracy: position.coords.accuracy, capturedAt: new Date(position.timestamp).toISOString() });
        state.locationStatus = `Последняя отправка: ${time(new Date())} · точность ${Math.round(position.coords.accuracy)} м`;
        if (config.mode === "api") state.locationStatus += " · статус смены проверяет сервер";
      } catch (error) { state.locationStatus = errorMessage(error); if (config.mode === "api") stopLocation(); else if (/Смена закрыта/.test(state.locationStatus)) stopLocation(); }
      if (state.page === "worker") render();
    }, () => { state.locationStatus = "Нет доступа к местоположению. Разрешите его в настройках браузера."; render(); }, { enableHighAccuracy: true, maximumAge: 10000, timeout: 15000 });
    return;
  }
  if (action === "set-provider") {
    await run(async () => { const next = await api.setAssistantProvider(button.dataset.provider); state.assistantInfo = next.unsupported ? { ...state.assistantInfo, switchable: false } : next; });
    return;
  }
  if (action === "suggest-question") {
    // Подсказка сразу отправляет вопрос; текст остаётся в поле, чтобы его можно было поправить и спросить заново.
    const field = document.querySelector('#question-form [name="question"]');
    if (field) field.value = button.dataset.question;
    await askAssistant(button.dataset.question); return;
  }
  if (action === "journal-filter") { state.journalFilter = button.dataset.value; render(); return; }
  if (action === "employee-filter") { state.employeeFilter = state.employeeFilter === button.dataset.value ? "all" : button.dataset.value; render(); return; }
  if (action === "employee-reset") { state.employeeFilter = "all"; state.employeeQuery = ""; render(); return; }
  if (action === "dismiss-enroll") { state.enrollCode = null; render(); return; }
  if (action === "copy-enroll") {
    try { await navigator.clipboard.writeText(state.enrollCode?.code || ""); notify("Код скопирован.", "success"); }
    catch { notify("Не удалось скопировать. Выделите код и скопируйте вручную.", "error"); }
    return;
  }
});

root.addEventListener("change", (event) => {
  if (event.target.id === "demo-role") {
    stopLocation(); clearChallenge(); state.enrollCode = null; state.workerPassage = null; demoRole(event.target.value);
    state.page = allowedPages()[0]; state.dashboard = null; state.worker = null; state.employees = []; state.attempts = [];
    render(); refresh(); return;
  }
  if (event.target.id === "gate-person-select") { clearChallenge(); state.gateEmployeeId = event.target.value; render(); return; }
  if (event.target.id === "worker-select") { stopLocation(); state.locationStatus = "Геолокация выключена"; state.workerId = event.target.value; state.workerPassage = null; refresh("worker"); }
  if (event.target.id === "photo-input") document.querySelector("#file-name").textContent = event.target.files[0]?.name || "Выбрать файл";
});
root.addEventListener("input", (event) => {
  if (event.target.id === "gate-person-search") {
    const query = event.target.value.trim().toLocaleLowerCase("ru");
    const select = document.querySelector("#gate-person-select");
    for (const option of select.options) option.hidden = Boolean(option.value && !option.textContent.toLocaleLowerCase("ru").includes(query));
    if (config.mode === "api") {
      // Список от сервера ограничен 50 людьми: при вводе просим сервер найти по ФИО и обновляем только выпадающий список.
      clearTimeout(gateSearchTimer);
      gateSearchTimer = setTimeout(async () => {
        try {
          const found = await api.gatePeople(event.target.value.trim());
          const selected = state.employees.find(x => x.id === state.gateEmployeeId);
          state.employees = selected && !found.some(x => x.id === selected.id) ? [selected, ...found] : found;
          select.innerHTML = gateOptions();
        } catch (error) { state.loadError = errorMessage(error); render(); }
      }, 300);
    }
    return;
  }
  if (event.target.id !== "employee-search") return;
  state.employeeQuery = event.target.value; render();
});

root.addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.target;
  if (form.id === "employee-form") {
    if (!canEdit()) { notify("Недостаточно прав", "error"); return; }
    const data = new FormData(form), employeeId = data.get("employeeId") || null;
    state.employeeError = null;
    await run(async () => {
      let result;
      try { result = await api.saveEmployee(employeeId, data); }
      catch (error) { state.employeeError = errorMessage(error); return; }
      await refresh("employees");
      document.querySelector("#employee-dialog")?.close();
      notify(result.photoCheck?.message || "Данные сотрудника сохранены.", "success");
    }); return;
  }
  if (form.id === "group-form") {
    if (!canEdit()) { notify("Недостаточно прав", "error"); return; }
    const data = new FormData(form);
    await run(async () => { await api.createGroup(String(data.get("name")).trim(), data.get("parentId")); await refresh("employees"); notify("Группа добавлена.", "success"); }); return;
  }
  if (form.id === "gate-code-form") {
    const code = String(new FormData(form).get("code") || "").trim();
    await run(async () => {
      if (!state.challenge) throw new Error("Сначала отправьте запрос работнику.");
      state.challenge = await api.verifyCode(state.challenge.id, code);
      form.reset();
      if (refusedAtOnce()) return;
      notify("Код подтверждён. Теперь проверьте лицо сотрудника.", "success");
    }); return;
  }
  if (form.id === "filters-form") {
    const data = new FormData(form);
    const from = data.get("from"), to = data.get("to"), hourFrom = Number(data.get("hourFrom")), hourTo = Number(data.get("hourTo"));
    if (from > to || hourFrom > hourTo) { notify("Проверьте даты и часы: начало должно быть раньше конца.", "error"); return; }
    state.filters = { from, to, hourFrom: String(hourFrom), hourTo: String(hourTo), employeeIds: [...form.querySelector('[name="employeeIds"]').selectedOptions].map(x => x.value).join(",") };
    await refresh("analytics"); return;
  }
  if (form.id === "person-report-form") {
    const id = new FormData(form).get("personId");
    await run(async () => { state.personReport = await api.personReport(id, state.filters.from, state.filters.to); }); return;
  }
  if (["plan-form", "calibration-form", "zones-form"].includes(form.id)) {
    if (!canManageSite()) { notify("Недостаточно прав", "error"); return; }
    const data = new FormData(form);
    await run(async () => {
      if (form.id === "plan-form") await api.updatePlan(data.get("image"));
      else if (form.id === "calibration-form") {
        const points = JSON.parse(String(data.get("points")));
        if (!Array.isArray(points) || points.length !== 3 || points.some(p => !Number.isFinite(Number(p.px)) || !Number.isFinite(Number(p.py)) || !Number.isFinite(Number(p.lat)) || !Number.isFinite(Number(p.lon)))) throw new Error("Нужны три точки с px, py, lat и lon.");
        await api.updateCalibration(points);
      } else {
        const zones = CASE_ZONES.map((name, index) => ({
          id: state.site.zones.find(zone => caseZoneName(zone.name) === name)?.id,
          name, polygon: JSON.parse(String(data.get(`polygon${index}`))),
        }));
        if (zones.some(zone => !Array.isArray(zone.polygon) || zone.polygon.length < 3 ||
          zone.polygon.some(point => !Array.isArray(point) || point.length !== 2 || point.some(value => !Number.isFinite(Number(value)))))) {
          throw new Error("Для каждой из трёх зон нужен полигон минимум из трёх точек [x,y].");
        }
        await api.updateZones(zones);
      }
      await refresh("analytics"); notify("Изменения сохранены.", "success");
    }); return;
  }
  if (form.id === "question-form" || form.id === "summary-form") {
    const data = new FormData(form);
    if (form.id === "question-form") { await askAssistant(String(data.get("question") || "")); return; }
    await run(async () => { state.assistant = await api.summary(data.get("from"), data.get("to")); });
  }
});

setInterval(async () => {
  if (!initialized || polling || state.busy || !allowedPages().includes(state.page)) return;
  polling = true;
  try {
  if (state.page === "gate" && state.challenge?.id && ["waiting", "confirmed"].includes(state.challenge.status)) {
    try { const next = await api.challenge(state.challenge.id); if (next.status !== state.challenge.status) { state.challenge = { ...state.challenge, ...next, code: next.code || state.challenge.code }; refusedAtOnce(); render(); } } catch (error) { state.loadError = errorMessage(error); render(); }
  }
  if (state.page === "worker") {
    try {
      const previousPassage = JSON.stringify(state.workerPassage);
      const before = state.worker?.employee || state.employees.find(x => x.id === state.workerId);
      if (config.mode === "demo") { state.employees = await api.employees(); state.workerPassage = await api.workerPassage(state.workerId); }
      else state.worker = await api.me();
      const after = state.worker?.employee || state.employees.find(x => x.id === state.workerId);
      if (state.locationWatch !== null && before?.shiftId && after?.shiftId !== before.shiftId) {
        stopLocation(); state.locationStatus = "Смена закрыта. Передача координат остановлена.";
      }
      const hadError = Boolean(state.loadError);
      state.loadError = null;
      if (hadError || previousPassage !== JSON.stringify(state.workerPassage) || JSON.stringify(before) !== JSON.stringify(after)) render();
    } catch (error) {
      const message = errorMessage(error);
      if (state.loadError !== message) { state.loadError = message; render(); }
    }
  }
  if (state.page === "overview" && Date.now() - (state.dashboardAt || 0) >= (config.mode === "api" ? Math.max(config.pollingMs, 10000) : config.pollingMs)) await refresh("overview");
  } finally { polling = false; }
}, config.pollingMs);

window.addEventListener("operator-session-expired", (event) => {
  stopLocation(); clearChallenge(); Object.assign(session, { kind: "guest", roles: [], name: "", demo: false });
  state.employees = []; state.attempts = []; state.dashboard = null; state.enrollCode = null; state.assistant = null; state.assistantInfo = null;
  entryError = event.detail?.message || "Сессия истекла, войдите заново"; render();
});
window.addEventListener("beforeunload", () => { stopCamera(); stopLocation(); cameraLive.stop(); });
render();
try { await initAuth(); } catch (error) { entryError = errorMessage(error); }
// Демо-стенд: сервер сообщает, можно ли входить кнопкой роли. Прошлая роль этой вкладки возвращается после перезагрузки.
await loadAuthConfig();
if (demoLoginEnabled() && session.kind === "guest" && savedDemoRole()) { try { await demoLogin(savedDemoRole()); entryError = ""; } catch { /* остаётся стартовый экран */ } }
initialized = true;
state.page = allowedPages()[0] || "overview";
render();
if (session.kind === "worker") {
  const activation = new URL(location.href);
  const code = activation.searchParams.get("code");
  if (code) {
    const input = document.querySelector("#enroll-code");
    if (input) input.value = code;
    activation.searchParams.delete("code");
    history.replaceState(null, "", activation.pathname + activation.search + activation.hash);
  }
}
if (allowedPages().length) await refresh();
