// The backend signs passage QR data and location payloads with ECDSA P-256.
const encoder = new TextEncoder();

export function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function secureContextReady() {
  if (!globalThis.crypto?.subtle) throw new Error("Для ключа устройства нужен HTTPS или localhost.");
}

async function store(mode, operation) {
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
  secureContextReady();
  const keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  return { privateKey: keys.privateKey, publicKey: await crypto.subtle.exportKey("jwk", keys.publicKey) };
}

export function parsePassagePayload(value) {
  let payload;
  try { payload = JSON.parse(String(value || "").trim()); } catch { throw new Error("Вставьте QR-данные прохода в формате JSON."); }
  if (!/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(payload?.sid || "") ||
      typeof payload.n !== "string" || !payload.n || !["in", "out"].includes(payload.d)) {
    throw new Error("QR-данные неполные или повреждены. Создайте новый запрос на проходной.");
  }
  return { sessionId: payload.sid, nonce: payload.n, direction: payload.d };
}

export function passageBytes({ sessionId, nonce, direction }) {
  return encoder.encode(`${sessionId}|${nonce}|${direction}`);
}

async function sign(device, bytes) {
  secureContextReady();
  if (!device?.deviceId || !device.privateKey) throw new Error("Сначала зарегистрируйте телефон по коду администратора.");
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, device.privateKey, bytes);
  return base64url(signature); // WebCrypto returns raw r||s (64 bytes) for P-256.
}

export function signPassage(device, payload) { return sign(device, passageBytes(payload)); }
export function signLocations(device, payload) { return sign(device, encoder.encode(payload)); }
