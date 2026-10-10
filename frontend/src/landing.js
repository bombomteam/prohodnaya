// Стартовый экран демо-стенда, переключатель ролей, «Как проверить кейс» и подсказки.
// Чистые функции: возвращают HTML-строки, без обращения к DOM и сети (их вызывает app.js).
// Роль выбирается кнопкой, пароли не нужны: сервер сам выдаёт вход (см. docs/frontend-api.md, «Демо-вход»).

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Четыре карточки стартового экрана. «worker» — не вход, а открытие телефона рабочего.
export const DEMO_ROLES = [
  { id: "admin", icon: "♙", title: "Администратор", text: "Заводит людей и выдаёт код телефона" },
  { id: "controller", icon: "◉", title: "Контролёр", text: "Пропускает на проходной" },
  { id: "manager", icon: "▦", title: "Руководитель", text: "Кто на объекте, отчёты, помощник" },
  { id: "worker", icon: "▣", title: "Телефон рабочего", text: "Привязка и код прохода" },
];
export const roleTitle = id => DEMO_ROLES.find(role => role.id === id)?.title || "";

// «Что здесь проверить»: одна короткая строка на экран роли.
export const PAGE_HINTS = {
  overview: "Кто сейчас на объекте и последние проходы. После пропуска человек появляется здесь.",
  employees: "Добавьте человека с фото и нажмите «Enroll-код» в его строке: появятся код и ссылка для телефона.",
  gate: "Выберите человека, введите 6 цифр с его телефона и снимите лицо. Своё лицо — «Проход разрешён», чужое — отказ.",
  worker: "Привяжите телефон по коду администратора, потом на проходной нажмите «Я на проходной — получить код».",
  analytics: "Маршруты и посещаемость трёх зон за день; «Табель сотрудника» даёт отчёт по человеку.",
  assistant: "Задайте вопрос, например «Кто сейчас на объекте?», и переключите модель: Локальная или Внешняя.",
  journal: "Все попытки прохода, в том числе отказы, с понятной причиной.",
};
export const pageHint = page => PAGE_HINTS[page] || "";

// Шаги проверки. role + page — куда перейти кнопкой; для рабочего вместо кнопки ссылка в новой вкладке.
export const GUIDE_STEPS = [
  { title: "Администратор заводит человека", role: "admin", page: "employees", go: "Открыть экран администратора",
    text: "Нажмите «Добавить сотрудника», впишите ФИО и загрузите фото лица (один человек в кадре). Потом в строке человека нажмите «Enroll-код»: появятся код и ссылка для телефона." },
  { title: "Рабочий привязывает телефон", role: "worker", go: "Открыть телефон рабочего",
    text: "На телефоне рабочего откройте ссылку из первого шага или впишите код в поле «Enroll-код» и нажмите «Привязать устройство». Должна появиться надпись «Устройство привязано». Логин и пароль не нужны." },
  { title: "Контролёр пропускает человека", role: "controller", page: "gate", go: "Открыть проходную",
    text: "Выберите человека и нажмите «Отправить запрос работнику». Рабочий на своём телефоне жмёт «Я на проходной — получить код» и называет 6 цифр. Введите их, включите камеру, снимите лицо и нажмите «Проверить лицо и получить решение». Ждём «Проход разрешён»." },
  { title: "Чужое лицо не проходит", role: "controller", page: "gate", go: "Повторить на проходной",
    text: "Повторите то же самое, но перед камерой пусть будет другой человек. Ждём «Отказ в проходе: лицо не совпадает». Отказ тоже записывается в журнал." },
  { title: "Руководитель смотрит картину", role: "manager", page: "overview", go: "Кто на объекте",
    text: "Здесь видно, кто сейчас на объекте и последние проходы. Журнал показывает все попытки, в том числе отказы.",
    more: [{ role: "manager", page: "journal", go: "Журнал попыток" }, { role: "manager", page: "analytics", go: "Маршруты и отчёт" }],
    extra: "В «Аналитике» есть маршруты по трём зонам, посещаемость зон и «Табель сотрудника» с кнопкой «Получить отчёт»." },
  { title: "Помощник отвечает на вопросы", role: "manager", page: "assistant", go: "Открыть помощника",
    text: "Задайте вопрос, например «Кто сейчас на объекте?» или «Сводка за сегодня». Справа переключатель модели: Локальная или Внешняя. Числа считает система, модель только пишет текст." },
  { title: "После перезапуска данные на месте", role: "admin", page: "employees", go: "Проверить список людей",
    text: "Попросите организаторов перезапустить стенд и обновите страницу. Люди, привязанные телефоны и журнал остаются." },
];

