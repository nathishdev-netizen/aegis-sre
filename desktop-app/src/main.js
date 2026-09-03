'use strict';
/**
 * Electron main process.
 *
 * File reading happens HERE, in Electron's own runtime. That is the whole reason this
 * shell exists: a bundled Python re-executes itself through a nested Python.app with
 * its own code signature, so a Full Disk Access grant on the app never applied to the
 * process actually doing the reading. Electron has no such indirection.
 */

const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const fs = require('fs');
const path = require('path');
const { RuntimeState } = require('./state');
const { askModel, interpretRun, backendStatus } = require('./llm');
const { listListeningPorts, logFileForPid } = require('./sources');

const POLL_MS = 400;
const BACKFILL_LINES = 200;

let win = null;
const runtime = new RuntimeState();
let watcher = null;

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

runtime.on('change', (snapshot) => send('state', snapshot));

/** Tail a file: read what is already there, then follow it as it grows. */
function watchFile(filePath) {
  stopWatching();
  let offset = 0;
  try {
    const stat = fs.statSync(filePath);
    // Show the recent run immediately. Starting at the very end leaves an empty
    // screen until the next line happens to arrive, which reads as broken.
    const existing = fs.readFileSync(filePath, 'utf8').split('\n').filter((l) => l.trim());
    runtime.reset();
    runtime.setSource({ type: 'file', label: `Watching ${path.basename(filePath)}`, path: filePath });
    existing.slice(-BACKFILL_LINES).forEach((l) => runtime.ingest(l));
    offset = stat.size;
  } catch (err) {
    runtime.setSource({ type: 'file', label: `Cannot read file: ${err.code || err.message}`, path: filePath });
    return;
  }

  watcher = setInterval(() => {
    let stat;
    try {
      stat = fs.statSync(filePath);
    } catch {
      return; // file rotated away; keep waiting for it to return
    }
    if (stat.size < offset) offset = 0;      // truncated / rotated
    if (stat.size === offset) return;
    try {
      const fd = fs.openSync(filePath, 'r');
      const buf = Buffer.alloc(stat.size - offset);
      fs.readSync(fd, buf, 0, buf.length, offset);
      fs.closeSync(fd);
      offset = stat.size;
      buf.toString('utf8').split('\n').forEach((l) => { if (l.trim()) runtime.ingest(l); });
    } catch (err) {
      runtime.setSource({ type: 'file', label: `Read error: ${err.code || err.message}`, path: filePath });
    }
  }, POLL_MS);
}

function stopWatching() {
  if (watcher) { clearInterval(watcher); watcher = null; }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1380,
    height: 920,
    minWidth: 940,
    minHeight: 640,
    title: 'Log Agent',
    titleBarStyle: 'hiddenInset',
    backgroundColor: '#f5f7fb',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, '..', 'web', 'index.html'));
  win.on('closed', () => { stopWatching(); win = null; });
}

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('window-all-closed', () => { stopWatching(); app.quit(); });

// --- IPC: everything the renderer can ask for -------------------------------

ipcMain.handle('get-state', () => runtime.snapshot());
ipcMain.handle('reset', () => runtime.reset());
ipcMain.handle('backend-status', () => backendStatus());

ipcMain.handle('list-ports', async () => listListeningPorts());

ipcMain.handle('attach-port', async (_e, port) => {
  // Prefer the log FILE the process already writes: most apps serve no log endpoint,
  // so probing them means several 404s in their console to learn what the OS knows.
  const ports = await listListeningPorts();
  const match = ports.find((p) => p.port === Number(port));
  const file = match ? logFileForPid(match.pid) : null;
  if (file) { watchFile(file); return runtime.snapshot(); }
  runtime.setSource({
    type: 'port',
    label: `Port ${port} writes no log file this app can find`,
    path: `127.0.0.1:${port}`,
  });
  return runtime.snapshot();
});

ipcMain.handle('attach-file', async (_e, filePath) => { watchFile(filePath); return runtime.snapshot(); });

ipcMain.handle('pick-file', async () => {
  // The native picker is also how a user grants access to a folder macOS protects.
  const result = await dialog.showOpenDialog(win, {
    title: 'Choose a log file to watch',
    properties: ['openFile'],
    filters: [{ name: 'Logs', extensions: ['log', 'txt', 'out', 'jsonl'] }, { name: 'All files', extensions: ['*'] }],
  });
  if (result.canceled || !result.filePaths.length) return null;
  watchFile(result.filePaths[0]);
  return runtime.snapshot();
});

ipcMain.handle('ask', async (_e, question) => askModel(runtime.snapshot(), question));
ipcMain.handle('interpret', async () => {
  const result = await interpretRun(runtime.snapshot());
  if (result) {
    runtime.summary = result.summary || runtime.summary;
    runtime.reason = result.reason || runtime.reason;
    if (result.causes?.length) runtime.causes = result.causes;
    if (result.fixes?.length) runtime.fixes = result.fixes;
    runtime.confidence = result.confidence || 0;
    runtime.evidence = result.evidence || [];
    runtime.interpretation = 'llm';
    runtime.emitChange();
  }
  return runtime.snapshot();
});

ipcMain.handle('open-external', (_e, url) => shell.openExternal(url));
