"use strict";

const {
  app, BrowserWindow, Menu, Tray, dialog, ipcMain, nativeImage, shell,
} = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");

const IS_DEV = !app.isPackaged;
const ROOT = path.resolve(__dirname, "..");
const APP_NAME = "Ruodao2API";
const PORT_START = Number(process.env.RUDAO_DESKTOP_PORT || 8388);
const PORT_TRIES = 20;
const HEALTH_TIMEOUT = 45000;

let backendChild = null;
let backendSpawned = false;
let backendPort = PORT_START;
let baseUrl = "";
let mainWindow = null;
let tray = null;
let isQuitting = false;
let restarting = false;
let logStream = null;
let backendLogPath = "";
let desktopLogPath = "";
let trayImage = null;

app.setAppUserModelId("local.ruodao2api.desktop");

function dataDir() {
  if (process.env.RUDAO_DATA_DIR) {
    return path.resolve(process.env.RUDAO_DATA_DIR);
  }
  if (IS_DEV) {
    return path.join(ROOT, "data");
  }
  return path.join(app.getPath("userData"), "data");
}

function ensureDir(dir) {
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (err) {
    console.error("mkdir failed", dir, err);
  }
}

function log() {
  if (!desktopLogPath) {
    return;
  }
  const message = Array.prototype.join.call(arguments, " ");
  const line = "[" + new Date().toISOString() + "] " + message + "\n";
  fs.appendFile(desktopLogPath, line, () => {});
}

function iconPath() {
  return path.join(__dirname, "assets", "app.png");
}

function loadTrayImage() {
  if (trayImage) {
    return trayImage;
  }
  const source = nativeImage.createFromPath(iconPath());
  trayImage = source.isEmpty() ? nativeImage.createEmpty() : source.resize({ width: 16, height: 16 });
  return trayImage;
}

function ping(port) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port: port, path: "/health", timeout: 1200 },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          if (body.length < 2000) {
            body += chunk;
          }
        });
        res.on("end", () => {
          resolve(res.statusCode === 200 && body.indexOf('"status"') >= 0);
        });
      },
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

function waitForHealth(port, timeout) {
  const deadline = Date.now() + timeout;
  return new Promise((resolve) => {
    const attempt = () => {
      ping(port).then((ok) => {
        if (ok) {
          resolve(true);
        } else if (Date.now() >= deadline) {
          resolve(false);
        } else {
          setTimeout(attempt, 450);
        }
      });
    };
    attempt();
  });
}

async function findBackend() {
  for (let offset = 0; offset < PORT_TRIES; offset += 1) {
    const port = PORT_START + offset;
    if (await ping(port)) {
      log("reuse running gateway on port " + port);
      return { port: port, reuse: true };
    }
    const free = await portFree(port);
    if (free) {
      return { port: port, reuse: false };
    }
  }
  throw new Error("端口 " + PORT_START + "-" + (PORT_START + PORT_TRIES - 1) + " 均被占用");
}

function portFree(port) {
  return new Promise((resolve) => {
    const socket = require("net").connect({ host: "127.0.0.1", port: port });
    const done = (free) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(free);
    };
    socket.setTimeout(1000);
    socket.once("connect", () => done(false));
    socket.once("timeout", () => done(true));
    socket.once("error", () => done(true));
  });
}

function backendCommand(port) {
  const env = Object.assign({}, process.env, {
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
    RUDAO_DATA_DIR: dataDir(),
  });
  if (!IS_DEV) {
    return {
      command: path.join(process.resourcesPath, "backend", "Ruodao2API-backend.exe"),
      args: ["--headless", "--port", String(port)],
      cwd: path.dirname(app.getPath("exe")),
      env: env,
    };
  }
  return {
    command: process.env.RUDAO_PYTHON || "python",
    args: [path.join(ROOT, "app.py"), "--headless", "--port", String(port)],
    cwd: ROOT,
    env: env,
  };
}

