const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("wisp", {
  resize: (height) => ipcRenderer.send("minibar:resize", height),
  hide: () => ipcRenderer.send("minibar:hide"),
  openMain: () => ipcRenderer.send("minibar:open-main"),
  // the dashboard is frameless, so it draws its own traffic lights
  win: {
    close: () => ipcRenderer.send("win:close"),
    minimize: () => ipcRenderer.send("win:minimize"),
    maximize: () => ipcRenderer.send("win:maximize"),
  },
});
