"""Background scheduler: daily check-in, token refresh and balance refresh."""

import threading
import time

from . import util

TICK_SECONDS = 15


class Scheduler(object):
    def __init__(self, runtime):
        self.runtime = runtime
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._fired = {}
        self._last_balance = 0.0
        self._last_expiry_scan = 0.0

    # -- lifecycle --------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ruodao-scheduler",
                                        daemon=True)
        self._thread.start()
        self.runtime.log("INFO", "定时任务已启动")

    def stop(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        self._thread = None

    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def wake(self):
        self._wake.set()

    # -- loop -------------------------------------------------------------
    def _loop(self):
        self._wake.wait(2)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.runtime.log("ERROR", "定时任务异常：%s" % exc)
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()

    def tick(self):
        if not self.runtime.config.get("scheduler_enabled", True):
            return
        now = time.time()
        self._maybe_checkin(now)
        self._maybe_daily_refresh(now)
        self._maybe_refresh_expiring(now)
        self._maybe_balance(now)

    # -- helpers ----------------------------------------------------------
    def _mark(self, key):
        with self._lock:
            if self._fired.get(key):
                return False
            self._fired[key] = time.time()
            return True

    def _today(self):
        return time.strftime("%Y-%m-%d")

    @staticmethod
    def _minutes(text):
        try:
            hour, minute = str(text or "").split(":")[:2]
            return int(hour) * 60 + int(minute)
        except Exception:
            return -1

    def _now_minutes(self):
        local = time.localtime()
        return local.tm_hour * 60 + local.tm_min

    # -- check-in ---------------------------------------------------------
    def _maybe_checkin(self, now):
        if not self.runtime.config.get("checkin_enabled", True):
            return
        times = self.runtime.config.get("checkin_times") or []
        slots = [self._minutes(t) for t in times]
        slots = [s for s in slots if s >= 0]
        if not slots:
            return
        current = self._now_minutes()
        if not any(current >= s for s in slots):
            return
        if not self._mark("checkin:" + self._today()):
            return
        threading.Thread(target=self.run_checkin, name="ruodao-checkin",
                         daemon=True).start()

    def run_checkin(self):
        accounts = self.runtime.accounts.list()
        targets = [a for a in accounts
                   if self.runtime.accounts.checkin_eligible(a)]
        if not targets:
            return {"checked": 0, "skipped": 0, "failed": 0}
        today = self._today()
        checked = skipped = failed = 0
        for account in targets:
            data = account.get("data") or {}
            if str(data.get("last_checkin") or "") == today:
                skipped += 1
                continue
            channel = self.runtime.channels.get(account.get("channel"))
            if not channel:
                continue
            try:
                channel.checkin(account)
                self.runtime.accounts.save(account)
                checked += 1
                self.runtime.log("INFO", "自动签到 %s/%s：%s"
                                 % (account.get("channel"), account.get("name"),
                                    data.get("last_checkin_message") or "完成"))
            except Exception as exc:
                failed += 1
                self.runtime.log("WARN", "自动签到 %s/%s 失败：%s"
                                 % (account.get("channel"), account.get("name"), exc))
            time.sleep(0.4)
        return {"checked": checked, "skipped": skipped, "failed": failed}

    # -- token refresh ----------------------------------------------------
    def _maybe_daily_refresh(self, now):
        target = self._minutes(self.runtime.config.get("refresh_time") or "22:00")
        if target < 0 or self._now_minutes() < target:
            return
        if not self._mark("refresh:" + self._today()):
            return
        threading.Thread(target=self.run_refresh, name="ruodao-refresh",
                         daemon=True).start()

    def run_refresh(self):
        refreshed = 0
        for account in self.runtime.accounts.list():
            if not account.get("enabled"):
                continue
            try:
                if self.runtime.accounts.refresh_account(account, force=True):
                    refreshed += 1
            except Exception:
                continue
        if refreshed:
            self.runtime.log("INFO", "每日凭证刷新完成，更新 %d 个账号" % refreshed)

    def _maybe_refresh_expiring(self, now):
        if now - self._last_expiry_scan < 600:
            return
        self._last_expiry_scan = now
        for account in self.runtime.accounts.list():
            if not account.get("enabled"):
                continue
            data = account.get("data") or {}
            if data.get("needs_login"):
                continue
            exp = util.normalize_epoch(data.get("expires_at"))
            if not exp:
                continue
            if exp <= now + 1800:
                try:
                    if self.runtime.accounts.refresh_account(account, force=True):
                        self.runtime.log("INFO", "凭证即将过期，已刷新 %s"
                                         % account.get("name"))
                except Exception:
                    continue

    # -- balance ----------------------------------------------------------
    def _maybe_balance(self, now):
        try:
            minutes = max(5, int(self.runtime.config.get("credits_refresh_minutes")
                                 or 30))
        except Exception:
            minutes = 30
        if now - self._last_balance < minutes * 60:
            return
        self._last_balance = now
        threading.Thread(target=self._balance_job, name="ruodao-balance",
                         daemon=True).start()

    def _balance_job(self):
        try:
            self.runtime.accounts.refresh_all_balances(force=True)
        except Exception as exc:
            self.runtime.log("WARN", "刷新余额失败：%s" % exc)
