import test from "node:test";
import assert from "node:assert/strict";
import { allowedPages } from "../src/auth.js";
import { passCodeBytes, signPassCode } from "../src/device.js";

test("operator permissions do not grant a worker key or controller powers to admin", () => {
  assert.deepEqual(allowedPages("worker", ["admin"]), ["worker"]);
  assert.deepEqual(allowedPages("guest", []), []);
  assert.deepEqual(allowedPages("operator", ["controller"]), ["gate"]);
  assert.ok(!allowedPages("operator", ["admin"]).includes("gate"));
  assert.ok(!allowedPages("operator", ["admin"]).includes("analytics"));
  assert.ok(!allowedPages("operator", ["manager"]).includes("employees"));
  assert.ok(allowedPages("operator", ["admin", "controller"]).includes("gate"));
});

test("device signature binds pass-code request to the device id and timestamp", async () => {
  const keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  await assert.rejects(crypto.subtle.exportKey("jwk", keys.privateKey));
  const device = { deviceId: "7f1d2c9e-5a64-4b0e-9c1b-0d9e8a7b6c5d", privateKey: keys.privateKey };
  const ts = "2026-10-10T12:00:00.000Z";
  const raw = Uint8Array.from(atob((await signPassCode(device, ts)).replace(/-/g, "+").replace(/_/g, "/")), char => char.charCodeAt(0));
  assert.equal(raw.length, 64);
  const verify = bytes => crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, keys.publicKey, raw, bytes);
  assert.equal(await verify(passCodeBytes(device.deviceId, ts)), true);
  assert.equal(await verify(passCodeBytes(device.deviceId, "2026-10-10T12:00:01.000Z")), false);
  assert.equal(await verify(passCodeBytes("00000000-0000-4000-8000-000000000000", ts)), false);
});
