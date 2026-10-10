// Transport proposal device-v1; enable only after agreeing it with the backend.
let deviceProtocol = "";
export function configureDeviceProtocol(value) { deviceProtocol = value; }
const encoder = new TextEncoder();
export function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
export function signedBytes(method, path, body, nonce, timestamp) {
  return encoder.encode(JSON.stringify({ version: 1, method, path, body: body ?? null, nonce, timestamp }));
}
async function store(mode, operation) {
  const db = await new Promise((resolve, reject) => {
    const r = indexedDB.open("prohodnaya-device", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("keys");
    r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error);
  });
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("keys", mode); const request = operation(tx.objectStore("keys"));
      tx.oncomplete = () => resolve(request.result); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error);
    });
  } finally { db.close(); }
}
export function loadDevice() { return store("readonly", s => s.get("current")); }
export function saveDevice(value) { return store("readwrite", s => s.put(value, "current")); }
export function forgetDevice() { return store("readwrite", s => s.delete("current")); }
export function protocolReady() {
  if (deviceProtocol !== "device-v1") throw new Error("Протокол ECDSA ещё не согласован с сервером. Сервер должен подтвердить протокол через /worker/config после сверки контракта.");
  if (!globalThis.crypto?.subtle) throw new Error("Для ключа устройства нужен HTTPS или localhost.");
}
export async function createDevice() {
  protocolReady();
  const keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  return { privateKey: keys.privateKey, publicKey: await crypto.subtle.exportKey("jwk", keys.publicKey) };
}
export async function signRequest(device, method, path, body, nonce) {
  protocolReady();
  const timestamp = new Date().toISOString();
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, device.privateKey, signedBytes(method, path, body, nonce, timestamp));
  return { keyId: device.keyId, nonce, timestamp, signature: base64url(signature) };
}
