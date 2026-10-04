"use strict";
// `npm start` : lance Electron sur ce dossier.
// ELECTRON_RUN_AS_NODE (hérité de certains terminaux, dont ceux de VSCode) ferait démarrer Electron comme un
// simple Node, sans fenêtre : on le retire.
const { spawn } = require("node:child_process");

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
const child = spawn(require("electron"), [__dirname, ...process.argv.slice(2)], { stdio: "inherit", env });
child.on("exit", (code) => process.exit(code === null ? 1 : code));