function stepAction(step, extraClass = "") {
  if (step.role === "worker") return `<a class="button button-outline guide-go ${extraClass}" href="?worker=1" target="_blank" rel="noopener noreferrer">${esc(step.go)} ↗</a>`;
  return `<button class="button button-outline guide-go ${extraClass}" data-action="guide-go" data-role="${esc(step.role)}" data-go="${esc(step.page)}">${esc(step.go)} →</button>`;
}

// Пронумерованные шаги с кнопками-переходами. Одинаково работает на стартовом экране и внутри приложения.
export function guideHtml() {
  return `<section class="guide-panel panel" aria-label="Как проверить кейс"><div class="guide-head"><h2>Как проверить кейс</h2><p>Семь шагов, около десяти минут. Пароли и регистрация не нужны: роль выбирается кнопкой.</p></div>
    <ol class="guide-steps">${GUIDE_STEPS.map((step, index) => `<li class="guide-step"><span class="guide-num">${index + 1}</span><div class="guide-body"><b>${esc(step.title)}</b><p>${esc(step.text)}</p>${step.extra ? `<p>${esc(step.extra)}</p>` : ""}<div class="guide-actions">${stepAction(step)}${(step.more || []).map(item => stepAction(item)).join("")}</div></div></li>`).join("")}</ol></section>`;
}

// Стартовый экран: вместо стены входа, когда сервер разрешил демо-вход.
export function landingHtml({ busyRole = "", error = "", guideOpen = false } = {}) {
  const cards = DEMO_ROLES.map(role => {
    const body = `<span class="landing-card-icon" aria-hidden="true">${role.icon}</span><span class="landing-card-text"><b>${esc(role.title)}</b><small>${esc(role.text)}</small></span><span class="landing-card-go" aria-hidden="true">${busyRole === role.id ? "…" : "→"}</span>`;
    return role.id === "worker"
      ? `<a class="landing-card" href="?worker=1">${body}</a>`
      : `<button class="landing-card" data-action="demo-login" data-role="${role.id}" ${busyRole ? "disabled" : ""}>${body}</button>`;
  }).join("");
  return `<main class="landing-page">
    <header class="landing-hero"><div class="landing-brand"><span class="brand-mark">П</span><strong>ПРОХОДНАЯ</strong></div>
      <h1>Проходная — демо-стенд</h1>
      <p>Пропуск на стройплощадку по телефону и лицу: кто на объекте, маршруты, отчёты и помощник. Выберите роль, пароль не нужен.</p></header>
    ${error ? `<div class="error-banner" role="alert"><span>${esc(error)}</span></div>` : ""}
    <div class="landing-cards">${cards}</div>
    <div class="landing-extra"><button class="button button-outline" data-action="toggle-guide" aria-expanded="${guideOpen}">${guideOpen ? "Скрыть «Как проверить кейс»" : "Как проверить кейс"}</button><button class="landing-link" data-action="login">Войти через Keycloak</button></div>
    ${guideOpen ? guideHtml() : ""}
  </main>`;
}

// Полоса над содержимым экрана роли: смена роли без выхода и подсказка «Что здесь проверить».
export function demoBarHtml({ current = "", page = "", busy = false } = {}) {
  const roles = DEMO_ROLES.map(role => {
    if (role.id === "worker") {
      return current === "worker" ? `<span class="guide-role selected" aria-current="true">${esc(role.title)}</span>`
        : `<a class="guide-role" href="?worker=1" target="_blank" rel="noopener noreferrer">${esc(role.title)} ↗</a>`;
    }
    return `<button class="guide-role ${current === role.id ? "selected" : ""}" data-action="demo-switch" data-role="${role.id}" ${busy ? "disabled" : ""}>${esc(role.title)}</button>`;
  }).join("");
  const hint = pageHint(page);
  return `<div class="guide-bar"><div class="guide-bar-row"><span class="guide-bar-label">Роль</span><div class="guide-roles" role="group" aria-label="Сменить роль">${roles}</div></div>${hint ? `<p class="guide-hint"><b>Что здесь проверить:</b> ${esc(hint)}</p>` : ""}</div>`;
}
