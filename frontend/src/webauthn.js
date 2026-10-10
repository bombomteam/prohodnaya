function fromBase64Url(value) {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const bytes = atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "="));
  return Uint8Array.from(bytes, c => c.charCodeAt(0));
}

function toBase64Url(buffer) {
  const bytes = new Uint8Array(buffer);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
}

export async function createCredential(options) {
  if (!window.PublicKeyCredential) throw new Error("Этот браузер не поддерживает подтверждение устройства WebAuthn.");
  const publicKey = { ...options, challenge: fromBase64Url(options.challenge), user: { ...options.user, id: fromBase64Url(options.user.id) } };
  if (options.excludeCredentials) publicKey.excludeCredentials = options.excludeCredentials.map(x => ({ ...x, id: fromBase64Url(x.id) }));
  const credential = await navigator.credentials.create({ publicKey });
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toBase64Url(credential.response.clientDataJSON),
      attestationObject: toBase64Url(credential.response.attestationObject),
    },
  };
}

export async function getAssertion(options) {
  if (!window.PublicKeyCredential) throw new Error("Этот браузер не поддерживает подтверждение устройства WebAuthn.");
  const publicKey = { ...options, challenge: fromBase64Url(options.challenge) };
  if (options.allowCredentials) publicKey.allowCredentials = options.allowCredentials.map(x => ({ ...x, id: fromBase64Url(x.id) }));
  const credential = await navigator.credentials.get({ publicKey });
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toBase64Url(credential.response.clientDataJSON),
      authenticatorData: toBase64Url(credential.response.authenticatorData),
      signature: toBase64Url(credential.response.signature),
      userHandle: credential.response.userHandle ? toBase64Url(credential.response.userHandle) : null,
    },
  };
}
