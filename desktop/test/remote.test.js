"use strict";
// Mode distant : l'adresse du serveur, ce que la fenêtre a le droit de charger, la lecture de la session.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const remote = require("../lib/remote");

const ORIGIN = "https://trade.exemple.org";
const answer = (status, text) => async () => ({ status, text });

test("l'adresse saisie est ramenée à une origine HTTPS", () => {
  assert.equal(remote.parseAddress("https://trade.exemple.org"), ORIGIN);
  assert.equal(remote.parseAddress("  trade.exemple.org  "), ORIGIN);
  assert.equal(remote.parseAddress("https://trade.exemple.org/"), ORIGIN);
  assert.equal(remote.parseAddress("HTTPS://Trade.Exemple.ORG"), ORIGIN);
  assert.equal(remote.parseAddress("https://trade.exemple.org:8443"), "https://trade.exemple.org:8443");
});

test("une adresse dangereuse ou incomplète est refusée avec un message qui dit quoi corriger", () => {
  const refused = [
    ["", /Saisis l'adresse/], [null, /Saisis l'adresse/],
    ["http://trade.exemple.org", /https/], ["ftp://trade.exemple.org", /https/], ["file:///C:/x", /https/],
    ["javascript://trade.exemple.org", /https/],
    ["https://pierre:secret@trade.exemple.org", /ni identifiant ni mot de passe/],
    ["https://trade.exemple.org/p/board/", /sans chemin/], ["https://trade.exemple.org/?x=1", /sans chemin/],
    ["https://trade.exemple.org/#x", /sans chemin/],
    ["https://serveur", /nom complet/], ["https://", /pas valide/], ["https://exa mple.org", /pas valide/],
  ];
  for (const [text, message] of refused) assert.throws(() => remote.parseAddress(text), message, String(text));
  assert.throws(() => remote.parseAddress("https://pierre:secret@trade.exemple.org"), (err) => !err.message.includes("secret"));
});

test("le réglage se relit tel qu'il a été écrit, et un fichier abîmé ne choisit rien", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tradeagent-remote-"));
  const file = path.join(dir, "data", "desktop.json");
  assert.deepEqual(remote.readConfig(file), {});
  assert.deepEqual(remote.writeConfig(file, { remote: "trade.exemple.org" }), { remote: ORIGIN });
  assert.deepEqual(remote.readConfig(file), { remote: ORIGIN });
  assert.deepEqual(remote.writeConfig(file, { mode: "local" }), { mode: "local" });
  assert.deepEqual(remote.readConfig(file), { mode: "local" });
  assert.throws(() => remote.writeConfig(file, { remote: "http://trade.exemple.org" }), /https/);
  assert.deepEqual(remote.readConfig(file), { mode: "local" });          // un refus n'écrit rien
  for (const content of ["pas du json", "[]", "null", '{"remote": 3}', '{"remote": "http://x.y"}', '{"remote": "https://a.b/chemin"}', '{"mode": "autre"}']) {
    fs.writeFileSync(file, content, "utf8");
    assert.deepEqual(remote.readConfig(file), {}, content);
  }
  fs.rmSync(dir, { recursive: true, force: true });
});

test("une page du serveur ne peut ni en sortir ni charger autre chose que lui", () => {
  for (const url of [ORIGIN + "/", ORIGIN + "/login", ORIGIN + "/p/board/api/snapshot", ORIGIN + ":443/p/hold/"]) {
    assert.ok(remote.isRemoteUrl(url, ORIGIN), url);
    assert.ok(remote.allowsRequest(url, ORIGIN), url);
  }
  const outside = [
    "http://trade.exemple.org/", "https://trade.exemple.org.evil.test/", "https://evil.test/?" + ORIGIN,
    "https://trade.exemple.org:8443/", "https://x.trade.exemple.org/", "https://trade.exemple.org@evil.test/",
    "file:///C:/Windows/win.ini", "javascript:alert(1)", "about:blank", "", "pas une url", "http://127.0.0.1:8765/",
  ];
  for (const url of outside) {
    assert.ok(!remote.isRemoteUrl(url, ORIGIN), url);
    assert.ok(!remote.allowsRequest(url, ORIGIN), url);
  }
  assert.ok(remote.allowsRequest("data:image/png;base64,AAAA", ORIGIN));   // l'icône en ligne de la page
  assert.ok(!remote.isRemoteUrl("data:text/html,<p>x", ORIGIN));          // mais on n'y navigue pas
});

test("les adresses des profils sont fabriquées ici, jamais reprises du serveur", () => {
  assert.equal(remote.profileUrl(ORIGIN, "board"), ORIGIN + "/p/board/");
  assert.equal(remote.snapshotUrl(ORIGIN, "board"), ORIGIN + "/p/board/api/snapshot");
  for (const name of ["../x", "a/b", "", "Board", "a b", "x".repeat(33), "évil"]) {
    assert.throws(() => remote.profileUrl(ORIGIN, name), /invalide/, name);
  }
  assert.deepEqual(remote.parseProfiles('[{"name": "hold", "path": "https://evil.test/"}, {"name": "board"}]'), ["hold", "board"]);
});

test("une liste de profils douteuse est refusée en bloc", () => {
  const refused = [
    "[]", "{}", "null", '"hold"', '[{"name": "../x"}]', '[{"name": 3}]', "[null]", '[{"nom": "hold"}]',
    '[{"name": "hold"}, {"name": "hold"}]', '[{"name": "accueil"}]',
    JSON.stringify(Array.from({ length: 17 }, (_, i) => ({ name: "p" + i }))),
  ];
  for (const text of refused) assert.throws(() => remote.parseProfiles(text), undefined, text);
  assert.equal(remote.parseProfiles(JSON.stringify(Array.from({ length: 16 }, (_, i) => ({ name: "p" + i })))).length, 16);
});

