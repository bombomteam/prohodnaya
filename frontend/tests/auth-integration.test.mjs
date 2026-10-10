import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { config } from "../config.js";
import { configureAuthTransport, initAuth, login, session } from "../src/auth.js";
import { createDevice, loadDevice, requireSecureContext } from "../src/device.js";

test("panel realm includes subject and authentication context scopes", () => {
  const realm = JSON.parse(readFileSync(new URL("../../keycloak/import/construction-realm.json", import.meta.url)));
  const panel = realm.clients.find(client => client.clientId === "panel");
  for (const scope of ["basic", "acr", "roles"]) assert.ok(panel.defaultClientScopes.includes(scope));
});

test("PKCE login from index.html uses the origin root for authorization and code exchange", async () => {
  const previous = { location: globalThis.location, history: globalThis.history, sessionStorage: globalThis.sessionStorage,
    isSecureContext: globalThis.isSecureContext, mode: config.mode, session: { ...session } };
  const storage = new Map();
  const calls = [];
  let redirect;
  const origin = "https://app.example.test";
  globalThis.location = { href: origin + "/index.html", origin, assign: url => { redirect = new URL(url); } };
  globalThis.sessionStorage = { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
  globalThis.history = { replaceState(_state, _title, url) { assert.equal(url, origin + "/"); } };
  globalThis.isSecureContext = true;
  config.mode = "api";
  configureAuthTransport(async (path, options) => {
    calls.push({ path, options });
    if (path === "/auth/config") return { authorizationEndpoint: "https://auth.example.test/realms/construction/protocol/openid-connect/auth" };
    return { accessToken: "test-access-token", expiresIn: 300, user: { preferred_username: "Контролёр", roles: ["controller"] } };
  });
  try {
    await login();
    assert.equal(redirect.searchParams.get("redirect_uri"), origin + "/");
    assert.equal(redirect.searchParams.get("code_challenge_method"), "S256");
    assert.equal(redirect.searchParams.get("client_id"), "panel");
    globalThis.location.href = origin + "/?code=test-code&state=" + redirect.searchParams.get("state");
    await initAuth();
    const exchange = calls.find(call => call.path === "/auth/exchange");
    assert.equal(exchange.options.body.redirectUri, origin + "/");
    assert.ok(exchange.options.body.codeVerifier);
    assert.equal(storage.size, 0);
    assert.deepEqual(session.roles, ["controller"]);
  } finally {
    config.mode = previous.mode;
    Object.assign(session, previous.session);
    for (const key of ["location", "history", "sessionStorage", "isSecureContext"]) globalThis[key] = previous[key];
  }
});

test("insecure context gives readable errors before login, key creation or key storage", async () => {
  const previous = globalThis.isSecureContext;
  globalThis.isSecureContext = false;
  try {
    assert.throws(() => requireSecureContext(), /HTTPS/);
    await assert.rejects(login(), /HTTPS/);
    await assert.rejects(createDevice(), /HTTPS/);
    await assert.rejects(loadDevice(), /HTTPS/);
  } finally { globalThis.isSecureContext = previous; }
});

test("backend activation URL enters worker mode without interpreting its code as an OAuth callback", async () => {
  const previous = { location: globalThis.location, mode: config.mode, session: { ...session } };
  config.mode = "api";
  try {
    for (const path of ["/enroll?code=activation-code", "/enroll/?code=activation-code", "/?worker=1&code=activation-code"]) {
      globalThis.location = new URL("https://app.example.test" + path);
      await initAuth();
      assert.equal(session.kind, "worker");
      assert.deepEqual(session.roles, []);
    }
  } finally {
    globalThis.location = previous.location;
    config.mode = previous.mode;
    Object.assign(session, previous.session);
  }
});
