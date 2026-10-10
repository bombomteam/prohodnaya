import test from "node:test";
import assert from "node:assert/strict";
import { allowedPages } from "../src/auth.js";
import { passageBytes, parsePassagePayload } from "../src/device.js";

test("operator permissions do not grant a worker key or controller powers to admin", () => {
  assert.deepEqual(allowedPages("worker", ["admin"]), ["worker"]);
  assert.deepEqual(allowedPages("guest", []), []);
  assert.deepEqual(allowedPages("operator", ["controller"]), ["gate"]);
  assert.ok(!allowedPages("operator", ["admin"]).includes("gate"));
  assert.ok(!allowedPages("operator", ["admin"]).includes("analytics"));
  assert.ok(!allowedPages("operator", ["manager"]).includes("employees"));
  assert.ok(allowedPages("operator", ["admin", "controller"]).includes("gate"));
});

test("device signature binds backend passage session, nonce and direction", async () => {
  const keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  await assert.rejects(crypto.subtle.exportKey("jwk", keys.privateKey));
  const value = { sessionId: "e0ae024b-c511-47d6-903a-f2099d94fa03", nonce: "nonce-1", direction: "in" };
  assert.deepEqual(parsePassagePayload(JSON.stringify({ sid: value.sessionId, n: value.nonce, d: value.direction })), value);
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, keys.privateKey, passageBytes(value));
  const verify = item => crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, keys.publicKey, signature, passageBytes(item));
  assert.equal(await verify(value), true);
  assert.equal(await verify({ ...value, direction: "out" }), false);
  assert.equal(await verify({ ...value, nonce: "nonce-2" }), false);
});
