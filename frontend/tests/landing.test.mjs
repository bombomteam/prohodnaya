import test from "node:test";
import assert from "node:assert/strict";
import { config } from "../config.js";
import { accessToken, allowedPages, configureAuthTransport, demoLogin, demoLoginEnabled, loadAuthConfig, logout, savedDemoRole, session } from "../src/auth.js";
import { DEMO_ROLES, GUIDE_STEPS, PAGE_HINTS, demoBarHtml, guideHtml, landingHtml } from "../src/landing.js";

const stripTags = html => html.replace(/<[^>]*>/g, " ");

async function withApiMode(transport, body) {
  const previous = { mode: config.mode, session: { ...session }, sessionStorage: globalThis.sessionStorage };
  const storage = new Map();
  globalThis.sessionStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
  config.mode = "api";
  configureAuthTransport(transport);
  try { await body(storage); }
  finally {
    await logout().catch(() => {});
    config.mode = "demo";
    await loadAuthConfig(); // вне режима api демо-вход выключается
    config.mode = previous.mode;
    Object.assign(session, previous.session);
    globalThis.sessionStorage = previous.sessionStorage;
  }
}

test("стартовый экран: заголовок, четыре роли, ссылка на Keycloak", () => {
  const html = landingHtml();
  assert.match(html, /Проходная — демо-стенд/);
  for (const title of ["Администратор", "Контролёр", "Руководитель", "Телефон рабочего"]) assert.ok(html.includes(title), title);
  assert.equal(DEMO_ROLES.length, 4);
  for (const role of ["admin", "controller", "manager"]) assert.match(html, new RegExp(`data-action="demo-login" data-role="${role}"`));
  // Телефон рабочего открывается без входа: обычная ссылка, а не запрос на вход.
  assert.match(html, /<a class="landing-card" href="\?worker=1">/);
  assert.match(html, /data-action="login"[^>]*>Войти через Keycloak</);
  assert.ok(!html.includes("guide-steps"), "руководство свёрнуто, пока его не открыли");
});

test("стартовый экран: ошибка входа и блокировка карточек во время входа", () => {
  const html = landingHtml({ busyRole: "admin", error: "Вход не удался <b>" });
  assert.match(html, /role="alert"/);
  assert.ok(html.includes("Вход не удался &lt;b&gt;"), "текст ошибки экранируется");
  assert.equal((html.match(/data-action="demo-login"[^>]*disabled/g) || []).length, 3);
});

test("«Как проверить кейс»: семь пронумерованных шагов с переходами и без жаргона", () => {
  const html = landingHtml({ guideOpen: true });
  assert.equal(GUIDE_STEPS.length, 7);
  assert.equal((html.match(/class="guide-step"/g) || []).length, 7);
  assert.match(html, /<span class="guide-num">7<\/span>/);
  assert.match(html, /data-action="guide-go" data-role="admin" data-go="employees"/);
  assert.match(html, /data-action="guide-go" data-role="controller" data-go="gate"/);
  assert.match(html, /data-action="guide-go" data-role="manager" data-go="assistant"/);
  assert.match(html, /<a class="button button-outline guide-go " href="\?worker=1" target="_blank" rel="noopener noreferrer">/);
  const text = stripTags(html + guideHtml() + demoBarHtml({ current: "admin", page: "gate" }) + Object.values(PAGE_HINTS).join(" "));
  for (const word of [/\bAPI\b/i, /токен/i, /\bJWT\b/i]) assert.doesNotMatch(text, word);
  assert.match(text, /лицо не совпадает/);
  assert.match(text, /Локальная или Внешняя/);
});

test("переключатель ролей и подсказка «Что здесь проверить»", () => {
  const bar = demoBarHtml({ current: "controller", page: "gate" });
  assert.match(bar, /class="guide-role selected" data-action="demo-switch" data-role="controller"/);
  assert.match(bar, /data-action="demo-switch" data-role="admin"/);
  assert.match(bar, /href="\?worker=1" target="_blank"/);
  assert.match(bar, /<p class="guide-hint"><b>Что здесь проверить:<\/b>/);
  assert.doesNotMatch(demoBarHtml({ current: "worker", page: "worker" }), /data-role="worker"/);
  assert.match(demoBarHtml({ current: "worker", page: "worker" }), /class="guide-role selected"/);
  // Подсказка есть на каждом экране роли, на экране руководства она не нужна.
  for (const page of ["overview", "employees", "gate", "worker", "analytics", "assistant", "journal"]) assert.ok(PAGE_HINTS[page], page);
  assert.doesNotMatch(demoBarHtml({ current: "admin", page: "guide" }), /guide-hint/);
});

