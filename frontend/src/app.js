import { config } from "../config.js";
import { api, appearancesFromRoutes } from "./api.js";
import { session, allowedPages, canEdit, canManageSite, initAuth, login, logout, demoRole, useWorker } from "./auth.js";
import { forgetDevice } from "./device.js";
import { createView } from "./view.js";

const root = document.querySelector("#app");
const paint = createView(root);
let initialized = false;
let entryError = "";
let polling = false;
const nowDate = () => new Intl.DateTimeFormat("sv-SE", { timeZone: config.timeZone || "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
const state = {
  page: "overview", employees: [], groups: [], attempts: [], dashboard: null, analytics: null, shiftReport: null, personReport: null, site: null,
  worker: null, workerId: "", direction: "entry", challenge: null, passResult: null,
  cameraStream: null, faceBlob: null, faceFrames: [], faceUrl: null, locationWatch: null, locationLastSent: 0,
  locationStatus: "Геолокация выключена", assistant: null, busy: false, toast: null, loadError: null, mobileMenu: false,
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
};
const nav = [
  ["overview", "▦", "Обстановка"], ["employees", "♙", "Сотрудники"],
  ["gate", "◉", "Проходная"], ["worker", "▣", "Телефон рабочего"],
  ["analytics", "◇", "Аналитика"], ["assistant", "✦", "Помощник"],
  ["journal", "☷", "Журнал"],
];

function esc(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]); }
function dateTime(value) { return value ? new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: config.timeZone || "Europe/Moscow" }).format(new Date(value)) : "—"; }
function time(value) { return value ? new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: config.timeZone || "Europe/Moscow" }).format(new Date(value)) : "—"; }
function duration(value) {
  if (!value) return "—";
  const mins = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60000));
  return `${Math.floor(mins / 60)} ч ${String(mins % 60).padStart(2, "0")} мин`;
}
function statusBadge(status) { return status === "inside" ? '<span class="badge badge-green">На объекте</span>' : '<span class="badge badge-muted">Вне объекта</span>'; }
function resultBadge(outcome) { if (outcome === "pending" || outcome === "error") return `<span class="badge badge-amber">${outcome === "pending" ? "Ожидание" : "Ошибка"}</span>`; return outcome === "allowed" ? '<span class="badge badge-green">Разрешён</span>' : '<span class="badge badge-red">Отказ</span>'; }
function dirName(direction) { return direction === "entry" ? "Вход" : "Выход"; }
function initials(name) { return String(name || "?").split(" ").slice(0, 2).map(x => x[0] || "").join(""); }
function notify(message, kind = "info") { state.toast = { message, kind }; render(); setTimeout(() => { if (state.toast?.message === message) { state.toast = null; render(); } }, 5000); }
function errorMessage(error) {
  if (error?.name === "NotAllowedError") return "Подтверждение на устройстве отменено или время ожидания истекло. Повторите попытку.";
  if (error?.name === "SecurityError") return "Подтверждение устройства доступно только по HTTPS или на localhost.";
  if (error?.name === "InvalidStateError") return "Это устройство уже зарегистрировано. Обновите страницу или обратитесь к администратору.";
  return error?.message || "Произошла ошибка. Повторите попытку.";
}

async function refresh(page = state.page) {
  try {
    if (!allowedPages().includes(page)) return;
    if (page === "overview") state.dashboard = await api.dashboard();
    if (page === "employees") {
      [state.employees, state.groups] = config.mode === "demo" ? [await api.employees(), []] : await Promise.all([api.employees(), api.groups()]);
    }
    if (page === "worker" && config.mode === "demo") state.employees = await api.employees();
    if (page === "journal") state.attempts = await api.attempts();
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
    }
    state.loadError = null;
  } catch (error) { state.loadError = errorMessage(error); }
  render();
}

