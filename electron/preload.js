"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("ruodaoDesktop", {
  isDesktop: true,
  openLog: () => ipcRenderer.send("desktop:open-log"),
  openData: () => ipcRenderer.send("desktop:open-data"),
  retry: () => ipcRenderer.send("desktop:retry"),
  openExternal: (url) => ipcRenderer.invoke("desktop:open-external", url),
});