test("l'état de la session se lit dans la réponse du serveur", async () => {
  const asked = [];
  const state = await remote.sessionState(ORIGIN, async (url) => { asked.push(url); return { status: 200, text: '[{"name": "hold"}, {"name": "board"}]' }; });
  assert.deepEqual(state, { state: "in", names: ["hold", "board"] });
  assert.deepEqual(asked, [ORIGIN + "/api/profiles"]);
  const limits = [];
  await remote.sessionState(ORIGIN, async (url, max) => { limits.push(max); return { status: 401, text: "" }; });
  assert.deepEqual(limits, [remote.MAX_SMALL_BYTES]);
  assert.deepEqual(await remote.sessionState(ORIGIN, answer(401, '{"error": "x"}')), { state: "out" });
  assert.deepEqual(await remote.sessionState(ORIGIN, answer(303, "")), { state: "out" });      // pas encore de compte
  assert.deepEqual(await remote.sessionState(ORIGIN, answer(0, "")), { state: "out" });        // redirection masquée
  for (const fetchText of [answer(200, "<html>"), answer(200, "[]"), answer(500, ""), answer(404, ""), answer(403, "")]) {
    const down = await remote.sessionState(ORIGIN, fetchText);
    assert.equal(down.state, "down");
    assert.match(down.error, /tradeagent/);
  }
  const unreachable = await remote.sessionState(ORIGIN, async () => { throw new Error("net::ERR_NAME_NOT_RESOLVED"); });
  assert.equal(unreachable.state, "down");
  assert.match(unreachable.error, /ne répond pas/);
  // La liste est illisible (redirection que le moteur refuse de rendre) mais le serveur répond : pas de session, c'est tout.
  const alive = async (url) => { if (url.endsWith("/healthz")) return { status: 200, text: "ok\n" }; throw new Error("redirection"); };
  assert.deepEqual(await remote.sessionState(ORIGIN, alive), { state: "out" });
});

test("l'instantané d'un profil est lu en lecture seule, ou vaut null", async () => {
  const asked = [];
  const snap = await remote.fetchSnapshot(ORIGIN, "board", async (url) => { asked.push(url); return { status: 200, text: '{"has_data": true}' }; });
  assert.deepEqual(snap, { has_data: true });
  assert.deepEqual(asked, [ORIGIN + "/p/board/api/snapshot"]);
  assert.equal(await remote.fetchSnapshot(ORIGIN, "board", answer(401, '{"error": "x"}')), null);
  assert.equal(await remote.fetchSnapshot(ORIGIN, "board", answer(200, "pas du json")), null);
  assert.equal(await remote.fetchSnapshot(ORIGIN, "board", answer(200, "[1]")), null);
  assert.equal(await remote.fetchSnapshot(ORIGIN, "board", answer(200, "null")), null);
  const limits = [];
  await remote.fetchSnapshot(ORIGIN, "board", async (url, max) => { limits.push(max); return { status: 200, text: "{}" }; });
  assert.deepEqual(limits, [remote.MAX_BODY_BYTES]);                                           // la limite est demandée à la lecture
  assert.equal(await remote.fetchSnapshot(ORIGIN, "board", async () => { throw new Error("coupé"); }), null);
  assert.equal(await remote.fetchSnapshot(ORIGIN, "../x", answer(200, "{}")), null);          // nom invalide : aucune requête utile
});

test("une réponse est lue par morceaux et abandonnée dès qu'elle dépasse la limite", async () => {
  assert.equal(await remote.boundedText(new Response("bonjour é"), 100), "bonjour é");
  assert.equal(await remote.boundedText(new Response("x".repeat(100)), 100), "x".repeat(100));      // pile à la limite
  await assert.rejects(remote.boundedText(new Response("x".repeat(101)), 100), /trop volumineuse/);
  await assert.rejects(remote.boundedText(new Response("é".repeat(60)), 100), /trop volumineuse/);  // 120 octets, 60 caractères
  assert.equal(await remote.boundedText(new Response(null), 100), "");
  let pulled = 0;
  const endless = new ReadableStream({ pull(controller) { pulled += 1; controller.enqueue(new Uint8Array(1024)); } });
  await assert.rejects(remote.boundedText(new Response(endless), 4096), /trop volumineuse/);
  assert.ok(pulled < 20, "la lecture s'arrête dès la limite : " + pulled);
});

test("un serveur tradeagent se reconnaît à son contrôle de santé", async () => {
  const asked = [];
  assert.equal(await remote.probe(ORIGIN, async (url) => { asked.push(url); return { status: 200, text: "ok\n" }; }), true);
  assert.deepEqual(asked, [ORIGIN + "/healthz"]);
  assert.equal(await remote.probe(ORIGIN, answer(200, "<html>bienvenue</html>")), false);
  assert.equal(await remote.probe(ORIGIN, answer(403, "ok")), false);
  assert.equal(await remote.probe(ORIGIN, async () => { throw new Error("injoignable"); }), false);
});

test("le mode distant ne sait rien faire d'autre que lire", () => {
  const code = fs.readFileSync(path.join(__dirname, "..", "lib", "remote.js"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  for (const forbidden of ["POST", "PUT", "DELETE", "method:", "spawn", "child_process", "stdin", "/logout", "/login", "/setup"]) {
    assert.ok(!code.includes(forbidden), forbidden);
  }
});
