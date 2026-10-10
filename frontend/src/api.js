import { config } from "../config.js";
import { demoApi } from "./demo-api.js";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request(path, { method = "GET", body, signal } = {}) {
  const isForm = body instanceof FormData;
  let response;
  try {
    response = await fetch(`${config.apiBaseUrl}${path}`, {
      method,
      credentials: "include",
      headers: isForm || body === undefined ? {} : { "Content-Type": "application/json" },
      body: isForm ? body : body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("Нет связи с сервером. Проверьте подключение и повторите попытку.");
  }
  let data;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    throw new ApiError(data?.message || data?.detail || "Не удалось выполнить действие. Повторите попытку.", response.status);
  }
  return data;
}

const realApi = {
  employees: () => request("/employees"),
  saveEmployee: (id, form) => request(id ? `/employees/${id}` : "/employees", { method: id ? "PATCH" : "POST", body: form }),
  attempts: () => request("/passes/attempts"),
  createChallenge: (direction) => request("/passes/challenges", { method: "POST", body: { direction } }),
  challenge: (id) => request(`/passes/challenges/${encodeURIComponent(id)}`),
  decidePass: (challengeId, direction, image) => {
    const form = new FormData();
    form.append("challengeId", challengeId);
    form.append("direction", direction);
    form.append("faceImage", image, "face.jpg");
    return request("/passes/attempts", { method: "POST", body: form });
  },
  me: () => request("/worker/me"),
  enrollmentOptions: (employeeId) => request("/worker/enrollment/options", { method: "POST", body: { employeeId } }),
  enroll: (employeeId, credential) => request("/worker/enrollment/verify", { method: "POST", body: { employeeId, credential } }),
  passOptions: (code) => request("/worker/pass/options", { method: "POST", body: { code } }),
  confirmPass: (challengeId, assertion) => request("/worker/pass/confirm", { method: "POST", body: { challengeId, assertion } }),
  sendLocation: (shiftId, position) => request(`/shifts/${encodeURIComponent(shiftId)}/locations`, { method: "POST", body: position }),
  dashboard: () => request("/dashboard"),
  analytics: (filters) => request(`/analytics?${new URLSearchParams(filters)}`),
  shiftReport: (filters) => request(`/reports/shifts?${new URLSearchParams(filters)}`),
  site: () => request("/site"),
  updateSite: (site) => request("/site", { method: "PUT", body: site }),
  ask: (question) => request("/assistant/questions", { method: "POST", body: { question } }),
  summary: (from, to) => request("/assistant/summaries", { method: "POST", body: { from, to } }),
};

export const api = config.mode === "demo" ? demoApi : realApi;