test("demoLogin=true в /auth/config включает стартовый экран, 404 и ошибка оставляют прежний вход", async () => {
  await withApiMode(async path => ({ authorizationEndpoint: "https://auth.example.test/auth", demoLogin: true }), async () => {
    assert.deepEqual(await loadAuthConfig(), { demoLogin: true });
    assert.equal(demoLoginEnabled(), true);
  });
  await withApiMode(async () => ({ authorizationEndpoint: "https://auth.example.test/auth", demoLogin: false }), async () => {
    assert.equal((await loadAuthConfig()).demoLogin, false);
  });
  await withApiMode(async () => ({ authorizationEndpoint: "https://auth.example.test/auth" }), async () => {
    assert.equal((await loadAuthConfig()).demoLogin, false);
  });
  await withApiMode(async () => { throw Object.assign(new Error("нет маршрута"), { status: 404 }); }, async () => {
    assert.equal((await loadAuthConfig()).demoLogin, false);
    assert.equal(demoLoginEnabled(), false);
  });
  const previous = config.mode;
  config.mode = "demo";
  try { assert.equal((await loadAuthConfig()).demoLogin, false); } finally { config.mode = previous; }
});

test("вход по кнопке роли: POST /auth/demo, токены как после обмена кода, экран роли открыт", async () => {
  const calls = [];
  const transport = async (path, options = {}) => {
    calls.push({ path, options });
    if (path === "/auth/config") return { authorizationEndpoint: "https://auth.example.test/auth", demoLogin: true };
    if (path === "/auth/demo") return { accessToken: `token-${options.body.role}`, refreshToken: "refresh", expiresIn: 300, user: { preferred_username: "demo", roles: [options.body.role] } };
    return {};
  };
  await withApiMode(transport, async storage => {
    await loadAuthConfig();
    assert.deepEqual(allowedPages("operator", ["controller"]), ["gate"], "без входа руководство в меню не показывается");
    await demoLogin("controller");
    const call = calls.find(item => item.path === "/auth/demo");
    assert.equal(call.options.method, "POST");
    assert.equal(call.options.auth, "none");
    assert.deepEqual(call.options.body, { role: "controller" });
    assert.equal(session.kind, "operator");
    assert.deepEqual(session.roles, ["controller"]);
    assert.equal(session.demo, "controller");
    assert.match(session.name, /Контролёр/);
    assert.equal(await accessToken(), "token-controller");
    assert.deepEqual(allowedPages(), ["gate", "guide"]);
    assert.equal(savedDemoRole(), "controller", "роль помнится в этой вкладке на случай перезагрузки");

    // Смена роли без выхода: тот же запрос, новая роль и новые токены.
    await demoLogin("manager");
    assert.deepEqual(session.roles, ["manager"]);
    assert.equal(await accessToken(), "token-manager");
    assert.deepEqual(allowedPages(), ["overview", "analytics", "assistant", "journal", "guide"]);

    await logout();
    assert.equal(session.kind, "guest");
    assert.equal(session.demo, false);
    assert.equal(savedDemoRole(), "");
    assert.equal(storage.size, 0);
    await assert.rejects(demoLogin("root"), /Неизвестная роль/);
  });
});

test("вход по кнопке: понятные ошибки, сессия не создаётся", async () => {
  await withApiMode(async path => {
    if (path === "/auth/demo") throw Object.assign(new Error("Не найдено"), { status: 404 });
    return { demoLogin: true };
  }, async () => {
    await assert.rejects(demoLogin("admin"), /выключен/);
    assert.equal(session.kind, "guest");
  });
  await withApiMode(async path => path === "/auth/demo" ? { user: { roles: ["admin"] } } : { demoLogin: true }, async () => {
    await assert.rejects(demoLogin("admin"), /проверенный профиль/);
    assert.equal(session.kind, "guest");
    assert.equal(session.demo, false);
  });
});
