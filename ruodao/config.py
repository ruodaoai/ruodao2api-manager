"""Configuration and paths. Everything lives next to the app by default."""

import os
import sys
from pathlib import Path

from .util import atomic_write_json, make_api_key, read_json

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = (Path(sys.executable).resolve().parent if FROZEN
        else Path(__file__).resolve().parent.parent)
DATA_DIR = Path(os.environ.get("RUDAO_DATA_DIR") or (ROOT / "data")).resolve()
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "ruodao.db"

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8388,
    "api_key": "",
    "checkin_enabled": True,
    "checkin_times": ["10:05", "21:05"],
    "refresh_time": "22:00",
    "credits_refresh_minutes": 30,
    "scheduler_enabled": True,
    "request_timeout": 600,
    "max_body_mb": 32,
    "allow_origins": ["http://127.0.0.1", "http://localhost"],
    "guard_enabled": True,
    "guard_global_rpm": 120,
    "guard_account_rpm": 30,
    "guard_daily_limit": 0,
    "guard_max_concurrency": 2,
    "guard_global_concurrency": 4,
    "guard_min_interval_ms": 350,
    "guard_jitter_ms": 250,
    "guard_queue_timeout": 30,
    "guard_cooldown_429": 180,
    "guard_cooldown_5xx": 30,
}


class Config(object):
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.values = dict(DEFAULTS)
        loaded = read_json(CONFIG_PATH, {}) or {}
        if isinstance(loaded, dict):
            self.values.update(loaded)
        if not self.values.get("api_key"):
            self.values["api_key"] = make_api_key()
            self.save()
        self.apply_env()

    def apply_env(self):
        if os.environ.get("RUODAO_HOST"):
            self.values["host"] = os.environ["RUODAO_HOST"]
        if os.environ.get("RUODAO_PORT"):
            try:
                self.values["port"] = int(os.environ["RUODAO_PORT"])
            except Exception:
                pass
        if os.environ.get("RUODAO_API_KEY"):
            self.values["api_key"] = os.environ["RUODAO_API_KEY"].strip()

    def save(self):
        atomic_write_json(CONFIG_PATH, self.values)

    def get(self, key, default=None):
        return self.values.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        self.values[key] = value
        self.save()

    def update(self, patch):
        for key, value in (patch or {}).items():
            if key in DEFAULTS or key in ("api_key",):
                self.values[key] = value
        self.save()

    @property
    def api_key(self):
        return str(self.values.get("api_key") or "")

    @property
    def host(self):
        return str(self.values.get("host") or "127.0.0.1")

    @property
    def port(self):
        try:
            return int(self.values.get("port") or 8388)
        except Exception:
            return 8388

    @property
    def base_url(self):
        host = self.host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return "http://%s:%d" % (host, self.port)

    def rotate_key(self):
        self.values["api_key"] = make_api_key()
        self.save()
        return self.values["api_key"]

    def public(self):
        data = dict(self.values)
        data["base_url"] = self.base_url
        data["data_dir"] = str(DATA_DIR)
        return data
