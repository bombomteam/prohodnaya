import { config } from "../config.js";
let transport;
let tokens;
let settings;
export function configureAuthTransport(fn) { transport = fn; }
export const session = { kind: "guest", roles: [], name: "" };
export function allowedPages(kind = session.kind, roles = session.roles) {
  if (kind === "worker") return ["worker"];
  const pages = new Set();
  for (const role of roles) {
    if (role === "admin") ["employees", "overview", "analytics", "assistant", "journal"].forEach(p => pages.add(p));
    if (role === "manager") ["overview", "analytics", "assistant", "journal"].forEach(p => pages.add(p));
    if (role === "controller") pages.add("gate");
  }
  return [...pages];
}
export function canEdit() { return session.kind === "operator" && session.roles.includes("admin"); }
export function demoRole(role) {
  if (config.mode !== "demo") throw new Error("Переключение роли доступно только в демо.");
  Object.assign(session, role === "worker" ? { kind: "worker", roles: [], name: "Демо: рабочий" } : { kind: "operator", roles: [role], name: `Демо: ${role}` });
}
export function useWorker() { Object.assign(session, { kind: "worker", roles: [], name: "Рабочий" }); }
function base64url(bytes) { return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,""); }
function redirectUri() { const url = new URL(location.href); url.search = ""; url.hash = ""; return url.href; }
function accept(result) {
  if (!result?.accessToken || !result.user || !Array.isArray(result.user.roles) || !Number.isFinite(Number(result.expiresIn)) || Number(result.expiresIn) <= 0) throw new Error("Сервер не вернул проверенный профиль оператора.");
  tokens = { ...result, expiresAt: Date.now() + Number(result.expiresIn) * 1000 };
  Object.assign(session, {kind:"operator",roles:result.user.roles,name:result.user.preferred_username || "Оператор"});
}
export async function initAuth() {
  if (config.mode === "demo") { demoRole("admin"); return; }
  const url = new URL(location.href);
  if (url.searchParams.get("worker") === "1") { useWorker(); return; }
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
  settings = await transport("/auth/config", {auth:"none"});
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
  tokens = undefined; Object.assign(session,{kind:"guest",roles:[],name:""});
  if (refreshToken) await transport("/auth/logout", {method:"POST",auth:"none",body:{refreshToken}});
}
let refreshing;
export async function accessToken() {
  if (!tokens) throw new Error("Сессия истекла, войдите заново");
  if (Date.now() >= tokens.expiresAt - 30000) {
    refreshing ||= transport("/auth/refresh", {method:"POST",auth:"none",body:{refreshToken:tokens.refreshToken}}).then(accept).catch(() => { tokens=undefined; Object.assign(session,{kind:"guest",roles:[],name:""}); throw new Error("Сессия истекла, войдите заново"); }).finally(() => {refreshing=undefined;});
    await refreshing;
  }
  return tokens.accessToken;
}
