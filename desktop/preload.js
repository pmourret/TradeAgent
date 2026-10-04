"use strict";
/*
 * Chargé UNIQUEMENT par la barre d'onglets (shell.html), jamais par la page d'un profil.
 * Deux messages, rien d'autre : recevoir l'état des onglets, demander d'en afficher un.
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktop", {
  onTabs: (callback) => ipcRenderer.on("tabs", (_event, state) => callback(state)),
  select: (name) => ipcRenderer.send("select-tab", String(name)),
  ready: () => ipcRenderer.send("shell-ready"),
});