function shell(content) {
  const [title, subtitle] = pageMeta[state.page];
  const visibleNav = nav.filter(([id]) => allowedPages().includes(id));
  return `<div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">П</div><div><strong>ПРОХОДНАЯ</strong><small>Контроль объекта</small></div></div>
      <div class="side-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
      <nav class="side-nav">${visibleNav.map(([id, icon, label]) => `<button class="nav-link ${state.page === id ? "active" : ""}" data-page="${id}"><span class="nav-icon">${icon}</span>${label}</button>`).join("")}</nav>
      <div class="sidebar-foot"><div class="connection-dot"></div><div><b>${config.mode === "demo" ? "Деморежим" : "Подключение к API"}</b><small>${config.mode === "demo" ? "Данные только для разработки" : esc(config.apiBaseUrl)}</small></div></div>
    </aside>
    <div class="main-column"><header class="topbar"><button class="mobile-menu-button" data-action="toggle-menu" aria-label="Открыть разделы" aria-expanded="${state.mobileMenu}">☰</button><div class="breadcrumbs">ОБЪЕКТ <span>/</span> ${esc(title.toUpperCase())}</div><div class="top-actions"><span class="today">${new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(new Date())}</span><span>${esc(session.name)}</span><button class="button button-outline" data-action="logout">Выйти</button></div></header>
      <main class="main-content"><div class="page-heading"><div><div class="eyebrow">СИСТЕМА КОНТРОЛЯ ДОСТУПА</div><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div>${state.page === "overview" ? '<button class="button button-outline" data-action="refresh">↻ Обновить данные</button>' : ""}</div>
        ${config.mode === "demo" ? '<div class="demo-banner"><strong>Демонстрационный режим</strong><span>Примеры и результаты прохода имитируются в браузере. На защите переключите фронт на API: проверки лица, ECDSA и решения о проходе должны выполняться сервером.</span></div>' : ""}
        ${session.dev ? '<div class="demo-banner"><strong>Локальная разработка</strong><span>Запросы идут без токена. Это работает только с AUTH_DEV_BYPASS=true на бэкенде и не подтверждает безопасность прохода.</span></div>' : ""}
        ${config.mode === "demo" ? `<label>Демо-роль <select id="demo-role">${["admin", "manager", "controller", "worker"].map(r => `<option value="${r}" ${r === (session.kind === "worker" ? "worker" : session.roles[0]) ? "selected" : ""}>${r}</option>`).join("")}</select></label>` : ""}
        ${state.loadError ? `<div class="error-banner"><span>${esc(state.loadError)}</span><button class="button button-outline" data-action="refresh">Повторить</button></div>` : ""}
        ${content}</main></div>
    <nav class="mobile-nav" aria-label="Основные разделы">${visibleNav.filter(([id]) => ["overview", "gate", "worker", "analytics"].includes(id)).map(([id, icon, label]) => `<button class="${state.page === id ? "active" : ""}" data-page="${id}"><span>${icon}</span><small>${label}</small></button>`).join("")}<button class="${["employees", "assistant", "journal"].includes(state.page) ? "active" : ""}" data-action="toggle-menu"><span>☰</span><small>Ещё</small></button></nav>
    ${state.mobileMenu ? `<div class="mobile-menu-backdrop" data-action="close-menu"></div><div class="mobile-menu-panel" role="dialog" aria-label="Разделы приложения"><div class="mobile-menu-title"><b>Разделы</b><button data-action="close-menu" aria-label="Закрыть">×</button></div>${visibleNav.map(([id, icon, label]) => `<button class="mobile-menu-item ${state.page === id ? "active" : ""}" data-page="${id}"><span>${icon}</span>${label}</button>`).join("")}</div>` : ""}
    ${state.toast ? `<div class="toast ${state.toast.kind}">${esc(state.toast.message)}</div>` : ""}
  </div>`;
}

function eventRows(events, limit) {
  if (!events?.length) return '<div class="empty">Событий пока нет. Новые попытки появятся здесь.</div>';
  return `<div class="event-list">${events.slice(0, limit || events.length).map(event => `<div class="event-row"><div class="event-icon ${event.outcome}">${event.outcome === "allowed" ? "✓" : "×"}</div><div class="event-main"><b>${esc(event.employeeName || "Неизвестный человек")}</b><span>${dirName(event.direction)} · ${esc(event.reason)}</span></div><div class="event-side">${resultBadge(event.outcome)}<small>${dateTime(event.at)}</small></div></div>`).join("")}</div>`;
}

function overview() {
  const d = state.dashboard;
  if (!d) return '<div class="panel loading">Загружаем обстановку…</div>';
  return `<div class="stats-grid">
    <div class="stat-card"><div class="stat-icon green">♙</div><span>На объекте сейчас</span><strong>${d.inside.length}</strong><small>${d.totalEmployees == null ? "По открытым сменам" : `из ${d.totalEmployees} сотрудников`}</small></div>
    <div class="stat-card"><div class="stat-icon blue">◷</div><span>Открытых смен</span><strong>${d.activeShifts}</strong><small>активны сейчас</small></div>
    <div class="stat-card"><div class="stat-icon red">⊘</div><span>Отказов сегодня</span><strong>${d.deniedToday ?? "—"}</strong><small>${d.deniedToday == null ? "Сервер не отдаёт этот агрегат" : "смотрите журнал"}</small></div>
  </div><div class="two-columns"><section class="panel"><div class="panel-heading"><div><h2>Кто на объекте</h2><p>Сотрудники с открытой сменой</p></div><span class="count-pill">${d.inside.length}</span></div>
    ${d.inside.length ? `<div class="people-list">${d.inside.map(x => `<div class="person-row"><div class="person-avatar">${esc(initials(x.name))}</div><div><b>${esc(x.name)}</b><small>${esc(x.role || x.company || "Сотрудник")}</small></div><div class="person-time"><b>${time(x.enteredAt)}</b><small>${duration(x.enteredAt)}</small></div></div>`).join("")}</div>` : '<div class="empty">На объекте сейчас никого нет.</div>'}
  </section><section class="panel"><div class="panel-heading"><div><h2>Последние проходы</h2><p>Входы, выходы и отказы</p></div><button class="text-button" data-page="journal">Весь журнал →</button></div>${eventRows(d.recentAttempts, 6)}</section></div>`;
}