function startBackend(port) {
  const spec = backendCommand(port);
  if (!IS_DEV && !fs.existsSync(spec.command)) {
    throw new Error("未找到内置后端：" + spec.command);
  }
  ensureDir(dataDir());
  ensureDir(path.dirname(backendLogPath));
  logStream = fs.createWriteStream(backendLogPath, { flags: "a" });
  logStream.write("\n===== " + new Date().toISOString() + " start " + spec.command + " =====\n");
  log("spawn backend: " + spec.command + " " + spec.args.join(" "));
  backendChild = spawn(spec.command, spec.args, {
    cwd: spec.cwd,
    env: spec.env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  backendSpawned = true;
  backendChild.stdout.pipe(logStream, { end: false });
  backendChild.stderr.pipe(logStream, { end: false });
  backendChild.on("error", (err) => {
    log("backend spawn error: " + err.message);
    backendSpawned = false;
    backendChild = null;
  });
  backendChild.on("exit", (code, signal) => {
    log("backend exited code=" + code + " signal=" + signal);
    if (!isQuitting && !restarting) {
      backendSpawned = false;
      backendChild = null;
      if (mainWindow && !mainWindow.isDestroyed()) {
        showStartupError(
          "后台服务已退出",
          "退出码：" + code + (signal ? "，信号：" + signal : "") + "。请查看后台日志后重试。",
        );
      }
    }
  });
}

function stopBackend() {
  if (backendChild && backendSpawned) {
    const pid = backendChild.pid;
    log("stop backend pid=" + pid);
    try {
      // PyInstaller 单文件模式会派生实际服务子进程，必须按进程树结束。
      if (process.platform === "win32") {
        spawnSync("taskkill", ["/pid", String(pid), "/T", "/F"], {
          windowsHide: true,
          stdio: "ignore",
        });
      } else {
        backendChild.kill();
      }
    } catch (err) {
      log("backend kill error: " + err.message);
    }
  }
  backendChild = null;
  backendSpawned = false;
  if (logStream) {
    try {
      logStream.end();
    } catch (err) {
      /* ignore */
    }
    logStream = null;
  }
}

function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

function createWindow() {
  const windowIcon = iconPath();
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 1024,
    minHeight: 660,
    show: false,
    title: APP_NAME,
    backgroundColor: "#f6f7f9",
    icon: fs.existsSync(windowIcon) ? windowIcon : undefined,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: IS_DEV,
    },
  });
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });
  mainWindow.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    let sameOrigin = false;
    try {
      sameOrigin = new URL(url).origin === new URL(baseUrl).origin;
    } catch (err) {
      sameOrigin = false;
    }
    if (!sameOrigin) {
      event.preventDefault();
      if (/^https?:\/\//i.test(url)) {
        shell.openExternal(url);
      }
    }
  });
}

function loadDashboard() {
  baseUrl = "http://127.0.0.1:" + backendPort;
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.loadURL(baseUrl + "/");
  }
}

function showStartupError(message, detail) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
  }
  const query = {
    message: message || "启动失败",
    detail: detail || "",
    log: backendLogPath || "",
  };
  mainWindow.loadFile(path.join(__dirname, "error.html"), { query: query });
  mainWindow.show();
}

async function boot() {
  ensureDir(path.dirname(backendLogPath));
  try {
    const found = await findBackend();
    backendPort = found.port;
    baseUrl = "http://127.0.0.1:" + backendPort;
    if (found.reuse) {
      backendSpawned = false;
      backendChild = null;
      log("reuse gateway " + baseUrl);
    } else {
      startBackend(backendPort);
      const ready = await waitForHealth(backendPort, HEALTH_TIMEOUT);
      if (!ready) {
        throw new Error("后台服务在 " + Math.round(HEALTH_TIMEOUT / 1000) + " 秒内没有就绪");
      }
      log("backend ready on " + baseUrl);
    }
    loadDashboard();
  } catch (err) {
    log("startup failed: " + (err && err.stack ? err.stack : err));
    showStartupError("无法启动 Ruodao2API", String((err && err.message) || err));
  }
}

async function restartBackend() {
  if (!backendSpawned) {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.loadURL(baseUrl + "/");
    }
    return;
  }
  restarting = true;
  stopBackend();
  setTimeout(async () => {
    try {
      startBackend(backendPort);
      const ready = await waitForHealth(backendPort, HEALTH_TIMEOUT);
      if (ready) {
        restarting = false;
        loadDashboard();
      } else {
        restarting = false;
        showStartupError("后台服务重启超时", "请查看后台日志。");
      }
    } catch (err) {
      restarting = false;
      showStartupError("重启后台服务失败", String((err && err.message) || err));
    }
  }, 700);
}

