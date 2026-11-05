"""Upstream protection: pacing, quotas, concurrency and cooldown hints."""

import math
import random
import threading
import time
from collections import deque

from .errors import RateLimited

WINDOW = 60.0


def _int(value, default, low=0, high=None):
    try:
        number = int(float(value))
    except Exception:
        number = default
    number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def day_start(now=None):
    current = time.localtime(now or time.time())
    return time.mktime((current.tm_year, current.tm_mon, current.tm_mday,
                        0, 0, 0, 0, 0, -1))


class Lease(object):
    """One admitted upstream call. Always release it when the call is done."""

    def __init__(self, guard=None, account_id=""):
        self.guard = guard
        self.account_id = account_id
        self._released = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()

    def release(self):
        if self._released:
            return
        self._released = True
        if self.guard is not None:
            self.guard._release(self.account_id)


class _AccountState(object):
    def __init__(self):
        self.window = deque()
        self.inflight = 0
        self.last_start = 0.0
        self.wait_ms = 0

    def prune(self, now):
        while self.window and now - self.window[0] >= WINDOW:
            self.window.popleft()


class Guard(object):
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self._states = {}
        self._global_window = deque()
        self._global_inflight = 0
        self._stats = {
            "allowed": 0,
            "queued": 0,
            "rejected": 0,
            "wait_total_ms": 0,
            "wait_count": 0,
            "last_reject_at": 0,
            "last_reject_reason": "",
        }
        self.started_at = time.time()

    # -- configuration ----------------------------------------------------
    def _cfg(self, key, default):
        try:
            return self.runtime.config.get(key, default)
        except Exception:
            return default

    def settings(self):
        return {
            "enabled": bool(self._cfg("guard_enabled", True)),
            "global_rpm": _int(self._cfg("guard_global_rpm", 120), 120, 1, 6000),
            "account_rpm": _int(self._cfg("guard_account_rpm", 30), 30, 1, 600),
            "daily_limit": _int(self._cfg("guard_daily_limit", 0), 0, 0),
            "max_concurrency": _int(self._cfg("guard_max_concurrency", 2), 2, 1, 32),
            "global_concurrency": _int(
                self._cfg("guard_global_concurrency", 4), 4, 1, 128),
            "min_interval_ms": _int(
                self._cfg("guard_min_interval_ms", 350), 350, 0, 60000),
            "jitter_ms": _int(self._cfg("guard_jitter_ms", 250), 250, 0, 10000),
            "queue_timeout": _int(
                self._cfg("guard_queue_timeout", 30), 30, 1, 300),
            "cooldown_429": _int(
                self._cfg("guard_cooldown_429", 180), 180, 5, 1800),
            "cooldown_5xx": _int(
                self._cfg("guard_cooldown_5xx", 30), 30, 1, 600),
        }

    def cooldown_for(self, error):
        status = int(getattr(error, "status", 0) or 0)
        cfg = self.settings()
        if status == 429:
            wait = int(getattr(error, "wait", 0) or 0)
            return min(1800, max(cfg["cooldown_429"], wait))
        if status >= 500:
            return cfg["cooldown_5xx"]
        return None

    # -- state ------------------------------------------------------------
    def _state(self, account_id):
        state = self._states.get(account_id)
        if state is None:
            state = _AccountState()
            self._states[account_id] = state
        return state

    def _reject(self, reason, wait, code="guard_local"):
        with self.lock:
            self._stats["rejected"] += 1
            self._stats["last_reject_at"] = time.time()
            self._stats["last_reject_reason"] = reason
        raise RateLimited(reason, wait=max(1, int(math.ceil(wait))), code=code)

    def reset_stats(self):
        with self.lock:
            for key in ("allowed", "queued", "rejected", "wait_total_ms",
                        "wait_count"):
                self._stats[key] = 0
            self._stats["last_reject_at"] = 0
            self._stats["last_reject_reason"] = ""
            self.started_at = time.time()

    @staticmethod
    def _daily_used(runtime, account_id, now):
        try:
            return runtime.store.usage_count_account(account_id, day_start(now))
        except Exception:
            return 0

    # -- admission --------------------------------------------------------
    def acquire(self, account):
        """Wait for a safe slot, then return a Lease. Raises RateLimited."""
        enabled = bool(self._cfg("guard_enabled", True))
        if not enabled:
            return Lease()
        cfg = self.settings()
        account_id = str((account or {}).get("id") or "")
        name = str((account or {}).get("name") or account_id or "账号")
        if not account_id:
            return Lease()
        now = time.time()
        if cfg["daily_limit"] > 0:
            used = self._daily_used(self.runtime, account_id, now)
            if used >= cfg["daily_limit"]:
                remain = max(1, int(day_start(now) + 86400 - now))
                self._reject(
                    "%s 已达每日请求上限 %d 次，明日自动恢复"
                    % (name, cfg["daily_limit"]), min(remain, 3600),
                    code="guard_local")
        deadline = now + cfg["queue_timeout"]
        waited = 0.0
        jitter = random.uniform(0, cfg["jitter_ms"]) / 1000.0
        while True:
            now = time.time()
            with self.lock:
                state = self._state(account_id)
                state.prune(now)
                while self._global_window and \
                        now - self._global_window[0] >= WINDOW:
                    self._global_window.popleft()
                if cfg["daily_limit"] > 0:
                    used = self._daily_used(self.runtime, account_id, now)
                    if used >= cfg["daily_limit"]:
                        self._reject(
                            "%s 已达每日请求上限 %d 次，明日自动恢复"
                            % (name, cfg["daily_limit"]),
                            min(max(1, day_start(now) + 86400 - now), 3600),
                            code="guard_local")
                waits = []
                if len(state.window) >= cfg["account_rpm"]:
                    waits.append(WINDOW - (now - state.window[0]))
                if state.last_start:
                    gap = cfg["min_interval_ms"] / 1000.0 + jitter
                    waits.append(state.last_start + gap - now)
                if len(self._global_window) >= cfg["global_rpm"]:
                    waits.append(WINDOW - (now - self._global_window[0]))
                if state.inflight >= cfg["max_concurrency"] or \
                        self._global_inflight >= cfg["global_concurrency"]:
                    waits.append(0.2)
                need = max(waits) if waits else 0
                if need <= 0:
                    state.window.append(now)
                    state.last_start = now
                    state.inflight += 1
                    state.wait_ms = int(waited * 1000)
                    self._global_window.append(now)
                    self._global_inflight += 1
                    self._stats["allowed"] += 1
                    if waited > 0.05:
                        self._stats["queued"] += 1
                        self._stats["wait_total_ms"] += int(waited * 1000)
                        self._stats["wait_count"] += 1
                    return Lease(self, account_id)
            remaining = deadline - now
            if remaining <= 0:
                reason = "请求过于集中，%s 的防护队列已满，请稍后重试" % name
                global_hit = (len(self._global_window) >= cfg["global_rpm"]
                              or self._global_inflight >= cfg["global_concurrency"])
                account_hit = (len(state.window) >= cfg["account_rpm"]
                               or state.inflight >= cfg["max_concurrency"])
                if global_hit:
                    wait = WINDOW
                    code = "guard_global"
                elif account_hit:
                    wait = WINDOW
                    code = "guard_local"
                else:
                    wait = 5
                    code = "guard_local"
                self._reject(reason, wait, code=code)
            pause = min(max(need, 0.05), 0.25, remaining)
            time.sleep(pause)
            waited += pause

    def _release(self, account_id):
        with self.lock:
            state = self._states.get(account_id)
            if state and state.inflight > 0:
                state.inflight -= 1
            if self._global_inflight > 0:
                self._global_inflight -= 1

    # -- views ------------------------------------------------------------
    def snapshot(self):
        cfg = self.settings()
        now = time.time()
        accounts = []
        try:
            items = self.runtime.accounts.list()
        except Exception:
            items = []
        with self.lock:
            for acc in items:
                account_id = str(acc.get("id") or "")
                state = self._states.get(account_id)
                state_now = dict(
                    rpm_used=0, inflight=0, wait_ms=0, last_start=0)
                if state:
                    state.prune(now)
                    state_now = {
                        "rpm_used": len(state.window),
                        "inflight": state.inflight,
                        "wait_ms": state.wait_ms,
                        "last_start": state.last_start,
                    }
                try:
                    status, status_label = self.runtime.accounts.status_of(acc)
                except Exception:
                    status, status_label = "", ""
                try:
                    cooldown = self.runtime.accounts.cooldown_remaining(account_id)
                except Exception:
                    cooldown = 0
                accounts.append({
                    "id": account_id,
                    "name": acc.get("name") or "",
                    "channel": acc.get("channel") or "",
                    "enabled": bool(acc.get("enabled")),
                    "status": status,
                    "status_label": status_label,
                    "cooldown": int(cooldown),
                    "daily_used": self._daily_used(self.runtime, account_id, now),
                    "rpm_used": state_now["rpm_used"],
                    "inflight": state_now["inflight"],
                    "wait_ms": state_now["wait_ms"],
                    "last_start": state_now["last_start"],
                })
            global_window = len(self._global_window)
            global_inflight = self._global_inflight
            stats = dict(self._stats)
        stats["avg_wait_ms"] = int(stats["wait_total_ms"] / stats["wait_count"]) \
            if stats["wait_count"] else 0
        stats["uptime"] = int(now - self.started_at)
        return {
            "enabled": cfg["enabled"],
            "settings": cfg,
            "global": {
                "rpm_used": global_window,
                "inflight": global_inflight,
            },
            "accounts": accounts,
            "stats": stats,
        }