function employees() {
  return `<div class="content-toolbar"><div class="search-field"><span>⌕</span><input id="employee-search" placeholder="Поиск по имени или организации" aria-label="Поиск сотрудников"></div><button class="button button-primary" data-action="new-employee">＋ Добавить сотрудника</button></div>
    <section class="panel table-panel"><div class="panel-heading"><div><h2>Список сотрудников</h2><p>Фото и статус поступают с сервера</p></div><span class="count-pill">${state.employees.length}</span></div>
    <div class="table-wrap"><table><thead><tr><th>Сотрудник</th><th>Группа / должность</th><th>Статус</th><th>Телефон</th><th></th></tr></thead><tbody>${state.employees.map(x => `<tr data-search="${esc((x.name + " " + x.company).toLowerCase())}"><td><div class="table-person"><span class="person-avatar small">${esc(initials(x.name))}</span><b>${esc(x.name)}</b></div></td><td>${esc(x.company || "—")}<br><span class="muted">${esc(x.role || "—")}</span></td><td>${x.blocked ? '<span class="badge badge-red">Заблокирован</span>' : statusBadge(x.status)}</td><td>${x.enrolled ? '<span class="badge badge-green">Привязан</span>' : '<span class="badge badge-amber">Не привязан</span>'}</td><td><button class="text-button" data-action="edit-employee" data-id="${esc(x.id)}">Изменить →</button><button class="text-button" data-action="enroll-code" data-id="${esc(x.id)}">Enroll-код</button>${config.mode === "demo" ? `<button class="text-button" data-action="revoke-device" data-id="${esc(x.id)}">Отозвать ключ</button>` : ""}<button class="text-button" data-action="deactivate" data-id="${esc(x.id)}" ${x.blocked ? "disabled" : ""}>Деактивировать</button></td></tr>`).join("")}</tbody></table></div>${!state.employees.length ? '<div class="empty">Пока нет сотрудников. Добавьте первого человека.</div>' : ""}</section>
    ${state.enrollCode ? `<div class="panel"><b>Одноразовый enroll-код</b><p>${esc(state.enrollCode.code)} · до ${dateTime(state.enrollCode.expiresAt)}</p><p>Передайте лично рабочему. Код не является пропуском.</p></div>` : ""}
    ${config.mode === "api" ? `<section class="panel"><h2>Новая группа</h2><form id="group-form" class="filters-form"><label>Название<input name="name" required></label><label>Родительская группа<select name="parentId"><option value="">Нет</option>${state.groups.map(group => `<option value="${esc(group.id)}">${esc(group.name)}</option>`).join("")}</select></label><button class="button button-outline">Добавить группу</button></form></section>` : ""}
    <dialog id="employee-dialog" class="modal"><form id="employee-form" class="modal-form"><div class="modal-heading"><div><div class="eyebrow">АДМИНИСТРИРОВАНИЕ</div><h2 id="employee-dialog-title">Новый сотрудник</h2></div><button type="button" class="icon-button" data-action="close-dialog" aria-label="Закрыть">×</button></div><input type="hidden" name="employeeId"><label>ФИО <input name="name" required placeholder="Иванов Иван Иванович"></label><div class="form-grid">${config.mode === "api" ? `<label>Группа <select name="groupId"><option value="">Без группы</option>${state.groups.map(group => `<option value="${esc(group.id)}">${esc(group.name)}</option>`).join("")}</select></label>` : '<label>Организация <input name="company" placeholder="Название подрядчика"></label>'}<label>Должность <input name="role" placeholder="Специальность"></label></div><label class="upload-box"><span class="upload-icon">▧</span><b>Эталонное фото</b><small>Один человек в кадре, лицо видно чётко.</small><input name="referencePhoto" type="file" accept="image/jpeg,image/png,image/webp" id="photo-input"><span id="file-name" class="file-name">Выбрать файл</span></label><div class="modal-actions"><button type="button" class="button button-outline" data-action="close-dialog">Отмена</button><button class="button button-primary" type="submit">Сохранить</button></div></form></dialog>`;
}

function gate() {
  const c = state.challenge; const result = state.passResult;
  const apiMode = config.mode === "api";
  return `<div class="gate-layout"><div class="gate-main"><section class="panel"><div class="step-heading"><span class="step-number">01</span><div><h2>Выберите операцию</h2><p>Начните новый запрос на проход</p></div></div><div class="segment"><button class="${state.direction === "entry" ? "selected" : ""}" data-action="direction" data-value="entry">↗ Вход на объект</button><button class="${state.direction === "exit" ? "selected" : ""}" data-action="direction" data-value="exit">↙ Выход с объекта</button></div><button class="button button-primary wide" data-action="create-challenge" ${state.busy ? "disabled" : ""}>Создать запрос</button></section>
    <section class="panel"><div class="step-heading"><span class="step-number">02</span><div><h2>Подтверждение с телефона</h2><p>${apiMode ? "Передайте рабочему QR-данные для подписи на телефоне" : "Рабочий вводит одноразовый код на своём устройстве"}</p></div></div>${c ? `<div class="challenge-box"><div><small>${apiMode ? "QR-ДАННЫЕ" : "КОД ЗАПРОСА"}</small><strong style="overflow-wrap:anywhere;font-size:${apiMode ? "0.9rem" : "inherit"}">${esc(c.code)}</strong><span>Действует до ${time(c.expiresAt)}</span></div><span class="badge ${c.status === "confirmed" ? "badge-green" : c.status === "expired" ? "badge-red" : "badge-amber"}">${c.status === "confirmed" ? "Подтверждено" : c.status === "expired" ? "Истёк" : "Ожидание телефона"}</span></div>` : '<div class="empty compact">Сначала создайте запрос.</div>'}</section>
    <section class="panel"><div class="step-heading"><span class="step-number">03</span><div><h2>${apiMode ? "Три кадра лица" : "Снимок лица"}</h2><p>Лицо сравнивает сервер с эталонным фото</p></div></div><div class="camera-box">${state.faceUrl ? `<img src="${state.faceUrl}" alt="Снимок для проверки лица">` : '<video id="camera-video" autoplay playsinline muted></video><div class="camera-placeholder" id="camera-placeholder">Камера ещё не запущена</div>'}</div><div class="camera-actions"><button class="button button-outline" data-action="start-camera">${state.cameraStream ? "↻ Переснять" : "◉ Включить камеру"}</button><button class="button button-outline" data-action="capture" ${state.cameraStream ? "" : "disabled"}>${apiMode ? "Сделать три кадра" : "Сделать снимок"}</button></div></section>
    <button class="button button-primary wide decision-button" data-action="decide-pass" ${c?.status === "confirmed" && (apiMode ? state.faceFrames.length === 3 : state.faceBlob) && !state.busy ? "" : "disabled"}>Отправить на проверку сервера →</button>${result ? `<div class="result-card ${result.outcome}"><strong>${result.outcome === "allowed" ? "Проход разрешён" : result.outcome === "error" ? "Ошибка проверки" : result.outcome === "pending" ? "Проверка выполняется" : "Отказ в проходе"}</strong><span>${esc(result.employeeName)} · ${dirName(result.direction)}</span><p>${esc(result.reason)}</p></div>` : ""}</div>
    <aside class="gate-aside"><div class="info-card"><span class="info-symbol">⌁</span><h3>Порядок прохода</h3><ol><li>Создайте запрос на вход или выход</li><li>Попросите рабочего подтвердить код на телефоне</li><li>Сделайте снимок лица перед камерой</li><li>Дождитесь решения сервера</li></ol></div><div class="note-card"><b>Ответ принимает сервер</b><p>Фронт не вычисляет совпадение лиц и не открывает смену самостоятельно.</p></div></aside></div>`;
}

