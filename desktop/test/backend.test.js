"use strict";
// Tests hors réseau (boucle locale seulement), sans Electron : `npm test` dans desktop/.
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const test = require("node:test");
const backend = require("../lib/backend");

function serve(headers) {
  return new Promise((resolve) => {
    const server = http.createServer((_req, res) => {
      res.writeHead(200, headers);
      res.end();
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

test("le Python utilisé est celui du venv du projet", () => {
  assert.equal(backend.venvPython("/p", "linux"), path.join("/p", ".venv", "bin", "python"));
  assert.equal(backend.venvPython("C:\\p", "win32"), path.join("C:\\p", ".venv", "Scripts", "python.exe"));
});

test("l'interface est lancée exactement comme à la main, sans ouvrir de navigateur ni changer de port", () => {
  const args = backend.webArgs("llm");
  assert.deepEqual(args, ["-u", "-m", "tradeagent", "web", "--profile", "llm", "--config", "config.yaml"]);
  for (const forbidden of ["--open", "--port", "--host", "run", "reset", "resume"]) assert.ok(!args.includes(forbidden));
});

test("les enfants écrivent en UTF-8 sans tampon", () => {
  const env = backend.childEnv({ PATH: "x" });
  assert.deepEqual(env, { PATH: "x", PYTHONUNBUFFERED: "1", PYTHONUTF8: "1" });
});

test("les profils viennent de Python et sont validés", () => {
  const ok = backend.parseProfiles('[{"name": "hold", "port": 8765, "description": "d", "costs_money": false}]');
  assert.equal(ok[0].port, 8765);
  assert.throws(() => backend.parseProfiles("[]"));
  assert.throws(() => backend.parseProfiles('[{"name": "../x", "port": 8765}]'));
  assert.throws(() => backend.parseProfiles('[{"name": "hold", "port": "8765"}]'));
  assert.throws(() => backend.parseProfiles('[{"name": "hold", "port": 0}]'));
  assert.throws(() => backend.parseProfiles("pas du json"));
});

test("la page d'un profil ne peut pas quitter son serveur local", () => {
  assert.equal(backend.profileUrl(8765), "http://127.0.0.1:8765/");
  assert.ok(backend.isProfileUrl("http://127.0.0.1:8765/api/snapshot", 8765));
  for (const url of [
    "http://127.0.0.1:8766/", "https://127.0.0.1:8765/", "http://localhost:8765/", "http://127.0.0.1.evil.test:8765/",
    "http://evil.test/", "file:///C:/Windows/win.ini", "javascript:alert(1)", "",
  ]) assert.ok(!backend.isProfileUrl(url, 8765), url);
});

test("un port n'est affiché que si c'est notre serveur qui y répond", async () => {
  const ours = await serve({ Server: "tradeagent" });
  const foreign = await serve({ Server: "nginx" });
  const port = ours.address().port;
  try {
    assert.equal(await backend.probe(port), "ours");
    assert.equal(await backend.probe(foreign.address().port), "foreign");
  } finally {
    await new Promise((r) => ours.close(r));
    foreign.close();
  }
  assert.equal(await backend.probe(port), "down");   // port rendu : plus personne
});

test("l'instantané n'est lu que sur notre serveur, en GET, et une réponse inutilisable donne null", async () => {
  const methods = [];
  const answer = (status, headers, body) => new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      methods.push(req.method);
      res.writeHead(status, headers);
      res.end(body);
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
  const cases = [
    [200, { Server: "tradeagent" }, '{"has_data": false, "generated_at": 1}', { has_data: false, generated_at: 1 }],
    [200, { Server: "nginx" }, '{"has_data": false}', null],
    [503, { Server: "tradeagent" }, '{"error": "base momentanément illisible"}', null],
    [200, { Server: "tradeagent" }, "pas du json", null],
    [200, { Server: "tradeagent" }, `{"x": "${"a".repeat(5 * 1024 * 1024)}"}`, null],
  ];
  for (const [status, headers, body, expected] of cases) {
    const server = await answer(status, headers, body);
    try {
      assert.deepEqual(await backend.fetchSnapshot(server.address().port), expected);
    } finally {
      server.closeAllConnections();
      await new Promise((r) => server.close(r));
    }
  }
  assert.deepEqual([...new Set(methods)], ["GET"]);

  // Une réponse au compte-gouttes ne bloque pas la surveillance : la lecture entière est bornée.
  const drip = http.createServer((_req, res) => {
    res.writeHead(200, { Server: "tradeagent" });
    const timer = setInterval(() => res.write(" "), 20);
    res.on("close", () => clearInterval(timer));
  });
  await new Promise((r) => drip.listen(0, "127.0.0.1", r));
  try {
    const started = Date.now();
    assert.equal(await backend.fetchSnapshot(drip.address().port, 200), null);
    assert.ok(Date.now() - started < 2000);
  } finally {
    drip.closeAllConnections();
    await new Promise((r) => drip.close(r));
  }

  const gone = await serve({});
  const port = gone.address().port;
  await new Promise((r) => gone.close(r));
  assert.equal(await backend.fetchSnapshot(port), null);
});

test("l'attente s'arrête dès que le processus est mort", async () => {
  const closed = await serve({});
  const port = closed.address().port;
  await new Promise((r) => closed.close(r));
  const started = Date.now();
  assert.equal(await backend.waitForServer(port, { timeoutMs: 10000, intervalMs: 10, stillAlive: () => false }), "down");
  assert.ok(Date.now() - started < 3000);
});
