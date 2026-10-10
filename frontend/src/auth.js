import { config } from "../config.js";
import { requireSecureContext } from "./device.js";
import { roleTitle } from "./landing.js";
let transport;
let tokens;
let settings;
let demoEnabled = false;
const DEMO_ROLE_KEY = "demo-role";
export function configureAuthTransport(fn) { transport = fn; }
export const session = { kind: "guest", roles: [], name: "", dev: false, demo: false };
export function allowedPages(kind = session.kind, roles = session.roles) {
  if (kind === "worker") return demoEnabled ? ["worker", "guide"] : ["worker"];
  if (kind !== "operator") return [];
  const pages = new Set();
  for (const role of roles) {
    if (role === "admin") ["employees", "overview", "journal"].forEach(p => pages.add(p));
    if (role === "manager") ["overview", "analytics", "assistant", "journal"].forEach(p => pages.add(p));
    if (role === "controller") pages.add("gate");
  }
  if (demoEnabled && session.demo && pages.size) pages.add("guide");
  return [...pages];
}
export function canEdit() { return session.kind === "operator" && session.roles.includes("admin"); }
export function canManageSite() { return session.kind === "operator" && session.roles.includes("manager"); }
export function demoRole(role) {
  if (config.mode !== "demo") throw new Error("Переключение роли доступно только в демо.");
  Object.assign(session, role === "worker" ? { kind: "worker", roles: [], name: "Демо: рабочий", dev: false } : { kind: "operator", roles: [role], name: `Демо: ${role}`, dev: false });
}
export function useWorker() { Object.assign(session, { kind: "worker", roles: [], name: "Рабочий", dev: false }); }
function base64url(bytes) { return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,""); }
function redirectUri() { return location.origin + "/"; }
function accept(result) {
  if (!result?.accessToken || !result.user || !Array.isArray(result.user.roles) || !Number.isFinite(Number(result.expiresIn)) || Number(result.expiresIn) <= 0) throw new Error("Сервер не вернул проверенный профиль оператора.");
  tokens = { ...result, expiresAt: Date.now() + Number(result.expiresIn) * 1000 };
  Object.assign(session, {kind:"operator",roles:result.user.roles,name:session.demo ? session.name : result.user.preferred_username || "Оператор"});
}
// Демо-стенд: сервер сам сообщает, можно ли войти кнопкой роли (GET /auth/config → demoLogin). Нет маршрута или ошибка — прежний вход.
export function demoLoginEnabled() { return demoEnabled; }
export async function loadAuthConfig() {
  demoEnabled = false;
  if (config.mode !== "api") return { demoLogin: false };
  try {
    const value = await transport("/auth/config", { auth: "none", timeoutMs: 6000 });
    demoEnabled = value?.demoLogin === true;
  } catch { demoEnabled = false; }
  return { demoLogin: demoEnabled };
}
export function savedDemoRole() {
  try { const role = sessionStorage.getItem(DEMO_ROLE_KEY); return ["admin", "manager", "controller"].includes(role) ? role : ""; } catch { return ""; }
}
function rememberDemoRole(role) { try { if (role) sessionStorage.setItem(DEMO_ROLE_KEY, role); else sessionStorage.removeItem(DEMO_ROLE_KEY); } catch { /* память вкладки недоступна: после перезагрузки выберите роль снова */ } }
// Вход кнопкой роли: ответ тот же, что у /auth/exchange, поэтому токены сохраняются тем же accept().
export async function demoLogin(role) {
  if (!["admin", "manager", "controller"].includes(role)) throw new Error("Неизвестная роль.");
  let result;
  try { result = await transport("/auth/demo", { method: "POST", auth: "none", body: { role } }); }
  catch (error) { if (error.status === 404) throw new Error("На этом сервере вход по кнопке роли выключен."); throw error; }
  const wasDemo = session.demo;
  session.demo = role; // выбранная демо-роль: ею подсвечивается переключатель, даже если сервер вернул больше ролей
  try { accept(result); } catch (error) { session.demo = wasDemo; throw error; }
  session.name = `Демо: ${roleTitle(role)}`;
  rememberDemoRole(role);
}
export async function initAuth() {
  const url = new URL(location.href);
  const workerEntry = url.searchParams.get("worker") === "1" || ["/enroll", "/enroll/"].includes(url.pathname);
  if (config.mode === "demo") { demoRole(workerEntry ? "worker" : "admin"); return; }
  if (workerEntry) { useWorker(); return; }
  const devRole = url.searchParams.get("devRole");
  if (devRole) {
    if (!["localhost", "127.0.0.1"].includes(url.hostname) || !["admin", "manager", "controller"].includes(devRole)) {
      throw new Error("Локальный тестовый вход доступен только на localhost с ролью admin, manager или controller.");
    }
    Object.assign(session, { kind: "operator", roles: [devRole], name: `Локальный тест: ${devRole}`, dev: true });
    return;
  }
  if (!url.searchParams.has("code") && !url.searchParams.has("error")) return;
  const saved = JSON.parse(sessionStorage.getItem("operator-pkce") || "null");
  sessionStorage.removeItem("operator-pkce");
  const code = url.searchParams.get("code"), state = url.searchParams.get("state"), error = url.searchParams.get("error");
  history.replaceState(null, "", redirectUri());
  if (!saved || state !== saved.state || Date.now() - saved.createdAt > 600000) throw new Error("Запрос входа устарел. Начните вход заново.");
  if (error || !code) throw new Error("Вход отменён. Повторите попытку.");
  accept(await transport("/auth/exchange", {method:"POST",auth:"none",body:{code,codeVerifier:saved.verifier,redirectUri:saved.redirectUri}}));
}
export async function login() {
  requireSecureContext("Для входа откройте приложение по HTTPS или на localhost.");
  try { settings = await transport("/auth/config", {auth:"none"}); }
  catch (error) { if (error.status === 404) throw new Error("В текущем бэкенде нет маршрутов /auth/*. Операторский вход ещё не подключён."); throw error; }
  const endpoint = new URL(settings.authorizationEndpoint);
  if (endpoint.protocol !== "https:" && !["localhost","127.0.0.1"].includes(endpoint.hostname)) throw new Error("Для входа нужен HTTPS Keycloak.");
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const state = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const uri = redirectUri();
  sessionStorage.setItem("operator-pkce", JSON.stringify({verifier,state,redirectUri:uri,createdAt:Date.now()}));
  const challenge = base64url(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(verifier)));
  for (const [key,value] of Object.entries({client_id:"panel",response_type:"code",scope:"openid",redirect_uri:uri,state,code_challenge:challenge,code_challenge_method:"S256"})) endpoint.searchParams.set(key,value);
  location.assign(endpoint.href);
}
export async function logout() {
  const refreshToken = tokens?.refreshToken;
  tokens = undefined; Object.assign(session,{kind:"guest",roles:[],name:"",dev:false,demo:false}); rememberDemoRole("");
  if (refreshToken) await transport("/auth/logout", {method:"POST",auth:"none",body:{refreshToken}});
}
let refreshing;
export async function accessToken() {
  if (session.dev && ["localhost", "127.0.0.1"].includes(new URL(location.href).hostname)) return null;
  if (!tokens) throw new Error("Сессия истекла, войдите заново");
  if (Date.now() >= tokens.expiresAt - 30000) {
    refreshing ||= transport("/auth/refresh", {method:"POST",auth:"none",body:{refreshToken:tokens.refreshToken}}).then(accept).catch(async () => {
      const role = typeof session.demo === "string" ? session.demo : "";
      if (role) { try { await demoLogin(role); return; } catch { /* вход по кнопке не удался: ниже обычный выход */ } }
      tokens=undefined; Object.assign(session,{kind:"guest",roles:[],name:"",demo:false}); throw new Error("Сессия истекла, войдите заново");
    }).finally(() => {refreshing=undefined;});
    await refreshing;
  }
  return tokens.accessToken;
}