function worker() {
  const selected = state.worker?.employee || state.employees.find(x => x.id === state.workerId);
  const apiMode = config.mode === "api";
  return `<div class="phone-page"><div class="phone-frame"><div class="phone-top"><span>●●●</span><span>ПРОХОДНАЯ</span><span>◕</span></div><div class="phone-content"><div class="phone-welcome">МОЯ СМЕНА<h2>Здравствуйте${selected ? `, ${esc(selected.name.split(" ")[0])}` : ""}</h2><p>${esc(selected?.company || "Ваш пропуск на объект")}</p></div>${config.mode === "demo" ? `<label class="demo-worker-select">Сотрудник для демонстрации<select id="worker-select">${state.employees.map(x => `<option value="${esc(x.id)}" ${x.id === state.workerId ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>` : ""}
    <div class="shift-card"><div class="shift-card-top"><span>СТАТУС СМЕНЫ</span>${apiMode ? "— НЕТ ДАННЫХ" : selected?.status === "inside" ? "● НА ОБЪЕКТЕ" : "○ ВНЕ ОБЪЕКТА"}</div><strong>${apiMode ? "Статус недоступен" : selected?.status === "inside" ? "Смена открыта" : "Смена закрыта"}</strong><p>${apiMode ? "В API пока нет маршрута статуса смены рабочего." : selected?.status === "inside" ? `Вход в ${time(selected.enteredAt)} · ${duration(selected.enteredAt)} на объекте` : "Подтвердите вход на проходной, чтобы начать смену"}</p></div>
    <div class="phone-section"><h3>Подтвердить проход</h3><p>${apiMode ? "Вставьте QR-данные с экрана проходной и подпишите запрос устройством." : "Контролёр покажет одноразовый код запроса. Введите его здесь и подтвердите устройством."}</p><form id="confirm-form"><input name="code" ${apiMode ? 'placeholder="QR-данные {&quot;sid&quot;:...}"' : 'inputmode="numeric" pattern="[0-9]{6}" maxlength="6" placeholder="6-значный код"'} required><button class="button button-primary wide" ${state.busy || (apiMode && !selected?.enrolled) ? "disabled" : ""}>Подтвердить личность</button></form></div>
    <div class="phone-section"><h3>Моё устройство</h3><p>${selected?.enrolled ? "Устройство привязано к профилю" : "Сначала привяжите устройство. Регистрация по одноразовому enroll-коду, без логина и пароля."}</p><label>Enroll-код<input id="enroll-code" autocomplete="off" placeholder="Код от администратора"></label><button class="button button-outline wide" data-action="enroll" ${selected?.enrolled || state.busy ? "disabled" : ""}>${selected?.enrolled ? "✓ Устройство привязано" : "Привязать устройство"}</button>${config.mode === "api" ? `<button class="text-button" data-action="reset-key">Заменить отозванный ключ</button>` : ""}</div>
    <div class="phone-section"><h3>Геолокация</h3><p>Во время открытой смены отправляйте координаты серверу с интервалом ${Math.round((apiMode ? Math.max(30000, config.locationIntervalMs) : config.locationIntervalMs) / 1000)} сек. ${apiMode ? "Статус смены в API не доступен; приём определяет сервер." : ""}</p><div class="location-status">${esc(state.locationStatus)}</div><button class="button button-outline wide" data-action="toggle-location" ${(apiMode ? selected?.enrolled : selected?.status === "inside") ? "" : "disabled"}>${state.locationWatch === null ? "Включить геолокацию" : "Остановить передачу"}</button></div></div></div><div class="phone-explainer"><div class="eyebrow">ИНТЕРФЕЙС РАБОЧЕГО</div><h2>Личный телефон вместо бумажного пропуска</h2><p>На телефоне рабочий подтверждает одноразовый запрос проходной. После входа его местоположение передаётся в пределах открытой смены.</p><div class="note-card"><b>Для работы камеры, ключа устройства и геолокации</b><p>Открывайте сайт по HTTPS или на localhost. Разрешения браузера выдаются пользователем.</p></div></div></div>`;
}

function mapView() {
  const site = state.site, data = state.analytics;
  if (!site || !data) return '<div class="panel loading">Загружаем аналитику…</div>';
  const apiMode = config.mode === "api";
  const appearances = appearancesFromRoutes(data.routes, data.zones);
  const max = Math.max(1, ...data.zones.map(zone => Number(zone.count) || 0));
  const zoneRows = data.zones.map(zone => {
    const value = Number(zone.count) || 0;
    const level = value === 0 ? "empty" : zone.level === "low" || value / max < .35 ? "low" : "active";
    const label = level === "empty" ? "Нет посещений" : level === "low" ? "Малая активность" : "Активная зона";
    return `<div class="zone-activity"><div><b>${esc(zone.name)}</b><small class="zone-activity-label ${level}">${label}</small></div><div class="zone-activity-track" aria-hidden="true"><span class="${level}" style="width:${value === 0 ? 0 : Math.max(5, value / max * 100)}%"></span></div><strong>${esc(new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value))} ${apiMode ? "мин" : "точек"}</strong></div>`;
  }).join("");
  const report = apiMode
    ? `<section class="report-block"><h2>Табель сотрудника</h2><form id="person-report-form" class="filters-form"><label>Сотрудник <select name="personId" required><option value="">Выберите</option>${state.employees.map(person => `<option value="${esc(person.id)}" ${state.personReport?.person?.id == person.id ? "selected" : ""}>${esc(person.name)}</option>`).join("")}</select></label><button class="button button-outline">Получить отчёт</button></form>${state.personReport ? `<div class="report-row"><b>${esc(state.personReport.person?.fio || "Сотрудник")}</b><span>${Number(state.personReport.days_present || 0)} дней</span><strong>${Number(state.personReport.total_worked_minutes || 0)} мин</strong></div>${(state.personReport.shifts || []).map(shift => `<div class="report-row"><b>${dateTime(shift.started_at)}</b><span>${dateTime(shift.ended_at)}</span><strong>${Number(shift.worked_minutes || 0)} мин</strong></div>`).join("")}` : '<p class="metric-note">API предоставляет табель отдельно по сотруднику. Список пополняется людьми из текущего присутствия и маршрутов выбранного периода.</p>'}</section>`
    : `<section class="report-block"><h2>Табель по людям</h2>${state.shiftReport?.people.length ? state.shiftReport.people.map(person => `<div class="report-row"><b>${esc(person.employeeName)}</b><span>${person.shiftCount} смен</span><strong>${Math.floor(person.workedMinutes / 60)} ч ${String(person.workedMinutes % 60).padStart(2, "0")} мин</strong></div>`).join("") : '<div class="empty compact">Смен за выбранный период нет.</div>'}<h2>Смены</h2>${state.shiftReport?.shifts.length ? state.shiftReport.shifts.map(shift => `<div class="report-row"><b>${esc(shift.employeeName)}</b><span>${dateTime(shift.startedAt)} — ${shift.endedAt ? dateTime(shift.endedAt) : "сейчас"}</span><strong>${Math.floor(shift.workedMinutes / 60)} ч ${String(shift.workedMinutes % 60).padStart(2, "0")} мин</strong></div>`).join("") : '<div class="empty compact">Смен за выбранный период нет.</div>'}</section>`;
  const settings = !canManageSite() ? "" : apiMode
    ? `<section class="panel"><h2>Настройка зон</h2><form id="plan-form" class="filters-form"><label>План площадки<input name="image" type="file" accept="image/png,image/jpeg,image/webp" required></label><button class="button button-outline">Загрузить план</button></form><form id="calibration-form" class="filters-form"><label>Калибровка: три точки JSON<textarea name="points" rows="5" required>${esc(JSON.stringify(site.calibration, null, 2))}</textarea></label><button class="button button-outline">Сохранить калибровку</button></form><form id="zones-form" class="filters-form"><label>Зоны JSON: name и polygon [[x,y],…]<textarea name="zones" rows="8" required>${esc(JSON.stringify(site.zones, null, 2))}</textarea></label><button class="button button-outline">Сохранить зоны</button></form></section>`
    : `<section class="panel"><h2>Настройка зон</h2><form id="site-form" class="filters-form"><label>Название объекта<input name="name" value="${esc(site.name)}"></label><div class="form-grid"><label>Широта юг<input name="latMin" type="number" step="any" value="${site.latMin}"></label><label>Широта север<input name="latMax" type="number" step="any" value="${site.latMax}"></label><label>Долгота запад<input name="lonMin" type="number" step="any" value="${site.lonMin}"></label><label>Долгота восток<input name="lonMax" type="number" step="any" value="${site.lonMax}"></label></div><label>Названия зон по строкам<textarea name="zones" rows="5">${esc(site.zones.join("\n"))}</textarea></label><button class="button button-outline wide">Сохранить зоны</button></form></section>`;
  return `<div class="map-layout"><section class="panel map-panel"><div class="panel-heading"><div><h2>Где появлялись работники</h2><p>Точки геолокации за выбранный период, от новых к старым</p></div><span class="count-pill">${appearances.length}</span></div>
    ${appearances.length ? `<div class="appearance-list" role="list">${appearances.map(entry => `<div class="appearance-item" role="listitem"><div class="appearance-person"><span class="person-avatar small">${esc(initials(entry.employeeName))}</span><b>${esc(entry.employeeName)}</b></div><span class="appearance-place">${esc(entry.zoneName)}</span><time datetime="${esc(entry.at)}">${dateTime(entry.at)}</time></div>`).join("")}</div>` : '<div class="empty compact">За выбранный период нет точек геолокации. Проверьте фильтры или передачу координат.</div>'}
    <p class="metric-note">Каждая строка — время полученной точки; это не непрерывное присутствие в зоне. Время показано по часовому поясу объекта.</p>
    <section class="report-block"><h2>Посещаемость зон</h2><p class="metric-note">${esc(data.metric)}. Пустые и мало посещаемые зоны отмечены отдельно.</p>${zoneRows || '<div class="empty compact">Зоны пока не настроены.</div>'}${apiMode ? `<p class="metric-note">Вне размеченных зон: ${Number(data.outsideMinutes || 0)} мин</p>` : ""}</section>${report}</section>
    <aside class="map-side"><section class="panel"><div class="panel-heading"><div><h2>Фильтры</h2><p>Для списка и посещаемости зон</p></div></div><form id="filters-form" class="filters-form"><div class="form-grid"><label>С даты<input type="date" name="from" value="${esc(state.filters.from)}" required></label><label>По дату<input type="date" name="to" value="${esc(state.filters.to)}" required></label></div><div class="form-grid"><label>Часы с<input type="number" name="hourFrom" min="0" max="23" value="${esc(state.filters.hourFrom)}"></label><label>Часы до<input type="number" name="hourTo" min="0" max="23" value="${esc(state.filters.hourTo)}"></label></div><label>Сотрудники<select name="employeeIds" multiple size="4">${state.employees.map(person => `<option value="${esc(person.id)}" ${state.filters.employeeIds.split(",").includes(person.id) ? "selected" : ""}>${esc(person.name)}</option>`).join("")}</select><small>Без выбора — все сотрудники. Ctrl для выбора нескольких.</small></label><button class="button button-primary wide">Показать данные</button></form></section><section class="panel"><h2>Люди в маршрутах</h2>${data.routes.length ? data.routes.map(route => `<div class="route-person"><span>${esc(route.employeeName)}</span><small>${route.points.length} точек</small></div>`).join("") : '<div class="empty compact">Для выбранного периода нет маршрутов.</div>'}</section>${settings}</aside></div>`;
}
function assistant() {
  return `<div class="assistant-layout"><section class="panel assistant-panel"><div class="assistant-intro"><span class="assistant-mark">✦</span><div><h2>Спросите об объекте</h2><p>Ответы строятся по данным сервера. Если данных нет, помощник должен сказать об этом прямо.</p></div></div><div class="suggestions">${["Кто сейчас на объекте?", "Были ли отказы на входе за неделю и почему?", "В какой зоне вчера было больше всего людей?"].map(q => `<button data-action="suggest-question" data-question="${esc(q)}">${esc(q)} →</button>`).join("")}</div><form id="question-form" class="question-form"><textarea name="question" rows="3" placeholder="Например: кто вчера отработал меньше четырёх часов?" required></textarea><button class="button button-primary" ${state.busy ? "disabled" : ""}>Отправить вопрос</button></form>${state.assistant ? `<div class="assistant-answer"><span class="eyebrow">ОТВЕТ СИСТЕМЫ</span><p>${esc(state.assistant.answer || "Данных для ответа нет.")}</p>${state.assistant.status === "unavailable" ? '<small>Попробуйте позже после подключения сервера и модели.</small>' : ""}</div>` : ""}</section><aside class="panel summary-panel"><div class="panel-heading"><div><h2>Сводка за период</h2><p>Текст для передачи заказчику</p></div></div><form id="summary-form" class="filters-form"><label>С даты<input type="date" name="from" value="${nowDate()}" required></label><label>По дату<input type="date" name="to" value="${nowDate()}" required></label><button class="button button-outline wide" ${state.busy ? "disabled" : ""}>Сформировать сводку</button></form><div class="note-card"><b>Числа считает система</b><p>Модель формулирует текст на основе проверенных агрегатов. Фронт показывает ответ и не подставляет вымышленные цифры.</p></div></aside></div>`;
}

function journal() {
  return `<section class="panel"><div class="panel-heading"><div><h2>Все попытки прохода</h2><p>Успехи, отказы и понятные причины</p></div><span class="count-pill">${state.attempts.length}</span></div>${eventRows(state.attempts)}</section>`;
}

function render() {
  if (!initialized || !allowedPages().includes(state.page)) {
    paint(`<main class="main-content"><h1>Проходная</h1><p>${esc(entryError || (!initialized ? "Подключение…" : session.kind === "operator" ? "Доступ не назначен" : "Выберите способ входа"))}</p><button class="button button-primary" data-action="login">Вход оператора</button><button class="button button-outline" data-action="worker-entry">Телефон рабочего</button></main>`);
    return;
  }
  const content = ({ overview, employees, gate, worker, analytics: mapView, assistant, journal })[state.page]();
  paint(shell(content));
  if (state.page === "gate" && state.cameraStream && !state.faceUrl) {
    const video = document.querySelector("#camera-video");
    if (video) { video.srcObject = state.cameraStream; document.querySelector("#camera-placeholder")?.remove(); }
  }
}

function stopCamera() { state.cameraStream?.getTracks().forEach(t => t.stop()); state.cameraStream = null; }
function stopLocation() { if (state.locationWatch !== null) navigator.geolocation.clearWatch(state.locationWatch); state.locationWatch = null; }
function clearChallenge() { state.passRequestId = null; state.challenge = null; state.passResult = null; state.faceBlob = null; state.faceFrames = []; if (state.faceUrl) URL.revokeObjectURL(state.faceUrl); state.faceUrl = null; stopCamera(); }
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
  if (action === "login") { try { await login(); } catch (e) { entryError = errorMessage(e); render(); } return; }
  if (action === "worker-entry") { const url = new URL(location.href); url.search = "?worker=1"; url.hash = ""; location.assign(url); return; }
  if (action === "logout") { stopLocation(); clearChallenge(); state.enrollCode = null; try { await logout(); } finally { location.assign(location.pathname); } return; }
  if (["new-employee", "edit-employee", "enroll-code", "revoke-device", "deactivate"].includes(action) && !canEdit()) { notify("Недостаточно прав", "error"); return; }
  if (action === "enroll-code" || action === "revoke-device" || action === "deactivate") {
    await run(async () => {
      if (config.mode === "demo") throw new Error("Эта операция требует backend; демо не выпускает настоящие коды.");
      if (action === "enroll-code") state.enrollCode = await api.enrollmentCode(button.dataset.id);
      if (action === "revoke-device") { if (!confirm("Отозвать ключ телефона?")) return; await api.revokeDevice(button.dataset.id); }
      if (action === "deactivate") { if (state.employees.find(x => x.id === button.dataset.id)?.status === "inside") throw new Error("Нельзя деактивировать человека с открытой сменой."); if (!confirm("Деактивировать сотрудника?")) return; await api.deactivate(button.dataset.id); }
      await refresh("employees");
    }); return;
  }
  if (action === "toggle-menu") { state.mobileMenu = !state.mobileMenu; render(); return; }
  if (action === "close-menu") { state.mobileMenu = false; render(); return; }
  if (action === "refresh") { await refresh(); return; }
  if (action === "new-employee" || action === "edit-employee") {
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
  if (action === "create-challenge") { await run(async () => { clearChallenge(); state.challenge = await api.createChallenge(state.direction); notify(config.mode === "api" ? "Запрос создан. Передайте QR-данные рабочему." : "Запрос создан. Попросите рабочего подтвердить код на телефоне.", "success"); }); return; }
  if (action === "start-camera") {
    try {
      stopCamera(); state.faceBlob = null; state.faceFrames = []; if (state.faceUrl) URL.revokeObjectURL(state.faceUrl); state.faceUrl = null;
      state.cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
      render();
    } catch { notify("Не удалось включить камеру. Разрешите доступ в браузере и откройте страницу по HTTPS или localhost.", "error"); }
    return;
  }
  if (action === "capture") {
    const video = document.querySelector("#camera-video");
    if (!video?.videoWidth) { notify("Подождите, пока камера начнёт показывать изображение.", "error"); return; }
    const canvas = document.createElement("canvas"); canvas.width = video.videoWidth; canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    if (config.mode === "api") {
      state.faceFrames = [];
      for (let i = 0; i < 3; i++) {
        canvas.getContext("2d").drawImage(video, 0, 0);
        const frame = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", .88));
        if (!frame) throw new Error("Не удалось получить кадр с камеры.");
        state.faceFrames.push(frame);
        if (i < 2) await new Promise(resolve => setTimeout(resolve, 250));
      }
      state.faceBlob = state.faceFrames[2];
    } else state.faceBlob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", .88));
    state.faceUrl = URL.createObjectURL(state.faceBlob); stopCamera(); render(); return;
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
    if (!confirm("Продолжайте только после отзыва прежнего ключа администратором и получения нового enroll-кода. Старый ключ будет удалён с этого устройства без возможности восстановления.")) return;
    await run(async () => { stopLocation(); await forgetDevice(); state.worker = null; state.workerId = ""; await refresh("worker"); }); return;
  }
  if (action === "enroll") {
    const code = document.querySelector("#enroll-code")?.value.trim();
    await run(async () => {
      if (config.mode === "demo") await api.enroll(state.workerId);
      else { if (!code) throw new Error("Введите enroll-код от администратора."); await api.enroll(code); }
      await refresh("worker"); notify("Устройство привязано.", "success");
    }); return;
  }
  if (action === "toggle-location") {
    if (state.locationWatch !== null) { stopLocation(); state.locationStatus = "Геолокация выключена"; render(); return; }
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
  if (action === "suggest-question") { document.querySelector('#question-form [name="question"]').value = button.dataset.question; document.querySelector('#question-form [name="question"]').focus(); }
});

root.addEventListener("change", (event) => {
  if (event.target.id === "demo-role") {
    stopLocation(); clearChallenge(); state.enrollCode = null; demoRole(event.target.value);
    state.page = allowedPages()[0]; state.dashboard = null; state.worker = null; state.employees = []; state.attempts = [];
    render(); refresh(); return;
  }
  if (event.target.id === "worker-select") { stopLocation(); state.locationStatus = "Геолокация выключена"; state.workerId = event.target.value; render(); }
  if (event.target.id === "photo-input") document.querySelector("#file-name").textContent = event.target.files[0]?.name || "Выбрать файл";
});
root.addEventListener("input", (event) => {
  if (event.target.id !== "employee-search") return;
  const query = event.target.value.toLowerCase();
  document.querySelectorAll("tr[data-search]").forEach(row => { row.hidden = !row.dataset.search.includes(query); });
});

root.addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.target;
  if (form.id === "employee-form") {
    if (!canEdit()) { notify("Недостаточно прав", "error"); return; }
    const data = new FormData(form), employeeId = data.get("employeeId") || null;
    await run(async () => {
      const result = await api.saveEmployee(employeeId, data);
      await refresh("employees");
      document.querySelector("#employee-dialog")?.close();
      notify(result.photoCheck?.message || "Данные сотрудника отправлены серверу.", "success");
    }); return;
  }
  if (form.id === "group-form") {
    if (!canEdit()) { notify("Недостаточно прав", "error"); return; }
    const data = new FormData(form);
    await run(async () => { await api.createGroup(String(data.get("name")).trim(), data.get("parentId")); await refresh("employees"); notify("Группа отправлена серверу.", "success"); }); return;
  }
  if (form.id === "confirm-form") {
    const code = new FormData(form).get("code").trim();
    await run(async () => {
      const options = await api.passOptions(code);
      const assertion = config.mode === "demo" ? { demoEmployeeId: state.workerId } : options;
      if (config.mode !== "demo" && !confirm(`Подтвердить ${dirName(options.direction).toLowerCase()}?`)) return;
      const result = await api.confirmPass(options.challengeId, assertion);
      notify(`Подтверждение отправлено${result.employeeName ? `: ${result.employeeName}` : ""}. Ожидайте решения проходной.`, "success");
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
        const zones = JSON.parse(String(data.get("zones")));
        if (!Array.isArray(zones) || zones.some(z => !z.name || !Array.isArray(z.polygon) || z.polygon.length < 3)) throw new Error("Для каждой зоны нужны имя и многоугольник из трёх точек.");
        await api.updateZones(zones);
      }
      await refresh("analytics"); notify("Изменения отправлены серверу.", "success");
    }); return;
  }
  if (form.id === "site-form") {
    if (!canManageSite()) { notify("Недостаточно прав", "error"); return; }
    const data = new FormData(form); const zones = String(data.get("zones")).split("\n").map(x => x.trim());
    if (!zones.length || zones.length > 50 || zones.some(x => !x)) { notify("Укажите от 1 до 50 непустых названий зон.", "error"); return; }
    await run(async () => { await api.updateSite({ name: data.get("name"), latMin: Number(data.get("latMin")), latMax: Number(data.get("latMax")), lonMin: Number(data.get("lonMin")), lonMax: Number(data.get("lonMax")), zones }, data.get("planImage")); await refresh("analytics"); notify("Привязка зон сохранена.", "success"); }); return;
  }
  if (form.id === "question-form" || form.id === "summary-form") {
    const data = new FormData(form);
    await run(async () => { state.assistant = form.id === "question-form" ? await api.ask(data.get("question")) : await api.summary(data.get("from"), data.get("to")); });
  }
});

setInterval(async () => {
  if (!initialized || polling || state.busy || !allowedPages().includes(state.page)) return;
  polling = true;
  try {
  if (state.page === "gate" && state.challenge?.id && ["waiting", "confirmed"].includes(state.challenge.status)) {
    try { const next = await api.challenge(state.challenge.id); if (next.status !== state.challenge.status) { state.challenge = { ...state.challenge, ...next, code: next.code || state.challenge.code }; render(); } } catch (error) { state.loadError = errorMessage(error); render(); }
  }
  if (state.page === "worker") {
    try {
      const before = state.worker?.employee || state.employees.find(x => x.id === state.workerId);
      if (config.mode === "demo") state.employees = await api.employees();
      else state.worker = await api.me();
      const after = state.worker?.employee || state.employees.find(x => x.id === state.workerId);
      if (state.locationWatch !== null && before?.shiftId && after?.shiftId !== before.shiftId) {
        stopLocation(); state.locationStatus = "Смена закрыта. Передача координат остановлена.";
      }
      const hadError = Boolean(state.loadError);
      state.loadError = null;
      if (hadError || JSON.stringify(before) !== JSON.stringify(after)) render();
    } catch (error) {
      const message = errorMessage(error);
      if (state.loadError !== message) { state.loadError = message; render(); }
    }
  }
  if (state.page === "overview") await refresh("overview");
  } finally { polling = false; }
}, config.pollingMs);

window.addEventListener("operator-session-expired", () => {
  stopLocation(); clearChallenge(); Object.assign(session, { kind: "guest", roles: [], name: "" });
  state.employees = []; state.attempts = []; state.dashboard = null; state.enrollCode = null; state.assistant = null;
  entryError = "Сессия истекла, войдите заново"; render();
});
window.addEventListener("beforeunload", () => { stopCamera(); stopLocation(); });
render();
try { await initAuth(); } catch (error) { entryError = errorMessage(error); }
initialized = true;
state.page = allowedPages()[0] || "overview";
render(); if (allowedPages().length) await refresh();
