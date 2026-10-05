"use strict";
/*
 * Chargé UNIQUEMENT par la fenêtre de choix (connect.html). Trois messages : proposer une adresse de serveur,
 * choisir cet ordinateur, signaler que la fenêtre est prête. Aucun n'agit sur un bot.
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("connect", {
  remote: (address) => ipcRenderer.invoke("connect-remote", String(address)),
  local: () => ipcRenderer.send("connect-local"),
  ready: () => ipcRenderer.send("connect-ready"),
  onCurrent: (callback) => ipcRenderer.on("connect-current", (_event, value) => callback(String(value || ""))),
});
