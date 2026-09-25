// Inside a real Electron main process the built-in resolves first; if a node_modules
// copy shadows it, require returns the npm helper's CLI path string instead.
const m = process.mainModule || module;
console.log('is main process:', !!process.versions.electron);
try { console.log('electron/index.js exists:', require('fs').existsSync('./node_modules/electron/index.js')); } catch {}
