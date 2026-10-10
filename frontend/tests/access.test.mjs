import test from "node:test";
import assert from "node:assert/strict";
import { allowedPages } from "../src/auth.js";
import { signedBytes } from "../src/device.js";

test("operator permissions do not grant a worker key or controller powers to admin", () => {
  assert.deepEqual(allowedPages("worker", ["admin"]), ["worker"]);
  assert.deepEqual(allowedPages("guest", []), []);
  assert.deepEqual(allowedPages("operator", ["controller"]), ["gate"]);
  assert.ok(!allowedPages("operator", ["admin"]).includes("gate"));
  assert.ok(!allowedPages("operator", ["manager"]).includes("employees"));
  assert.ok(allowedPages("operator", ["admin", "controller"]).includes("gate"));
});

test("device signature binds direction, nonce, endpoint and payload with an unexportable key", async () => {
  const keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  await assert.rejects(crypto.subtle.exportKey("jwk", keys.privateKey));
  const args = ["POST", "/worker/pass/confirm", { direction: "entry" }, "nonce-1", "2026-10-10T00:00:00Z"];
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, keys.privateKey, signedBytes(...args));
  const verify = values => crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, keys.publicKey, signature, signedBytes(...values));
  assert.equal(await verify(args), true);
  assert.equal(await verify([args[0], args[1], { direction: "exit" }, ...args.slice(3)]), false);
  assert.equal(await verify([args[0], "/locations", ...args.slice(2)]), false);
  assert.equal(await verify([...args.slice(0, 3), "nonce-2", args[4]]), false);
});
