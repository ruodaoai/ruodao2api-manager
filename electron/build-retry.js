"use strict";

// Windows 安全软件（360/火绒等）会在新 exe 落盘后短暂独占扫描，
// electron-builder 紧随其后的读写会 EBUSY。这里对文件读写做退避重试。
const fsp = require("fs/promises");
const resEdit = require("app-builder-lib/out/util/resEdit.js");

function withRetry(fn) {
  return async function retried(...args) {
    let lastError;
    for (let attempt = 0; attempt < 20; attempt++) {
      try {
        return await fn.apply(this, args);
      } catch (error) {
        if (error.code !== "EBUSY" && error.code !== "EPERM") throw error;
        lastError = error;
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
    throw lastError;
  };
}

fsp.writeFile = withRetry(fsp.writeFile);
fsp.readFile = withRetry(fsp.readFile);
resEdit.editWindowsResources = withRetry(resEdit.editWindowsResources);

// 便携版以当前用户权限运行，不需要 elevate.exe；跳过注入，
// 避免 360 扫描刚复制出来的 elevate.exe 导致 7z 打包 EBUSY。
const nsisUtil = require("app-builder-lib/out/targets/nsis/nsisUtil.js");
nsisUtil.CopyElevateHelper.prototype.resolve = async function resolveWithoutElevate() {
  return null;
};

// 安全软件扫描刚写入的 exe 需要时间，7z 归档和 NSIS 嵌入卸载器
// 紧接其后就会读取这些文件，可通过环境变量插入等待窗口。
// 延迟到事件循环再加载 NsisTarget，避免与 app-builder-lib 的模块初始化形成循环依赖。
const scanDelay = parseInt(process.env.RUDAO_SCAN_DELAY_MS || "0", 10);
const entryScript = process.argv[1] || "";
if (scanDelay > 0 && entryScript.includes("electron-builder")) {
  setImmediate(() => {
    const sleep = () => new Promise((resolve) => setTimeout(resolve, scanDelay));
    const nsisTarget = require("app-builder-lib/out/targets/nsis/NsisTarget.js").NsisTarget;
    const originalBuildAppPackage = nsisTarget.prototype.buildAppPackage;
    nsisTarget.prototype.buildAppPackage = async function delayedArchive(...args) {
      await sleep();
      return originalBuildAppPackage.apply(this, args);
    };
    const originalComputeScript = nsisTarget.prototype.computeScriptAndSignUninstaller;
    nsisTarget.prototype.computeScriptAndSignUninstaller = async function delayedEmbed(...args) {
      const result = await originalComputeScript.apply(this, args);
      await sleep();
      return result;
    };
  });
}
