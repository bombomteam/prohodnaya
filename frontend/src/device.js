// Телефон подписывает запрос кода прохода и пакеты координат ECDSA P-256 (неизвлекаемый ключ).
const encoder = new TextEncoder();

export function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function requireSecureContext(message = "Для ключа устройства нужен HTTPS или localhost.") {
  if (globalThis.isSecureContext === false || !globalThis.crypto?.subtle) throw new Error(message);
}

async function store(mode, operation) {
  requireSecureContext();
  if (!globalThis.indexedDB) throw new Error("Браузер не поддерживает хранение ключа устройства. Откройте приложение в современном браузере.");
  const db = await new Promise((resolve, reject) => {
    const request = indexedDB.open("prohodnaya-device", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("keys");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("keys", mode);
      const request = operation(tx.objectStore("keys"));
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally { db.close(); }
}

export function loadDevice() { return store("readonly", objectStore => objectStore.get("current")); }
export function saveDevice(value) { return store("readwrite", objectStore => objectStore.put(value, "current")); }
export function forgetDevice() { return store("readwrite", objectStore => objectStore.delete("current")); }

export async function createDevice() {
  requireSecureContext();
  const keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  return { privateKey: keys.privateKey, publicKey: await crypto.subtle.exportKey("jwk", keys.publicKey) };
}

export function passCodeBytes(deviceId, ts) {
  return encoder.encode(`pass-code|${deviceId}|${ts}`);
}

async function sign(device, bytes) {
  requireSecureContext();
  if (!device?.deviceId || !device.privateKey) throw new Error("Сначала зарегистрируйте телефон по коду администратора.");
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, device.privateKey, bytes);
  return base64url(signature); // WebCrypto returns raw r||s (64 bytes) for P-256.
}

// POST /device/pass-code: подпись строки "pass-code|<device_id>|<ts>" той же парой ключей, что при привязке.
export function signPassCode(device, ts) { return sign(device, passCodeBytes(device.deviceId, ts)); }
export function signLocations(device, payload) { return sign(device, encoder.encode(payload)); }
