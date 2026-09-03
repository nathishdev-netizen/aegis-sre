'use strict';
// The only bridge between the page and the main process. contextIsolation is on and
// nodeIntegration off, so the renderer gets exactly these calls and nothing else.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('agent', {
  getState: () => ipcRenderer.invoke('get-state'),
  reset: () => ipcRenderer.invoke('reset'),
  backendStatus: () => ipcRenderer.invoke('backend-status'),
  listPorts: () => ipcRenderer.invoke('list-ports'),
  attachPort: (port) => ipcRenderer.invoke('attach-port', port),
  attachFile: (p) => ipcRenderer.invoke('attach-file', p),
  pickFile: () => ipcRenderer.invoke('pick-file'),
  ask: (q) => ipcRenderer.invoke('ask', q),
  interpret: () => ipcRenderer.invoke('interpret'),
  onState: (cb) => ipcRenderer.on('state', (_e, s) => cb(s)),
});
