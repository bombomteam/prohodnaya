import { config } from "../config.js";
import { accessToken, configureAuthTransport } from "./auth.js";
import { loadDevice, saveDevice, createDevice, signRequest, protocolReady, configureDeviceProtocol } from "./device.js";
import { demoApi } from "./demo-api.js";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function request(path, { method = "GET", body, signal, auth = "operator", headers = {} } = {}) {
  if (auth === "operator") headers = { ...headers, Authorization: `Bearer ${await accessToken()}` };
  const timeout = AbortSignal.timeout(config.requestTimeoutMs || 15000);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  const isForm = body instanceof FormData;
  let response;
  try {
    response = await fetch(`${config.apiBaseUrl}${path}`, {
      method,
      credentials: "omit",
      headers: { ...(isForm || body === undefined ? {} : { "Content-Type": "application/json" }), ...headers },
      body: isForm ? body : body === undefined ? undefined : JSON.stringify(body),
      signal: combined,
    });
  } catch (error) {
    if (error.name === "AbortError" || error.name === "TimeoutError") throw new ApiError("Время ожидания истекло. Проверьте результат попытки перед повтором.");
    throw new ApiError("Нет связи с сервером. Проверьте подключение и повторите попытку.");
  }
  let data;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401 && auth === "operator" && typeof window !== "undefined") window.dispatchEvent(new Event("operator-session-expired"));
    throw new ApiError(response.status === 401 ? "Сессия истекла, войдите заново" : response.status === 403 ? "Недостаточно прав" : (typeof data?.message === "string" ? data.message : typeof data?.detail === "string" ? data.detail : "Не удалось выполнить действие. Повторите попытку."), response.status);
  }
  return data;
}

const realApi = {
  employees: () => request("/employees"),
  saveEmployee: (id, form) => request(id ? `/employees/${id}` : "/employees", { method: id ? "PATCH" : "POST", body: form }),
  attempts: () => request("/passes/attempts"),
  createChallenge: (direction) => request("/passes/challenges", { method: "POST", body: { direction } }),
  challenge: (id) => request(`/passes/challenges/${encodeURIComponent(id)}`),
  decidePass: (challengeId, direction, image, requestId = crypto.randomUUID()) => {
    const form = new FormData();
    form.append("challengeId", challengeId);
    form.append("direction", direction);
    form.append("faceImage", image, "face.jpg");
    return request("/passes/attempts", { method: "POST", body: form, headers: { "Idempotency-Key": requestId } });
  },
  me: async () => { const device = await loadDevice(); return device?.keyId ? workerRequest("/worker/me") : { employee: null }; },
  enroll: async (code) => {
    await loadDeviceProtocol();
    protocolReady();
    let device = await loadDevice();
    if (device?.keyId) throw new Error("Устройство уже привязано. Для замены обратитесь к администратору.");
    if (!device) { device = await createDevice(); await saveDevice(device); }
    const options = await request("/enroll/options", { method: "POST", body: { code, publicKey: device.publicKey }, auth: "none" });
    if (typeof options?.nonce !== "string" || !options.nonce) throw new ApiError("Сервер не вернул nonce регистрации.");
    const body = { code, publicKey: device.publicKey };
    const proof = await signRequest(device, "POST", "/enroll", body, options.nonce);
    const result = await request("/enroll", { method: "POST", body: { ...body, proof }, auth: "none" });
    if (typeof result?.keyId !== "string" || !result.keyId) throw new ApiError("Сервер не вернул идентификатор ключа. Повторите регистрацию с тем же устройством.");
    await saveDevice({ ...device, keyId: result.keyId });
    return result;
  },
  passOptions: (code) => workerRequest("/worker/pass/options", "POST", { code }),
  confirmPass: (challengeId, options) => workerRequest("/worker/pass/confirm", "POST", { challengeId, direction: options.direction }, options.nonce),
  sendLocation: (shiftId, position) => workerRequest("/locations", "POST", { shiftId, batchId: crypto.randomUUID(), source: "phone", points: [position] }),
  reportPeople: () => request("/reports/people"),
  enrollmentCode: (id) => request(`/employees/${encodeURIComponent(id)}/enroll-code`, { method: "POST" }),
  revokeDevice: (id) => request(`/employees/${encodeURIComponent(id)}/device`, { method: "DELETE" }),
  deactivate: (id) => request(`/employees/${encodeURIComponent(id)}/deactivate`, { method: "POST" }),
  dashboard: () => request("/dashboard"),
  analytics: (filters) => request(`/analytics?${new URLSearchParams(filters)}`),
  shiftReport: (filters) => request(`/reports/shifts?${new URLSearchParams(filters)}`),
  site: () => request("/site"),
  updateSite: (site, image) => {
    if (!image?.size) return request("/site", { method: "PUT", body: site });
    const form = new FormData(); form.append("metadata", JSON.stringify(site)); form.append("planImage", image);
    return request("/site", { method: "PUT", body: form });
  },
  ask: (question) => request("/assistant/questions", { method: "POST", body: { question } }),
  summary: (from, to) => request("/assistant/summaries", { method: "POST", body: { from, to } }),
};

export const api = config.mode === "demo" ? demoApi : realApi;

async function workerRequest(path, method = "GET", body, nonce) {
  await loadDeviceProtocol();
  protocolReady();
  const device = await loadDevice();
  if (!device?.keyId) throw new Error("Сначала зарегистрируйте телефон по enroll-коду.");
  if (!nonce) {
    const challenge = await request("/worker/nonce", { method: "POST", body: { keyId: device.keyId, method, path }, auth: "none" });
    nonce = challenge.nonce;
  }
  if (typeof nonce !== "string" || !nonce) throw new ApiError("Сервер не вернул nonce для подписи.");
  const proof = await signRequest(device, method, path, body, nonce);
  return request(path, { method, body, auth: "none", headers: { "X-Device-Proof": JSON.stringify(proof) } });
}

configureAuthTransport(request);

let protocolPromise;
function loadDeviceProtocol() {
  protocolPromise ||= request("/worker/config", {auth:"none"}).then(value => { configureDeviceProtocol(value.deviceProtocol); }).catch(error => {protocolPromise=undefined; throw error;});
  return protocolPromise;
}
