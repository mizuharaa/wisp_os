const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("wisp", {
  resize: (height) => ipcRenderer.send("minibar:resize", height),
  hide: () => ipcRenderer.send("minibar:hide"),
  openMain: () => ipcRenderer.send("minibar:open-main"),
});
