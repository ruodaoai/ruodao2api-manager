"""Native Windows control panel (Tkinter) for the local gateway."""

import json
import os
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, scrolledtext, ttk

from . import util
from .config import DATA_DIR
from .errors import UpstreamError
from .gateway import GatewayService
from .runtime import CHANNEL_META

BG = "#f4f5f7"
WHITE = "#ffffff"
BORDER = "#e6e8ee"
TEXT = "#1f2430"
MUTED = "#717886"
PRIMARY = "#f4511e"
PRIMARY_DARK = "#d84315"
PRIMARY_SOFT = "#fff1eb"
GREEN = "#2f8f6b"
GREEN_SOFT = "#e8f5ef"
AMBER = "#a86b12"
AMBER_SOFT = "#fdf3e0"
RED = "#c0392b"
RED_SOFT = "#fdeceb"
BLUE = "#2f6fbe"
BLUE_SOFT = "#eaf2fb"

FONT = "Microsoft YaHei UI"
CHANNEL_ORDER = ("qoder", "codebuddy", "zcode", "gpt")
REALM_CHOICES = {
    "qoder": [("cn", "国内版"), ("intl", "国际版")],
    "codebuddy": [("cn", "国内版"), ("global", "国际版")],
    "zcode": [("zai", "Z.AI"), ("bigmodel", "BigModel")],
}


def _font(size=10, bold=False):
    return (FONT, size, "bold" if bold else "normal")


def _fmt_num(value):
    try:
        return "{:,}".format(int(value or 0))
    except Exception:
        return str(value or 0)


def _fmt_compact(value):
    try:
        n = int(value or 0)
    except Exception:
        return str(value or 0)
    if n >= 100000000:
        return "%.1f亿" % (n / 100000000.0)
    if n >= 10000:
        return "%.1f万" % (n / 10000.0)
    return "{:,}".format(n)


def _fmt_time(ts):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "—"


def _fmt_uptime(seconds):
    try:
        seconds = int(seconds or 0)
    except Exception:
        seconds = 0
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return "%d 天 %d 小时" % (days, hours)
    if hours:
        return "%d 小时 %d 分" % (hours, minutes)
    return "%d 分钟" % max(1, minutes)


def _credits_text(account):
    credits = account.get("credits") or {}
    if not isinstance(credits, dict):
        return "未刷新"
    if credits.get("unlimited"):
        return "不限量"
    remain = credits.get("remain")
    size = credits.get("size")
    if remain is None:
        return "未刷新"
    if size:
        return "%s / %s" % (_fmt_compact(remain), _fmt_compact(size))
    return _fmt_compact(remain)


def _channel_label(name):
    return CHANNEL_META.get(name, {}).get("label", name)


def _realm_label(channel, realm):
    for code, label in REALM_CHOICES.get(channel, []):
        if code == realm:
            return label
    return realm or ""