function openPath(target) {
  if (target) {
    shell.openPath(target);
  }
}

function buildAppMenu() {
  const template = [
    {
      label: "Ruodao2API",
      submenu: [
        { label: "打开管理界面", click: showWindow },
        { label: "在浏览器中打开", click: () => shell.openExternal(baseUrl + "/") },
        { type: "separator" },
        { label: "打开数据目录", click: () => openPath(dataDir()) },
        { label: "打开后台日志", click: () => openPath(backendLogPath) },
        { label: "重启后台服务", click: restartBackend },
        { type: "separator" },
        { label: "退出", accelerator: "CmdOrCtrl+Q", click: () => { isQuitting = true; app.quit(); } },
      ],
    },
    {
      label: "视图",
      submenu: [
        { role: "reload", label: "刷新界面" },
        { role: "toggleDevTools", label: "开发者工具", visible: IS_DEV },
        { type: "separator" },
        { role: "resetZoom", label: "实际大小" },
        { role: "zoomIn", label: "放大" },
        { role: "zoomOut", label: "缩小" },
      ],
    },
    {
      label: "帮助",
      submenu: [
        {
          label: "关于",
          click: () => {
            dialog.showMessageBox(mainWindow || undefined, {
              type: "info",
              title: "关于 Ruodao2API",
              message: APP_NAME + " " + app.getVersion(),
              detail: "本地 AI 聚合网关\n后台地址：" + baseUrl +
                "\n数据目录：" + dataDir() +
                "\n\n限速、排队与冷却由本机防护层执行，凭证只保存在本机。",
            });
          },
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createTray() {
  tray = new Tray(loadTrayImage());
  tray.setToolTip(APP_NAME + " · 本地 AI 聚合网关");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "打开管理界面", click: showWindow },
    { label: "在浏览器中打开", click: () => shell.openExternal(baseUrl + "/") },
    { type: "separator" },
    { label: "打开数据目录", click: () => openPath(dataDir()) },
    { label: "重启后台服务", click: restartBackend },
    { type: "separator" },
    { label: "退出", click: () => { isQuitting = true; app.quit(); } },
  ]));
  tray.on("click", showWindow);
  tray.on("double-click", showWindow);
}

function registerIpc() {
  ipcMain.on("desktop:open-log", () => openPath(backendLogPath));
  ipcMain.on("desktop:open-data", () => openPath(dataDir()));
  ipcMain.handle("desktop:open-external", async (_event, url) => {
    const target = String(url || "").trim();
    if (!/^https?:\/\//i.test(target)) {
      return false;
    }
    try {
      await shell.openExternal(target);
      return true;
    } catch (err) {
      log("ERROR", "打开外部链接失败: " + (err && err.message ? err.message : err));
      return false;
    }
  });
  ipcMain.on("desktop:retry", () => {
    if (!backendSpawned && backendChild === null) {
      startBackend(backendPort);
    }
    waitForHealth(backendPort, HEALTH_TIMEOUT).then((ok) => {
      if (ok) {
        loadDashboard();
      } else {
        showStartupError("后台仍未就绪", "请检查后台日志与端口占用。");
      }
    });
  });
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    showWindow();
  });

  app.whenReady().then(() => {
    const appData = app.getPath("userData");
    const logsDir = path.join(appData, "logs");
    ensureDir(logsDir);
    desktopLogPath = path.join(logsDir, "desktop.log");
    backendLogPath = path.join(logsDir, "backend.log");
    log("app start dev=" + IS_DEV + " version=" + app.getVersion());
    registerIpc();
    createWindow();
    createTray();
    buildAppMenu();
    boot();
  });

  app.on("activate", () => {
    showWindow();
  });

  app.on("window-all-closed", () => {
    /* keep running in tray */
  });

  app.on("before-quit", () => {
    isQuitting = true;
    stopBackend();
  });

  app.on("will-quit", () => {
    if (logStream) {
      try {
        logStream.end();
      } catch (err) {
        /* ignore */
      }
    }
  });
}
