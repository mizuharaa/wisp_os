const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("wisp", {
  // height only (legacy callers) or {w,h} — the island morphs on both axes
  resize: (height) => ipcRenderer.send("minibar:resize", height),
  bounds: (w, h) => ipcRenderer.send("minibar:bounds", { w, h }),
  move: (dx, dy) => ipcRenderer.send("minibar:move", { dx, dy }),
  hide: () => ipcRenderer.send("minibar:hide"),
  openMain: () => ipcRenderer.send("minibar:open-main"),
  // the dashboard is frameless, so it draws its own traffic lights
  win: {
    close: () => ipcRenderer.send("win:close"),
    minimize: () => ipcRenderer.send("win:minimize"),
    maximize: () => ipcRenderer.send("win:maximize"),
  },
});