class ScrollFrame(tk.Frame):
    """Vertically scrollable page container."""

    def __init__(self, parent, bg=BG):
        tk.Frame.__init__(self, parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical",
                                  command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._sync_bar)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window((0, 0), window=self.inner,
                                                anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _on_inner(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)

    def _sync_bar(self, first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            if self.vbar.winfo_ismapped():
                self.vbar.pack_forget()
        elif not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y")
        self.vbar.set(first, last)

    def _wheel(self, event):
        widget = event.widget
        if widget.winfo_class() in ("Text", "Treeview", "Listbox"):
            return
        while widget is not None:
            if widget is self:
                self.canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
                return
            widget = getattr(widget, "master", None)


class ControlPanel(tk.Tk):
    def __init__(self, runtime):
        tk.Tk.__init__(self)
        self.runtime = runtime
        self.service = GatewayService(runtime)
        self._accounts = []
        self._account_sig = None
        self._events_sig = None
        self._section_key = "overview"
        self._closed = False
        self._toast_job = None
        self._last_tick = 0.0
        self._service_stopping = False

        self.title("Ruodao2API 控制台")
        self.configure(bg=BG)
        left, top, area_w, area_h = self._work_area()
        width = min(1180, max(920, area_w - 60))
        height = min(760, max(600, area_h - 70))
        self.minsize(min(980, width), min(620, height))
        self._center(width, height)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._configure_style()
        self._build_layout()
        self._switch("overview")
        self.after(120, self._start_service)
        self.after(400, self._refresh_tick)
        self.after(3000, self._loop)

    # -- window -----------------------------------------------------------
    @staticmethod
    def _work_area():
        try:
            import ctypes
            import ctypes.wintypes
            rect = ctypes.wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):
                return (rect.left, rect.top, rect.right - rect.left,
                        rect.bottom - rect.top)
        except Exception:
            pass
        return (0, 0, 1280, 720)

    def _center(self, width, height):
        left, top, area_w, area_h = self._work_area()
        x = left + max(8, (area_w - width) // 2)
        y = top + max(8, (area_h - height - 34) // 2)
        self.geometry("%dx%d+%d+%d" % (width, height, x, y))

    def _configure_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background=WHITE, fieldbackground=WHITE,
                        foreground=TEXT, font=_font(10), rowheight=34,
                        borderwidth=0)
        style.configure("Treeview.Heading", background="#fafbfc",
                        foreground=MUTED, font=_font(9, True),
                        relief="flat", padding=(6, 8))
        style.map("Treeview", background=[("selected", PRIMARY_SOFT)],
                  foreground=[("selected", TEXT)])
        style.map("Treeview.Heading", background=[("active", "#f2f4f7")])
        style.configure("TCombobox", padding=4, fieldbackground=WHITE)
        style.configure("TEntry", padding=4, fieldbackground=WHITE)

    # -- layout -----------------------------------------------------------
    def _build_layout(self):
        self._build_sidebar()
        self.main = tk.Frame(self, bg=BG)
        self.main.pack(side="left", fill="both", expand=True)
        self._build_header()
        self._build_footer()
        self.body = tk.Frame(self.main, bg=BG)
        self.body.pack(fill="both", expand=True, padx=26, pady=(0, 10))
        self.sections = {}
        self._build_overview()
        self._build_accounts()
        self._build_logs()
        self._build_settings()

    def _build_sidebar(self):
        side = tk.Frame(self, bg=WHITE, width=214)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.sidebar = side

        brand = tk.Frame(side, bg=WHITE)
        brand.pack(fill="x", padx=18, pady=(22, 26))
        logo = tk.Canvas(brand, width=40, height=40, bg=WHITE,
                         highlightthickness=0)
        logo.pack(side="left")
        logo.create_oval(2, 2, 38, 38, fill=PRIMARY, outline="")
        logo.create_text(20, 20, text="R", fill=WHITE, font=_font(18, True))
        text = tk.Frame(brand, bg=WHITE)
        text.pack(side="left", padx=(10, 0))
        tk.Label(text, text="Ruodao2API", bg=WHITE, fg=TEXT,
                 font=_font(12, True), anchor="w").pack(anchor="w")
        tk.Label(text, text="本地 AI 聚合网关", bg=WHITE, fg=MUTED,
                 font=_font(8), anchor="w").pack(anchor="w")

        self.nav_buttons = {}
        for key, label in (("overview", "概览"), ("accounts", "账号"),
                           ("logs", "日志"), ("settings", "设置")):
            btn = tk.Button(side, text=label, anchor="w", justify="left",
                            font=_font(10), bg=WHITE, fg=MUTED,
                            activebackground=PRIMARY_SOFT,
                            activeforeground=PRIMARY, relief="flat", bd=0,
                            padx=24, pady=10, cursor="hand2",
                            command=lambda k=key: self._switch(k))
            btn.pack(fill="x", pady=1)
            self.nav_buttons[key] = btn

        bottom = tk.Frame(side, bg=WHITE)
        bottom.pack(side="bottom", fill="x", padx=16, pady=16)
        tk.Frame(bottom, bg=BORDER, height=1).pack(fill="x", pady=(0, 14))
        status_row = tk.Frame(bottom, bg=WHITE)
        status_row.pack(fill="x")
        self.dot = tk.Canvas(status_row, width=10, height=10, bg=WHITE,
                             highlightthickness=0)
        self.dot.pack(side="left")
        self.side_status = tk.Label(status_row, text="正在启动", bg=WHITE,
                                    fg=MUTED, font=_font(9, True), anchor="w")
        self.side_status.pack(side="left", padx=(6, 0))
        self.side_url = tk.Label(bottom, text=self.service.url, bg=WHITE,
                                 fg=MUTED, font=_font(8), anchor="w")
        self.side_url.pack(fill="x", pady=(4, 10))
        self.web_btn = self._button(bottom, "打开网页面板", self._open_web,
                                    "primary")
        self.web_btn.pack(fill="x", pady=(0, 6))
        self.service_btn = self._button(bottom, "停止服务",
                                        self._toggle_service, "secondary",
                                        small=True)
        self.service_btn.pack(fill="x")

    def _build_header(self):
        header = tk.Frame(self.main, bg=BG)
        header.pack(fill="x", padx=26, pady=(20, 12))
        left = tk.Frame(header, bg=BG)
        left.pack(side="left")
        self.title_label = tk.Label(left, text="概览", bg=BG, fg=TEXT,
                                    font=_font(18, True), anchor="w")
        self.title_label.pack(anchor="w")
        self.subtitle_label = tk.Label(left, text="", bg=BG, fg=MUTED,
                                       font=_font(9), anchor="w")
        self.subtitle_label.pack(anchor="w", pady=(3, 0))
        right = tk.Frame(header, bg=BG)
        right.pack(side="right")
        self.header_hint = tk.Label(right, text="", bg=BG, fg=MUTED,
                                    font=_font(9))
        self.header_hint.pack(side="left", padx=(0, 10))
        self._button(right, "刷新", lambda: self._refresh_now(True),
                     "secondary", small=True).pack(side="left")

    def _build_footer(self):
        footer = tk.Frame(self.main, bg=BG, height=28)
        footer.pack(side="bottom", fill="x", padx=26)
        self.footer_text = tk.Label(footer, text="就绪", bg=BG, fg=MUTED,
                                    font=_font(8), anchor="w")
        self.footer_text.pack(side="left")
        self.footer_version = tk.Label(footer, text="v0.1.0", bg=BG, fg=MUTED,
                                       font=_font(8), anchor="e")
        self.footer_version.pack(side="right")

    # -- widgets ----------------------------------------------------------
    def _button(self, parent, text, command, kind="secondary", small=False):
        palette = {
            "primary": (PRIMARY, WHITE, PRIMARY_DARK),
            "secondary": (WHITE, TEXT, "#f3f4f6"),
            "ghost": (BG, MUTED, "#eceef2"),
            "danger": (WHITE, RED, RED_SOFT),
        }
        bg, fg, active = palette.get(kind, palette["secondary"])
        options = {
            "text": text, "command": command, "font": _font(9 if small else 10),
            "bg": bg, "fg": fg, "activebackground": active,
            "activeforeground": fg, "relief": "flat", "bd": 0,
            "padx": 12 if small else 16, "pady": 5 if small else 8,
            "cursor": "hand2",
        }
        if kind == "secondary":
            options.update(highlightthickness=1, highlightbackground=BORDER,
                           highlightcolor=BORDER)
        else:
            options.update(highlightthickness=0)
        return tk.Button(parent, **options)

    def _card(self, parent, **kwargs):
        return tk.Frame(parent, bg=WHITE, highlightbackground=BORDER,
                        highlightthickness=1, bd=0, **kwargs)

    def _toast(self, text, error=False):
        self.footer_text.config(text=text, fg=RED if error else MUTED)
        if self._toast_job:
            try:
                self.after_cancel(self._toast_job)
            except Exception:
                pass
        self._toast_job = self.after(6000, lambda: self.footer_text.config(
            text="就绪", fg=MUTED))

    def _copy(self, text, label="已复制"):
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(str(text))
        self.update()
        self._toast(label)

    # -- sections ---------------------------------------------------------
    def _section(self, key, scrollable=False):
        if scrollable:
            frame = ScrollFrame(self.body)
            self.sections[key] = frame
            return frame.inner
        frame = tk.Frame(self.body, bg=BG)
        self.sections[key] = frame
        return frame

    def _switch(self, key):
        titles = {
            "overview": ("概览", "服务状态、账号额度与快速接入"),
            "accounts": ("账号池", "添加、启停与维护各渠道账号"),
            "logs": ("运行日志", "签到、刷新与上游请求记录"),
            "settings": ("设置", "API 接入、自动化与本地参数"),
        }
        self._section_key = key
        for name, frame in self.sections.items():
            if name == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for name, btn in self.nav_buttons.items():
            active = name == key
            btn.config(bg=PRIMARY_SOFT if active else WHITE,
                       fg=PRIMARY if active else MUTED,
                       font=_font(10, active))
        title, subtitle = titles.get(key, ("", ""))
        self.title_label.config(text=title)
        self.subtitle_label.config(text=subtitle)
        if key == "logs":
            self._render_logs(True)
        elif key == "settings":
            self._load_settings()
        elif key == "accounts":
            self._render_accounts()

    # -- overview ---------------------------------------------------------
    def _build_overview(self):
        view = self._section("overview", scrollable=True)
        hero = self._card(view)
        hero.pack(fill="x")
        left = tk.Frame(hero, bg=WHITE)
        left.pack(side="left", fill="both", expand=True, padx=26, pady=22)
        tk.Label(left, text="LOCAL AI GATEWAY", bg=WHITE, fg=PRIMARY,
                 font=_font(8, True), anchor="w").pack(anchor="w")
        tk.Label(left, text="一个入口，接上你的全部额度", bg=WHITE, fg=TEXT,
                 font=_font(17, True), anchor="w").pack(anchor="w", pady=(4, 6))
        tk.Label(left, text="把 Qoder、WorkBuddy、ZCode 和任意 OpenAI 兼容服务聚合为本地接口，"
                           "供本机 Agent 使用。", bg=WHITE, fg=MUTED, font=_font(9),
                 justify="left", anchor="w", wraplength=430).pack(anchor="w")
        actions = tk.Frame(left, bg=WHITE)
        actions.pack(anchor="w", pady=(16, 0))
        self._button(actions, "打开网页面板", self._open_web,
                     "primary").pack(side="left")
        self._button(actions, "立即签到", self._checkin_all,
                     "secondary").pack(side="left", padx=8)
        self._button(actions, "刷新余额", self._refresh_balances,
                     "secondary").pack(side="left")

        panel = tk.Frame(hero, bg="#fafafc", width=410)
        panel.pack(side="right", fill="y", padx=18, pady=18)
        panel.pack_propagate(False)
        tk.Label(panel, text="接口地址", bg="#fafafc", fg=MUTED,
                 font=_font(9, True), anchor="w").pack(fill="x", padx=8, pady=(4, 8))
        for label, path in (("OpenAI", "/v1"), ("Anthropic", ""),
                            ("Responses", "/v1")):
            row = tk.Frame(panel, bg="#fafafc")
            row.pack(fill="x", padx=8, pady=3)
            tk.Label(row, text=label, width=9, bg="#fafafc", fg=MUTED,
                     font=_font(8), anchor="w").pack(side="left")
            value = tk.Label(row, text=self.service.url + path, bg="#fafafc",
                             fg=TEXT, font=("Consolas", 9), anchor="w")
            value.pack(side="left", fill="x", expand=True)
            self._button(row, "复制", lambda v=value.cget("text"): self._copy(v),
                         "ghost", small=True).pack(side="right")
        key_row = tk.Frame(panel, bg="#fafafc")
        key_row.pack(fill="x", padx=8, pady=(9, 2))
        tk.Label(key_row, text="API Key", width=9, bg="#fafafc", fg=MUTED,
                 font=_font(8), anchor="w").pack(side="left")
        self.hero_key = tk.Entry(key_row, show="*", relief="flat", bd=0,
                                 bg="#eef0f4", fg=TEXT, font=("Consolas", 9),
                                 readonlybackground="#eef0f4")
        self.hero_key.pack(side="left", fill="x", expand=True, padx=(0, 6),
                           ipady=3)
        self.hero_key.config(state="readonly")
        self._button(key_row, "显示", self._toggle_hero_key,
                     "ghost", small=True).pack(side="right")
        self._button(key_row, "复制",
                     lambda: self._copy(self.runtime.api_keys.default_secret(),
                                        "API Key 已复制"),
                     "ghost", small=True).pack(side="right", padx=(0, 4))

        stats = tk.Frame(view, bg=BG)
        stats.pack(fill="x", pady=14)
        for i in range(4):
            stats.grid_columnconfigure(i, weight=1, uniform="stat")
        self.stat_accounts = self._stat_card(stats, 0, "可用账号", "全渠道账号池")
        self.stat_requests = self._stat_card(stats, 1, "近 24h 请求", "本机调用次数")
        self.stat_tokens = self._stat_card(stats, 2, "Token 消耗", "输入 + 输出")
        self.stat_rate = self._stat_card(stats, 3, "成功率", "近 24 小时")

        channels = tk.Frame(view, bg=BG)
        channels.pack(fill="x", pady=(0, 14))
        for i in range(4):
            channels.grid_columnconfigure(i, weight=1, uniform="channel")
        self.channel_slots = {}
        for i, name in enumerate(CHANNEL_ORDER):
            self.channel_slots[name] = self._channel_card(channels, i, name)

        events_card = self._card(view)
        events_card.pack(fill="both", expand=True)
        head = tk.Frame(events_card, bg=WHITE)
        head.pack(fill="x", padx=18, pady=(12, 6))
        tk.Label(head, text="最近动态", bg=WHITE, fg=TEXT,
                 font=_font(10, True)).pack(side="left")
        self._button(head, "查看全部", lambda: self._switch("logs"),
                     "ghost", small=True).pack(side="right")
        self.event_preview = tk.Frame(events_card, bg=WHITE)
        self.event_preview.pack(fill="both", expand=True, padx=18,
                                pady=(0, 12))

    def _stat_card(self, parent, column, label, sub):
        card = self._card(parent)
        card.grid(row=0, column=column, sticky="nsew",
                  padx=(0 if column == 0 else 7, 0))
        tk.Label(card, text=label, bg=WHITE, fg=MUTED, font=_font(9),
                 anchor="w").pack(anchor="w", padx=16, pady=(12, 2))
        value = tk.Label(card, text="—", bg=WHITE, fg=TEXT,
                         font=_font(19, True), anchor="w")
        value.pack(anchor="w", padx=16)
        sub_label = tk.Label(card, text=sub, bg=WHITE, fg=MUTED, font=_font(8),
                             anchor="w")
        sub_label.pack(anchor="w", padx=16, pady=(2, 12))
        return value, sub_label

    def _channel_card(self, parent, column, name):
        meta = CHANNEL_META.get(name, {})
        card = self._card(parent)
        card.grid(row=0, column=column, sticky="nsew",
                  padx=(0 if column == 0 else 7, 0))
        top = tk.Frame(card, bg=WHITE)
        top.pack(fill="x", padx=14, pady=(12, 0))
        logo = tk.Canvas(top, width=28, height=28, bg=WHITE,
                         highlightthickness=0)
        logo.pack(side="left")
        logo.create_oval(1, 1, 27, 27, fill=meta.get("color", MUTED),
                         outline="")
        logo.create_text(14, 14, text=name[:1].upper(), fill=WHITE,
                         font=_font(10, True))
        tk.Label(top, text=meta.get("label", name), bg=WHITE, fg=TEXT,
                 font=_font(9, True)).pack(side="left", padx=(7, 0))
        pill = tk.Label(top, text="未配置", bg=BG, fg=MUTED, font=_font(8),
                        padx=7, pady=1)
        pill.pack(side="right")
        detail = tk.Label(card, text="0 个账号", bg=WHITE, fg=MUTED,
                          font=_font(8), anchor="w")
        detail.pack(fill="x", padx=14, pady=(6, 9))
        buttons = tk.Frame(card, bg=WHITE)
        buttons.pack(fill="x", padx=12, pady=(0, 11))
        self._button(buttons, "添加",
                     lambda n=name: self._open_add(n),
                     "secondary", small=True).pack(side="left")
        self._button(buttons, "管理",
                     lambda n=name: self._goto_accounts(n),
                     "ghost", small=True).pack(side="left", padx=5)
        return {"pill": pill, "detail": detail}

    # -- accounts ---------------------------------------------------------
    def _build_accounts(self):
        view = self._section("accounts")
        toolbar = tk.Frame(view, bg=BG)
        toolbar.pack(fill="x", pady=(0, 10))
        tk.Label(toolbar, text="筛选", bg=BG, fg=MUTED,
                 font=_font(9)).pack(side="left", padx=(0, 6))
        self.filter_var = tk.StringVar(value="全部渠道")
        self.filter_box = ttk.Combobox(
            toolbar, textvariable=self.filter_var, state="readonly", width=14,
            values=["全部渠道"] + [_channel_label(n) for n in CHANNEL_ORDER])
        self.filter_box.pack(side="left")
        self.filter_box.bind("<<ComboboxSelected>>",
                             lambda _e: self._render_accounts())
        self._button(toolbar, "添加账号", lambda: self._open_add(None),
                     "primary", small=True).pack(side="left", padx=8)
        self._button(toolbar, "签到", self._checkin_selected,
                     "secondary", small=True).pack(side="left", padx=(0, 6))
        self._button(toolbar, "刷新", self._refresh_selected,
                     "secondary", small=True).pack(side="left", padx=(0, 6))
        self._button(toolbar, "测试", self._test_selected,
                     "secondary", small=True).pack(side="left", padx=(0, 6))
        self.account_hint = tk.Label(toolbar, text="双击查看详情", bg=BG,
                                     fg=MUTED, font=_font(8))
        self.account_hint.pack(side="right")

        card = self._card(view)
        card.pack(fill="both", expand=True)
        columns = ("channel", "name", "status", "credits", "expires", "updated")
        headings = {"channel": "渠道", "name": "名称", "status": "状态",
                    "credits": "剩余额度", "expires": "到期", "updated": "最近使用"}
        widths = {"channel": 130, "name": 190, "status": 120,
                  "credits": 170, "expires": 140, "updated": 140}
        wrap = tk.Frame(card, bg=WHITE)
        wrap.pack(fill="both", expand=True, padx=1, pady=1)
        self.account_tree = ttk.Treeview(wrap, columns=columns, show="headings",
                                         selectmode="browse")
        for col in columns:
            self.account_tree.heading(col, text=headings[col])
            self.account_tree.column(col, width=widths[col],
                                     anchor="w" if col != "credits" else "e",
                                     stretch=True)
        scroll = ttk.Scrollbar(wrap, orient="vertical",
                               command=self.account_tree.yview)
        self.account_tree.configure(yscrollcommand=scroll.set)
        self.account_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.account_tree.tag_configure("ready", foreground=GREEN)
        self.account_tree.tag_configure("disabled", foreground=MUTED)
        self.account_tree.tag_configure("cooling", foreground=AMBER)
        self.account_tree.tag_configure("expired", foreground=RED)
        self.account_tree.tag_configure("warn", foreground=AMBER)
        self.account_tree.bind("<Double-1>", lambda _e: self._show_detail())
        self.account_tree.bind("<Return>", lambda _e: self._show_detail())

        actions = tk.Frame(view, bg=BG)
        actions.pack(fill="x", pady=(8, 0))
        self._button(actions, "详情", self._show_detail,
                     "secondary", small=True).pack(side="left")
        self._button(actions, "启用 / 停用", self._toggle_selected,
                     "secondary", small=True).pack(side="left", padx=6)
        self._button(actions, "删除", self._delete_selected,
                     "danger", small=True).pack(side="left")
        self._button(actions, "在网页面板中登录 / 本地导入", self._open_web,
                     "ghost", small=True).pack(side="right")

    def _selected_id(self):
        selection = self.account_tree.selection()
        return selection[0] if selection else ""

    def _selected_account(self):
        account_id = self._selected_id()
        if not account_id:
            messagebox.showinfo("提示", "请先在列表中选择一个账号。", parent=self)
            return None
        return self.runtime.accounts.get(account_id)

    def _filtered_accounts(self):
        label = self.filter_var.get() if hasattr(self, "filter_var") else "全部渠道"
        channel = ""
        for name in CHANNEL_ORDER:
            if _channel_label(name) == label:
                channel = name
        return [a for a in self._accounts if not channel or a["channel"] == channel]

    def _render_accounts(self):
        if not hasattr(self, "account_tree"):
            return
        selected = self._selected_id()
        items = self._filtered_accounts()
        self.account_tree.delete(*self.account_tree.get_children())
        for acc in items:
            expires = acc.get("expires_at")
            values = (
                acc.get("channel_label") or _channel_label(acc.get("channel")),
                acc.get("name") or "—",
                acc.get("status_label") or "—",
                _credits_text(acc),
                _fmt_time(expires) if expires else "—",
                _fmt_time(acc.get("updated_at")),
            )
            tags = (acc.get("status") or "",)
            if not acc.get("enabled"):
                tags = ("disabled",)
            self.account_tree.insert("", "end", iid=acc["id"], values=values,
                                     tags=tags)
        if selected and self.account_tree.exists(selected):
            self.account_tree.selection_set(selected)
        self.account_hint.config(text="共 %d 个账号 · 双击查看详情" % len(items))

    # -- logs -------------------------------------------------------------
    def _build_logs(self):
        view = self._section("logs")
        toolbar = tk.Frame(view, bg=BG)
        toolbar.pack(fill="x", pady=(0, 10))
        tk.Label(toolbar, text="级别", bg=BG, fg=MUTED,
                 font=_font(9)).pack(side="left", padx=(0, 6))
        self.log_filter = ttk.Combobox(
            toolbar, state="readonly", width=10,
            values=["全部", "信息 INFO", "警告 WARN", "错误 ERROR"])
        self.log_filter.current(0)
        self.log_filter.pack(side="left")
        self.log_filter.bind("<<ComboboxSelected>>",
                             lambda _e: self._render_logs(True))
        self._button(toolbar, "刷新", lambda: self._render_logs(True),
                     "secondary", small=True).pack(side="left", padx=8)
        tk.Label(toolbar, text="签到、刷新和请求记录都会保存在本机 SQLite", bg=BG,
                 fg=MUTED, font=_font(8)).pack(side="right")

        card = self._card(view)
        card.pack(fill="both", expand=True)
        self.log_text = scrolledtext.ScrolledText(
            card, bg=WHITE, fg=TEXT, relief="flat", bd=0,
            font=(FONT, 9), wrap="word", padx=14, pady=10,
            state="disabled", spacing1=2, spacing3=2)
        self.log_text.pack(fill="both", expand=True, padx=1, pady=1)
        self.log_text.tag_configure("time", foreground=MUTED,
                                    font=("Consolas", 9))
        self.log_text.tag_configure("INFO", foreground=BLUE)
        self.log_text.tag_configure("WARN", foreground=AMBER)
        self.log_text.tag_configure("ERROR", foreground=RED)

    def _render_logs(self, force=False):
        if not hasattr(self, "log_text"):
            return
        events = self.runtime.store.events(300)
        label = self.log_filter.get() if hasattr(self, "log_filter") else "全部"
        level = {"全部": "", "信息 INFO": "INFO", "警告 WARN": "WARN",
                 "错误 ERROR": "ERROR"}.get(label, "")
        filtered = [e for e in events if not level or e.get("level") == level]
        signature = (len(events), level,
                     events[0]["id"] if events else 0)
        if not force and signature == self._events_sig:
            return
        self._events_sig = signature
        at_bottom = True
        try:
            at_bottom = self.log_text.yview()[1] > 0.97
        except Exception:
            pass
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        for event in filtered:
            self.log_text.insert("end", time.strftime(
                "%Y-%m-%d %H:%M:%S  ",
                time.localtime(float(event.get("ts") or 0))), "time")
            level_name = str(event.get("level") or "INFO")
            self.log_text.insert("end", "%-6s" % level_name, level_name)
            self.log_text.insert("end", "  " + str(event.get("message") or "") + "\n")
        self.log_text.config(state="disabled")
        if at_bottom:
            self.log_text.see("end")
        self._render_event_preview(events)

    def _render_event_preview(self, events=None):
        if not hasattr(self, "event_preview"):
            return
        events = events or self.runtime.store.events(8)
        for child in self.event_preview.winfo_children():
            child.destroy()
        if not events:
            tk.Label(self.event_preview, text="暂无动态。添加账号、签到或调用接口后会显示在这里。",
                     bg=WHITE, fg=MUTED, font=_font(9), anchor="w").pack(
                fill="x", pady=8)
            return
        for event in events[:5]:
            row = tk.Frame(self.event_preview, bg=WHITE)
            row.pack(fill="x", pady=2)
            level = str(event.get("level") or "INFO")
            color = {"ERROR": RED, "WARN": AMBER}.get(level, BLUE)
            tk.Label(row, text=time.strftime("%H:%M",
                                             time.localtime(float(event.get("ts") or 0))),
                     bg=WHITE, fg=MUTED, font=("Consolas", 8),
                     width=6, anchor="w").pack(side="left")
            tk.Label(row, text=level, bg=WHITE, fg=color, font=_font(8, True),
                     width=6, anchor="w").pack(side="left")
            message = str(event.get("message") or "").replace("\n", " ")
            tk.Label(row, text=message, bg=WHITE, fg=TEXT, font=_font(9),
                     anchor="w", justify="left").pack(side="left", fill="x",
                                                       expand=True)

    # -- settings ---------------------------------------------------------
    def _build_settings(self):
        view = self._section("settings", scrollable=True)
        columns = tk.Frame(view, bg=BG)
        columns.pack(fill="both", expand=True)
        columns.grid_columnconfigure(0, weight=1, uniform="col")
        columns.grid_columnconfigure(1, weight=1, uniform="col")
        left = tk.Frame(columns, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(columns, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        key_card = self._card(left)
        key_card.pack(fill="x", pady=(0, 14))
        tk.Label(key_card, text="API 接入", bg=WHITE, fg=TEXT,
                 font=_font(10, True), anchor="w").pack(
            fill="x", padx=18, pady=(14, 2))
        tk.Label(key_card, text="所有推理接口都使用这个 Key，仅保存在本机。",
                 bg=WHITE, fg=MUTED, font=_font(8), anchor="w").pack(
            fill="x", padx=18)
        key_row = tk.Frame(key_card, bg=WHITE)
        key_row.pack(fill="x", padx=18, pady=(10, 8))
        self.settings_key = tk.Entry(key_row, show="*", relief="flat", bd=0,
                                     bg="#f2f4f7", fg=TEXT,
                                     font=("Consolas", 9),
                                     readonlybackground="#f2f4f7")
        self.settings_key.pack(side="left", fill="x", expand=True, ipady=5)
        self.settings_key.config(state="readonly")
        self._button(key_row, "显示", self._toggle_settings_key,
                     "ghost", small=True).pack(side="right", padx=(6, 0))
        self._button(key_row, "复制",
                     lambda: self._copy(self.runtime.api_keys.default_secret(),
                                        "API Key 已复制"),
                     "ghost", small=True).pack(side="right", padx=(6, 0))
        self._button(key_row, "轮换", self._rotate_key,
                     "secondary", small=True).pack(side="right", padx=(6, 0))
        base_row = tk.Frame(key_card, bg=WHITE)
        base_row.pack(fill="x", padx=18, pady=(0, 14))
        tk.Label(base_row, text="Base URL", bg=WHITE, fg=MUTED,
                 font=_font(8)).pack(side="left")
        tk.Label(base_row, text=self.service.url, bg=WHITE, fg=TEXT,
                 font=("Consolas", 9)).pack(side="left", padx=(8, 6))
        self._button(base_row, "复制", lambda: self._copy(
            self.service.url, "Base URL 已复制"), "ghost",
            small=True).pack(side="left")

        access_card = self._card(left)
        access_card.pack(fill="both", expand=True)
        tk.Label(access_card, text="客户端接入", bg=WHITE, fg=TEXT,
                 font=_font(10, True), anchor="w").pack(
            fill="x", padx=18, pady=(14, 8))
        for name, url in (("OpenAI", self.service.url + "/v1"),
                          ("Anthropic", self.service.url),
                          ("Responses", self.service.url + "/v1"),
                          ("模型列表", self.service.url + "/v1/models")):
            row = tk.Frame(access_card, bg=WHITE)
            row.pack(fill="x", padx=18, pady=3)
            tk.Label(row, text=name, width=8, bg=WHITE, fg=MUTED,
                     font=_font(8), anchor="w").pack(side="left")
            tk.Label(row, text=url, bg=WHITE, fg=TEXT,
                     font=("Consolas", 8), anchor="w").pack(
                side="left", fill="x", expand=True)
            self._button(row, "复制", lambda u=url: self._copy(
                u, "地址已复制"), "ghost", small=True).pack(side="right")
        tk.Label(access_card, text="Claude Code / Codex 等客户端可直接使用上方对应地址；"
                                   "鉴权使用同一 API Key。", bg=WHITE, fg=MUTED,
                 font=_font(8), anchor="w", justify="left",
                 wraplength=380).pack(fill="x", padx=18, pady=(8, 14))

        auto_card = self._card(right)
        auto_card.pack(fill="x", pady=(0, 14))
        tk.Label(auto_card, text="自动化任务", bg=WHITE, fg=TEXT,
                 font=_font(10, True), anchor="w").pack(
            fill="x", padx=18, pady=(14, 8))
        self.checkin_var = tk.BooleanVar(value=True)
        self.scheduler_var = tk.BooleanVar(value=True)
        for text, var in (("每日自动签到", self.checkin_var),
                          ("后台定时任务（刷新与余额同步）", self.scheduler_var)):
            tk.Checkbutton(auto_card, text=text, variable=var, bg=WHITE,
                           fg=TEXT, activebackground=WHITE, font=_font(9),
                           anchor="w", highlightthickness=0,
                           selectcolor=WHITE).pack(fill="x", padx=16)
        form = tk.Frame(auto_card, bg=WHITE)
        form.pack(fill="x", padx=18, pady=(8, 4))
        self.checkin_times_var = tk.StringVar()
        self.refresh_time_var = tk.StringVar()
        self.balance_minutes_var = tk.StringVar()
        self._settings_field(form, "签到时间", self.checkin_times_var,
                             "多个时间用逗号分隔，24 小时制")
        self._settings_field(form, "每日强制刷新", self.refresh_time_var,
                             "HH:MM")
        self._settings_field(form, "余额刷新间隔", self.balance_minutes_var,
                             "分钟，最小 5")
        self._button(auto_card, "保存自动化设置", self._save_settings,
                     "primary", small=True).pack(anchor="w", padx=18,
                                                 pady=(6, 14))

        runtime_card = self._card(right)
        runtime_card.pack(fill="x", pady=(0, 14))
        tk.Label(runtime_card, text="运行参数", bg=WHITE, fg=TEXT,
                 font=_font(10, True), anchor="w").pack(
            fill="x", padx=18, pady=(14, 6))
        info = tk.Frame(runtime_card, bg=WHITE)
        info.pack(fill="x", padx=18)
        self.runtime_info = {}
        for key, label in (("listen", "监听地址"), ("uptime", "运行时长"),
                           ("data", "数据目录"), ("accounts", "账号")):
            row = tk.Frame(info, bg=WHITE)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, width=9, bg=WHITE, fg=MUTED,
                     font=_font(8), anchor="w").pack(side="left")
            value = tk.Label(row, text="—", bg=WHITE, fg=TEXT, font=_font(8),
                             anchor="w", justify="left")
            value.pack(side="left", fill="x", expand=True)
            self.runtime_info[key] = value
        form2 = tk.Frame(runtime_card, bg=WHITE)
        form2.pack(fill="x", padx=18, pady=(8, 4))
        self.timeout_var = tk.StringVar()
        self.body_mb_var = tk.StringVar()
        self._settings_field(form2, "请求超时", self.timeout_var, "秒")
        self._settings_field(form2, "最大请求体", self.body_mb_var, "MB")
        buttons = tk.Frame(runtime_card, bg=WHITE)
        buttons.pack(fill="x", padx=18, pady=(4, 14))
        self._button(buttons, "保存运行参数", self._save_runtime,
                     "secondary", small=True).pack(side="left")
        self._button(buttons, "打开数据目录", self._open_data_dir,
                     "ghost", small=True).pack(side="left", padx=6)

        tips = self._card(right)
        tips.pack(fill="both", expand=True)
        tk.Label(tips, text="安全提示", bg=WHITE, fg=TEXT,
                 font=_font(10, True), anchor="w").pack(
            fill="x", padx=18, pady=(14, 6))
        tk.Label(tips, text="• 服务默认只监听 127.0.0.1，不对局域网开放。\n"
                            "• 账号凭证仅保存在本机数据库，界面和日志均已脱敏。\n"
                            "• OAuth 扫码与本地客户端导入在网页面板中完成。\n"
                            "• 修改监听端口请编辑 data/config.json 后重启。",
                 bg=WHITE, fg=MUTED, font=_font(8), justify="left",
                 anchor="w", wraplength=380).pack(fill="x", padx=18,
                                                   pady=(0, 14))

    def _settings_field(self, parent, label, variable, hint):
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, width=12, bg=WHITE, fg=TEXT,
                 font=_font(9), anchor="w").pack(side="left")
        entry = ttk.Entry(row, textvariable=variable, width=16)
        entry.pack(side="left")
        tk.Label(row, text=hint, bg=WHITE, fg=MUTED,
                 font=_font(8)).pack(side="left", padx=(8, 0))

    def _load_settings(self):
        config = self.runtime.config
        key = util.mask_secret(self.runtime.api_keys.default_secret(), 10, 4)
        self.settings_key.config(state="normal")
        self.settings_key.delete(0, "end")
        self.settings_key.insert(0, key)
        self.settings_key.config(show="*")
        self.settings_key.config(state="readonly")
        self.hero_key.config(state="normal")
        self.hero_key.delete(0, "end")
        self.hero_key.insert(0, key)
        self.hero_key.config(show="*")
        self.hero_key.config(state="readonly")
        self.checkin_var.set(bool(config.get("checkin_enabled", True)))
        self.scheduler_var.set(bool(config.get("scheduler_enabled", True)))
        times = config.get("checkin_times") or []
        self.checkin_times_var.set(", ".join(str(t) for t in times))
        self.refresh_time_var.set(str(config.get("refresh_time") or "22:00"))
        self.balance_minutes_var.set(str(config.get("credits_refresh_minutes") or 30))
        self.timeout_var.set(str(config.get("request_timeout") or 600))
        self.body_mb_var.set(str(config.get("max_body_mb") or 32))
        self.runtime_info["listen"].config(text="%s:%d" % (config.host, config.port))
        self.runtime_info["data"].config(text=str(DATA_DIR))
        summary = self.runtime.accounts.summary()
        self.runtime_info["accounts"].config(
            text="%d 个（%d 可用）" % (summary.get("total", 0),
                                     summary.get("ready", 0)))
        self.runtime_info["uptime"].config(
            text=_fmt_uptime(time.time() - self.runtime.started_at))

    # -- service ----------------------------------------------------------
    def _start_service(self):
        try:
            self.service.start()
            self._set_service_state(True)
        except OSError as exc:
            self._set_service_state(False)
            runtime = self.runtime
            if messagebox.askyesno(
                    "端口被占用",
                    "无法监听 %s：端口可能已被另一个实例占用。\n\n"
                    "是否打开现有网页面板？" % self.service.url,
                    parent=self):
                webbrowser.open(runtime.config.base_url)

    def _set_service_state(self, alive):
        color = GREEN if alive else RED
        if hasattr(self, "dot"):
            self.dot.delete("all")
            self.dot.create_oval(1, 1, 9, 9, fill=color, outline="")
        if hasattr(self, "side_status"):
            self.side_status.config(text="运行中" if alive else "已停止",
                                    fg=GREEN if alive else RED)
        if hasattr(self, "service_btn"):
            self.service_btn.config(text="停止服务" if alive else "启动服务")

    def _toggle_service(self):
        if self.service.alive():
            if not messagebox.askyesno(
                    "停止服务",
                    "停止后本机 Agent 将无法调用接口，确定停止吗？", parent=self):
                return
            self._toast("正在停止服务...")
            try:
                self.service.stop()
            finally:
                if self.runtime.scheduler:
                    self.runtime.scheduler.stop()
                self._set_service_state(False)
                self._toast("服务已停止")
        else:
            try:
                self.service.start()
                if self.runtime.scheduler:
                    self.runtime.scheduler.start()
                self._set_service_state(True)
                self._toast("服务已启动")
            except OSError as exc:
                self._set_service_state(False)
                messagebox.showerror("启动失败",
                                     "无法监听 %s：%s" % (self.service.url, exc),
                                     parent=self)

    def _open_web(self, channel=None):
        if not self.service.alive():
            try:
                self.service.start()
                if self.runtime.scheduler:
                    self.runtime.scheduler.start()
                self._set_service_state(True)
            except OSError as exc:
                messagebox.showerror("服务未启动", str(exc), parent=self)
                return
        url = self.service.url
        if channel:
            url += "/?add=" + channel
        webbrowser.open(url)

    # -- refresh ----------------------------------------------------------
    def _loop(self):
        if self._closed:
            return
        try:
            self._refresh_tick()
        except Exception as exc:
            self.footer_text.config(text="刷新失败：%s" % exc, fg=RED)
        self.after(3000, self._loop)

    def _refresh_now(self, show_toast=False):
        try:
            self._refresh_tick(force=True)
            if show_toast:
                self._toast("已刷新")
        except Exception as exc:
            self._toast("刷新失败：%s" % exc, True)

    def _refresh_tick(self, force=False):
        self._last_tick = time.time()
        self._accounts = self.runtime.accounts.public_list()
        summary = self.runtime.accounts.summary()
        usage = self.runtime.store.usage_summary()
        alive = self.service.alive()
        self._set_service_state(alive)
        self.side_url.config(text=self.service.url)

        rate = "—"
        if usage.get("requests"):
            rate = "%.1f%%" % (100.0 * (1 - usage.get("errors", 0) /
                                        float(usage["requests"])))
        if hasattr(self, "stat_accounts"):
            self.stat_accounts[0].config(
                text="%d / %d" % (summary.get("ready", 0),
                                  summary.get("total", 0)))
            self.stat_accounts[1].config(
                text="%d 个渠道已接入" % len([c for c in summary.get("channels", {})
                                          if summary["channels"][c].get("total")]))
            self.stat_requests[0].config(text=_fmt_compact(usage.get("requests")))
            self.stat_requests[1].config(
                text="失败 %s 次" % _fmt_compact(usage.get("errors")))
            self.stat_tokens[0].config(text=_fmt_compact(usage.get("total_tokens")))
            self.stat_tokens[1].config(
                text="输入 %s · 输出 %s" % (_fmt_compact(usage.get("prompt_tokens")),
                                          _fmt_compact(usage.get("completion_tokens"))))
            self.stat_rate[0].config(text=rate)
            self.stat_rate[1].config(
                text="统计近 24 小时" if usage.get("requests") else "暂无调用")
        for name, slot in getattr(self, "channel_slots", {}).items():
            count = len([a for a in self._accounts if a["channel"] == name])
            ready = len([a for a in self._accounts
                         if a["channel"] == name and a["status"] == "ready"])
            slot["pill"].config(text="%d 可用" % ready if count else "未配置",
                                bg=GREEN_SOFT if ready else BG,
                                fg=GREEN if ready else MUTED)
            slot["detail"].config(text="%d 个账号 · %s" % (
                count, "已就绪" if count and ready else
                ("待处理" if count else "添加后即可使用")))

        signature = tuple((a["id"], a["status"], a["enabled"], a["updated_at"],
                           _credits_text(a)) for a in self._accounts)
        if force or signature != self._account_sig:
            self._account_sig = signature
            if self._section_key == "accounts":
                self._render_accounts()
        events = self.runtime.store.events(8)
        event_sig = tuple(e["id"] for e in events)
        if force or event_sig != getattr(self, "_event_preview_sig", None):
            self._event_preview_sig = event_sig
            self._render_event_preview(events)
        if self._section_key == "logs":
            self._render_logs(force=False)
        if self._section_key == "settings":
            self.runtime_info["uptime"].config(
                text=_fmt_uptime(time.time() - self.runtime.started_at))
            self.runtime_info["accounts"].config(
                text="%d 个（%d 可用）" % (summary.get("total", 0),
                                         summary.get("ready", 0)))
        self.header_hint.config(
            text="%s · 更新于 %s" % ("服务运行中" if alive else "服务已停止",
                                     time.strftime("%H:%M:%S")))

    # -- account actions --------------------------------------------------
    def _goto_accounts(self, channel):
        for name in CHANNEL_ORDER:
            if name == channel:
                self.filter_var.set(_channel_label(name))
        self._switch("accounts")
        self._render_accounts()

    def _open_add(self, channel=None):
        AddAccountDialog(self, channel or "qoder")

    def _show_detail(self):
        account = self._selected_account()
        if not account:
            return
        view = self.runtime.accounts.public(account)
        dialog = tk.Toplevel(self)
        dialog.title("账号详情 - %s" % (view.get("name") or view.get("id")))
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.resizable(False, False)
        width, height = 620, 560
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 2
        dialog.geometry("%dx%d+%d+%d" % (width, height, max(0, x), max(0, y)))
        dialog.grab_set()

        footer = tk.Frame(dialog, bg=WHITE)
        footer.pack(fill="x", side="bottom")
        self._button(footer, "复制账号 ID",
                     lambda: self._copy(view.get("id"), "账号 ID 已复制"),
                     "secondary", small=True).pack(side="left", padx=18,
                                                   pady=12)
        self._button(footer, "关闭", dialog.destroy, "primary",
                     small=True).pack(side="right", padx=18, pady=12)

        header = tk.Frame(dialog, bg=WHITE)
        header.pack(fill="x")
        tk.Label(header, text=view.get("name") or view.get("id"), bg=WHITE,
                 fg=TEXT, font=_font(13, True), anchor="w").pack(
            anchor="w", padx=22, pady=(16, 2))
        tk.Label(header, text="%s · %s · %s" % (
            view.get("channel_label"), view.get("realm_label") or "—",
            view.get("status_label")), bg=WHITE, fg=MUTED,
            font=_font(9), anchor="w").pack(anchor="w", padx=22, pady=(0, 14))

        body = tk.Frame(dialog, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)
        fields = [
            ("账号 ID", view.get("id")),
            ("UID", view.get("uid") or "—"),
            ("来源", view.get("source") or "—"),
            ("套餐", view.get("plan") or "—"),
            ("组织", view.get("org_name") or "—"),
            ("到期时间", _fmt_time(view.get("expires_at"))
             if view.get("expires_at") else "—"),
            ("剩余额度", _credits_text(view)),
            ("凭证", view.get("access_token") or "—"),
            ("Refresh Token", "已保存" if view.get("has_refresh_token") else "无"),
            ("最近签到", view.get("last_checkin_message") or
             str(view.get("last_checkin") or "—")),
        ]
        grid = tk.Frame(body, bg=WHITE, highlightbackground=BORDER,
                        highlightthickness=1)
        grid.pack(fill="x")
        for index, (label, value) in enumerate(fields):
            row, col = divmod(index, 2)
            cell = tk.Frame(grid, bg=WHITE)
            cell.grid(row=row, column=col, sticky="ew", padx=16, pady=6)
            tk.Label(cell, text=label, bg=WHITE, fg=MUTED, font=_font(8),
                     anchor="w").pack(anchor="w")
            tk.Label(cell, text=str(value), bg=WHITE, fg=TEXT, font=_font(9),
                     anchor="w", wraplength=260, justify="left").pack(anchor="w")
        grid.grid_columnconfigure(0, weight=1, uniform="detail")
        grid.grid_columnconfigure(1, weight=1, uniform="detail")

        data_card = tk.Frame(body, bg=WHITE, highlightbackground=BORDER,
                             highlightthickness=1)
        data_card.pack(fill="both", expand=True, pady=(10, 0))
        tk.Label(data_card, text="账号数据（敏感字段已隐藏）", bg=WHITE, fg=MUTED,
                 font=_font(8), anchor="w").pack(fill="x", padx=14, pady=(8, 2))
        text = scrolledtext.ScrolledText(data_card, height=9, bg=WHITE,
                                         fg=TEXT, relief="flat", bd=0,
                                         font=("Consolas", 8), wrap="word",
                                         padx=10, pady=6)
        text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        text.insert("1.0", json.dumps(view.get("data") or {}, ensure_ascii=False,
                                      indent=2))
        text.config(state="disabled")

    def _checkin_selected(self):
        account = self._selected_account()
        if not account:
            return
        channel = self.runtime.channels.get(account["channel"])
        if not channel:
            return
        self._run_action(
            "正在签到 %s..." % account.get("name"),
            lambda: channel.checkin(account),
            lambda result: "签到完成：%s" % (account.get("data", {}).get(
                "last_checkin_message") or "成功"),
            save_account=account)

    def _refresh_selected(self):
        account = self._selected_account()
        if not account:
            return
        def work():
            self.runtime.accounts.refresh_account(account, force=True)
            usage = self.runtime.accounts.refresh_balance(account, force=True)
            return usage
        self._run_action("正在刷新 %s..." % account.get("name"), work,
                         lambda _r: "账号已刷新")

    def _test_selected(self):
        account = self._selected_account()
        if not account:
            return
        channel = self.runtime.channels.get(account["channel"])
        if not channel:
            return
        def work():
            return channel.validate_account(account)
        def done(result):
            ok = bool((result or {}).get("ok"))
            detail = (result or {}).get("message") or (result or {}).get("detail") or ""
            if ok:
                self._toast("连接正常" + (("：" + str(detail)) if detail else ""))
            else:
                messagebox.showwarning("测试失败", str(detail or "上游未返回可用结果"),
                                       parent=self)
        self._run_action("正在测试 %s..." % account.get("name"), work, done)

    def _toggle_selected(self):
        account = self._selected_account()
        if not account:
            return
        enabled = not bool(account.get("enabled"))
        self.runtime.accounts.update_fields(account["id"], enabled=enabled)
        self.runtime.log("INFO", "%s账号 %s/%s" % (
            "启用" if enabled else "停用", account.get("channel"),
            account.get("name")))
        self._refresh_now(True)

    def _delete_selected(self):
        account = self._selected_account()
        if not account:
            return
        if not messagebox.askyesno(
                "删除账号",
                "确定删除 %s/%s 吗？凭证将一并从本机数据库移除。"
                % (account.get("channel"), account.get("name")), parent=self):
            return
        self.runtime.accounts.delete(account["id"])
        self._toast("账号已删除")
        self._refresh_now(True)

    def _checkin_all(self):
        if not self.runtime.scheduler:
            return
        scheduler = self.runtime.scheduler
        self._run_action("正在为全部账号签到...", scheduler.run_checkin,
                         lambda r: "签到完成：成功 %d，跳过 %d，失败 %d" % (
                             r.get("checked", 0), r.get("skipped", 0),
                             r.get("failed", 0)))

    def _refresh_balances(self):
        self._run_action("正在刷新全部余额...",
                         lambda: self.runtime.accounts.refresh_all_balances(True),
                         lambda _r: "余额刷新完成")

    def _run_action(self, message, work, done, save_account=None):
        self._toast(message)
        self.config(cursor="watch")
        self.update_idletasks()
        def runner():
            try:
                result = work()
                error = None
            except Exception as exc:
                result = None
                error = exc
            def finish():
                self.config(cursor="")
                try:
                    if error:
                        messagebox.showerror("操作失败", str(error), parent=self)
                    else:
                        if save_account is not None:
                            self.runtime.accounts.save(save_account)
                        if callable(done):
                            done(result)
                        else:
                            self._toast(str(done))
                finally:
                    self._refresh_now(False)
            self.after(0, finish)
        threading.Thread(target=runner, daemon=True).start()

    # -- settings actions -------------------------------------------------
    def _toggle_hero_key(self):
        self._toggle_key_entry(self.hero_key)

    def _toggle_settings_key(self):
        self._toggle_key_entry(self.settings_key)

    def _toggle_key_entry(self, entry):
        entry.config(state="normal")
        secret = self.runtime.api_keys.default_secret()
        if entry.cget("show"):
            entry.delete(0, "end")
            entry.insert(0, secret)
            entry.config(show="")
        else:
            entry.delete(0, "end")
            entry.insert(0, util.mask_secret(secret, 10, 4))
            entry.config(show="*")
        entry.config(state="readonly")

    def _rotate_key(self):
        if not messagebox.askyesno(
                "轮换 API Key",
                "轮换后旧 Key 立即失效，所有客户端都需要更新。确定继续？",
                parent=self):
            return
        key = self.runtime.api_keys.rotate_default()
        self.runtime.log("INFO", "API Key 已轮换")
        self._copy(key, "新 API Key 已复制到剪贴板")
        self._load_settings()

    def _save_settings(self):
        times = [t.strip() for t in self.checkin_times_var.get().replace("，", ",")
                 .split(",") if t.strip()]
        for value in times:
            parts = value.split(":")
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                messagebox.showerror("格式错误", "签到时间格式应为 HH:MM，多个用逗号分隔。",
                                     parent=self)
                return
        refresh = self.refresh_time_var.get().strip()
        if refresh:
            parts = refresh.split(":")
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                messagebox.showerror("格式错误", "每日刷新时间格式应为 HH:MM。",
                                     parent=self)
                return
        try:
            minutes = max(5, int(self.balance_minutes_var.get().strip() or 30))
        except ValueError:
            messagebox.showerror("格式错误", "余额刷新间隔必须是整数分钟。", parent=self)
            return
        self.runtime.config.update({
            "checkin_enabled": bool(self.checkin_var.get()),
            "scheduler_enabled": bool(self.scheduler_var.get()),
            "checkin_times": times,
            "refresh_time": refresh or "22:00",
            "credits_refresh_minutes": minutes,
        })
        if self.runtime.scheduler:
            self.runtime.scheduler.wake()
        self._toast("自动化设置已保存")

    def _save_runtime(self):
        try:
            timeout = int(self.timeout_var.get().strip() or 600)
            max_body = int(self.body_mb_var.get().strip() or 32)
        except ValueError:
            messagebox.showerror("格式错误", "超时与请求体大小必须是整数。", parent=self)
            return
        if timeout < 30 or max_body < 1:
            messagebox.showerror("数值过小", "请求超时应不小于 30 秒，请求体至少 1 MB。",
                                 parent=self)
            return
        self.runtime.config.update({"request_timeout": timeout,
                                    "max_body_mb": max_body})
        self._toast("运行参数已保存")

    def _open_data_dir(self):
        try:
            os.startfile(str(DATA_DIR))
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self)

    # -- close ------------------------------------------------------------
    def _on_close(self):
        if self.service.alive():
            if not messagebox.askyesno(
                    "退出 Ruodao2API",
                    "关闭窗口将停止本地网关，其他应用将无法继续调用。确定退出吗？",
                    parent=self):
                return
        self._closed = True
        try:
            self.service.stop()
        except Exception:
            pass
        try:
            if self.runtime.scheduler:
                self.runtime.scheduler.stop()
        except Exception:
            pass
        self.destroy()


class AddAccountDialog(tk.Toplevel):
    def __init__(self, panel, channel="qoder"):
        tk.Toplevel.__init__(self, panel)
        self.panel = panel
        self.runtime = panel.runtime
        self.channel = channel if channel in CHANNEL_ORDER else "qoder"
        self.vars = {}
        self.title("添加账号")
        self.configure(bg=BG)
        self.transient(panel)
        self.resizable(False, False)
        width, height = 640, 590
        x = panel.winfo_rootx() + (panel.winfo_width() - width) // 2
        y = panel.winfo_rooty() + (panel.winfo_height() - height) // 2
        self.geometry("%dx%d+%d+%d" % (width, height, max(0, x), max(0, y)))
        self.grab_set()
        self._build()
        self._render_fields()

    def _build(self):
        header = tk.Frame(self, bg=WHITE)
        header.pack(fill="x")
        tk.Label(header, text="添加账号", bg=WHITE, fg=TEXT,
                 font=_font(14, True), anchor="w").pack(
            anchor="w", padx=22, pady=(16, 2))
        tk.Label(header, text="选择渠道并填写凭证；敏感信息只保存在本机数据库。",
                 bg=WHITE, fg=MUTED, font=_font(9), anchor="w").pack(
            anchor="w", padx=22, pady=(0, 12))

        chip_row = tk.Frame(self, bg=WHITE)
        chip_row.pack(fill="x", padx=22, pady=(12, 0))
        self.channel_buttons = {}
        for name in CHANNEL_ORDER:
            btn = tk.Button(chip_row, text=_channel_label(name),
                            command=lambda n=name: self._pick(n),
                            font=_font(9), relief="flat", bd=0, padx=12,
                            pady=6, cursor="hand2")
            btn.pack(side="left", padx=(0, 6))
            self.channel_buttons[name] = btn

        body = tk.Frame(self, bg=WHITE)
        body.pack(fill="both", expand=True, padx=22, pady=12)
        self.fields_frame = tk.Frame(body, bg=WHITE)
        self.fields_frame.pack(fill="both", expand=True)
        self.note_label = tk.Label(body, text="", bg="#f8f9fb", fg=MUTED,
                                   font=_font(8), justify="left",
                                   anchor="w", wraplength=560, padx=10,
                                   pady=8)
        self.note_label.pack(fill="x", pady=(10, 0))
        if self.channel in ("qoder", "codebuddy", "zcode"):
            self.web_link = tk.Button(
                body, text="打开网页面板进行 OAuth 登录 / 本地客户端导入",
                command=lambda: self.panel._open_web(self.channel),
                font=_font(9), relief="flat", bd=0, bg=PRIMARY_SOFT,
                fg=PRIMARY, activebackground="#ffe3d8", activeforeground=PRIMARY,
                padx=10, pady=7, cursor="hand2")
            self.web_link.pack(fill="x", pady=(8, 0))
        else:
            self.web_link = None

        footer = tk.Frame(self, bg=WHITE)
        footer.pack(fill="x", side="bottom")
        self.submit_btn = tk.Button(
            footer, text="添加账号", command=self._submit,
            font=_font(10), relief="flat", bd=0, bg=PRIMARY, fg=WHITE,
            activebackground=PRIMARY_DARK, activeforeground=WHITE,
            padx=20, pady=8, cursor="hand2")
        self.submit_btn.pack(side="right", padx=22, pady=14)
        tk.Button(footer, text="取消", command=self.destroy, font=_font(10),
                  relief="flat", bd=0, bg=WHITE, fg=TEXT,
                  highlightthickness=1, highlightbackground=BORDER,
                  padx=18, pady=7, cursor="hand2").pack(side="right",
                                                        padx=(0, 8), pady=14)

    def _pick(self, channel):
        self.channel = channel
        self._render_fields()

    def _entry(self, parent, label, key, show=None, width=34, hint=""):
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, bg=WHITE, fg=TEXT, font=_font(9),
                 width=20, anchor="w").pack(side="left")
        var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=var, width=width,
                          show=show or "")
        entry.pack(side="left", fill="x", expand=True)
        if hint:
            tk.Label(row, text=hint, bg=WHITE, fg=MUTED,
                     font=_font(8)).pack(side="left", padx=(8, 0))
        self.vars[key] = var
        return var

    def _combo(self, parent, label, key, choices):
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, bg=WHITE, fg=TEXT, font=_font(9),
                 width=20, anchor="w").pack(side="left")
        var = tk.StringVar()
        combo = ttk.Combobox(row, textvariable=var, state="readonly",
                             values=[label for _code, label in choices],
                             width=16)
        combo.pack(side="left")
        if choices:
            combo.current(0)
        self.vars[key] = var
        self.vars[key + "_choices"] = choices
        return var

    def _render_fields(self):
        for name, btn in self.channel_buttons.items():
            active = name == self.channel
            btn.config(bg=PRIMARY_SOFT if active else BG,
                       fg=PRIMARY if active else MUTED,
                       font=_font(9, active))
        for child in self.fields_frame.winfo_children():
            child.destroy()
        self.vars = {}
        channel = self.channel
        if channel == "gpt":
            self.fields_frame.pack_configure()
            self._entry(self.fields_frame, "Base URL", "base_url",
                        hint="例如 https://api.openai.com/v1")
            self._entry(self.fields_frame, "API Key", "api_key", show="*")
            self._entry(self.fields_frame, "名称（可选）", "name")
            self._entry(self.fields_frame, "模型列表（可选）", "models",
                        hint="逗号分隔，留空自动获取")
            self.note_label.config(
                text="GPT / OpenAI 兼容渠道：Base URL 应包含 /v1。模型映射与自定义请求头等"
                     "高级选项可在网页面板中配置。")
        elif channel == "qoder":
            self._combo(self.fields_frame, "区域 / 版本", "realm",
                        REALM_CHOICES["qoder"])
            self._entry(self.fields_frame, "Access Token / PAT", "token",
                        show="*")
            self._entry(self.fields_frame, "Refresh Token（可选）", "refresh",
                        show="*")
            self._entry(self.fields_frame, "昵称（可选）", "nickname")
            self.note_label.config(
                text="支持 Qoder Access Token，或 pt- 开头的个人访问令牌（PAT）。"
                     "需要扫码登录时请使用网页面板。")
        elif channel == "codebuddy":
            self._combo(self.fields_frame, "区域 / 版本", "realm",
                        REALM_CHOICES["codebuddy"])
            self._entry(self.fields_frame, "Access Token", "token", show="*")
            self._entry(self.fields_frame, "Refresh Token（可选）", "refresh",
                        show="*")
            self._entry(self.fields_frame, "昵称（可选）", "nickname")
            self.note_label.config(
                text="从 WorkBuddy 客户端或网页登录态获取 Access Token。填写 Refresh Token "
                     "后可自动续期；扫码登录请使用网页面板。")
        else:
            self._combo(self.fields_frame, "区域 / 版本", "realm",
                        REALM_CHOICES["zcode"])
            self._entry(self.fields_frame, "API Key（推荐）", "api_key",
                        show="*", hint="形如 keyId.secret")
            self._entry(self.fields_frame, "OAuth Access Token（可选）", "oauth",
                        show="*")
            self._entry(self.fields_frame, "JWT Token（可选）", "jwt", show="*")
            self._entry(self.fields_frame, "昵称（可选）", "nickname")
            self.note_label.config(
                text="ZCode 推荐直接填写 API Key；JWT 用于余额和签到。OAuth 登录与本地凭证"
                     "导入请使用网页面板。")
        if self.web_link:
            if channel == "gpt":
                self.web_link.pack_forget()
            else:
                self.web_link.config(
                    text="打开网页面板进行 %s 登录 / 本地客户端导入"
                         % _channel_label(channel))
                self.web_link.pack(fill="x", pady=(8, 0))

    def _value(self, key):
        var = self.vars.get(key)
        return var.get().strip() if var else ""

    def _realm(self, key="realm"):
        choices = self.vars.get(key + "_choices") or []
        selected = self._value(key)
        for code, label in choices:
            if label == selected:
                return code
        return choices[0][0] if choices else ""

    def _submit(self):
        channel = self.channel
        try:
            if channel == "gpt":
                base_url = self._value("base_url")
                api_key = self._value("api_key")
                if not base_url or not api_key:
                    raise ValueError("Base URL 和 API Key 不能为空。")
                models = [m.strip() for m in self._value("models")
                          .replace("，", ",").split(",") if m.strip()]
                name = self._value("name")
                realm = "custom"

                def work():
                    return self.runtime.channels["gpt"].add_account(
                        base_url, api_key, name=name, models=models or None,
                        realm=realm)
            elif channel == "qoder":
                token = self._value("token")
                if not token:
                    raise ValueError("请填写 Access Token 或 PAT。")
                realm = self._realm()
                refresh = self._value("refresh")
                nickname = self._value("nickname")
                channel_obj = self.runtime.channels["qoder"]
                if token.startswith("pt-"):
                    def work():
                        return channel_obj.import_pat(token, realm=realm)
                else:
                    def work():
                        return channel_obj.import_token(
                            realm, token, refresh_token=refresh,
                            nickname=nickname, source="manual")
            elif channel == "codebuddy":
                token = self._value("token")
                if not token:
                    raise ValueError("请填写 Access Token。")
                realm = self._realm()
                refresh = self._value("refresh")
                nickname = self._value("nickname")
                channel_obj = self.runtime.channels["codebuddy"]

                def work():
                    return channel_obj.import_token(
                        token, refresh_token=refresh, realm=realm,
                        nickname=nickname, source="manual")
            else:
                api_key = self._value("api_key")
                oauth = self._value("oauth")
                jwt = self._value("jwt")
                if not (api_key or oauth or jwt):
                    raise ValueError("请至少填写 API Key、OAuth Token 或 JWT 中的一种。")
                realm = self._realm()
                nickname = self._value("nickname")
                channel_obj = self.runtime.channels["zcode"]

                def work():
                    return channel_obj.import_token(
                        realm=realm, oauth_access_token=oauth, jwt_token=jwt,
                        api_key=api_key, nickname=nickname, source="manual")
        except ValueError as exc:
            messagebox.showerror("无法添加", str(exc), parent=self)
            return

        self.submit_btn.config(state="disabled", text="正在添加...")
        self.config(cursor="watch")
        self.update_idletasks()

        def runner():
            try:
                account = work()
                error = None
            except Exception as exc:
                account = None
                error = exc
            def finish():
                self.config(cursor="")
                if error:
                    self.submit_btn.config(state="normal", text="添加账号")
                    messagebox.showerror("添加失败", str(error), parent=self)
                    return
                view = self.runtime.accounts.public(account)
                self.panel._refresh_now(False)
                self.panel._toast("已添加 %s/%s" % (
                    view.get("channel_label"), view.get("name")))
                if view.get("recheck_needed"):
                    messagebox.showwarning(
                        "需要重新登录",
                        "账号已保存，但上游拒绝了当前凭证，请在网页面板中重新登录。",
                        parent=self.panel)
                self.destroy()
            self.after(0, finish)
        threading.Thread(target=runner, daemon=True).start()


def run_gui(runtime):
    if os.name == "nt":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    panel = ControlPanel(runtime)
    panel.mainloop()
    return 0
