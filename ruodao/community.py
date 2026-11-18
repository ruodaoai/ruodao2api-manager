"""Ruodao open-source community bridge: read, comment and reply from the panel.

评论数据只有一份，写在若道（``content_market_comments``）。本模块做两件事：

1. 匿名读取公开评论（不需要登录，代码里不放任何凭证）；
2. 用若道账号（邮箱 + 密码）换取站内登录令牌后，代理发表 / 回复 / 删除
   评论。**密码只用一次**，换完令牌立刻丢弃；令牌只保存在本机 SQLite，
   不出现在页面、日志和错误信息里。

接口与约定见 https://ruodao.com/open-source/docs 。
"""

import datetime
import html as html_lib
import re
import threading
import time
import urllib.parse
from html.parser import HTMLParser

from . import util
from .errors import UpstreamError

ITEM_SLUG = "ruodao2api"
ITEM_TITLE = "若道 2API"
DEFAULT_BASE = "https://ruodao.com"

CACHE_TTL = 60
PAGE_SIZE = 30
MAX_BODY = 1000          # 服务端上限 1000 字
POST_INTERVAL = 3.0      # 本机最小发帖间隔，避免误触连点
AUTH_SETTING = "community_auth"

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "tr", "table", "section", "article",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def html_to_text(raw, limit=MAX_BODY):
    value = str(raw or "")
    if not value:
        return ""
    if "<" in value and ">" in value:
        parser = _TextExtractor()
        try:
            parser.feed(value)
            parser.close()
            value = parser.text()
        except Exception:
            value = re.sub(r"<[^>]+>", " ", value)
    value = html_lib.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = value.strip()
    if limit and len(value) > limit:
        value = value[:limit].rstrip() + "…"
    return value


def _clean_text(value, limit=200):
    return html_to_text(value, limit=limit) if value else ""


def _timestamp(value):
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e11:
            number = number / 1000.0
        return int(number)
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        number = float(text)
        if number > 1e11:
            number = number / 1000.0
        return int(number)
    cleaned = text.replace("Z", "+00:00")
    match = re.match(r"^(.*?\.\d{6})\d*([+-]\d{2}:\d{2})$", cleaned)
    if match:
        cleaned = match.group(1) + match.group(2)
    try:
        parsed = datetime.datetime.fromisoformat(cleaned)
    except Exception:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


class CommunityClient(object):
    def __init__(self, app):
        self.app = app
        self.config = app.config
        self.store = app.store
        self.log = app.log
        self._lock = threading.Lock()
        self._cache = None
        self._cache_at = 0.0
        self._last_post = 0.0

    # -- 基础配置 --------------------------------------------------------
    @property
    def base(self):
        value = str(self.config.get("community_base") or DEFAULT_BASE).strip()
        return value.rstrip("/") or DEFAULT_BASE

    @property
    def slug(self):
        return str(self.config.get("community_slug") or ITEM_SLUG).strip() \
            or ITEM_SLUG

    @property
    def timeout(self):
        try:
            return max(5, int(self.config.get("community_timeout") or 15))
        except Exception:
            return 15

    def community_url(self):
        return "%s/open-source/%s#comments" % (self.base, self.slug)

    def docs_url(self):
        return "%s/open-source/docs" % self.base

    # -- 登录态 ----------------------------------------------------------
    def _auth(self):
        data = self.store.get_setting(AUTH_SETTING, {}) or {}
        return data if isinstance(data, dict) else {}

    def _save_auth(self, data):
        self.store.set_setting(AUTH_SETTING, data or {})

    def auth_status(self):
        auth = self._auth()
        user = auth.get("user") if isinstance(auth.get("user"), dict) else {}
        return {
            "method": "password",
            "configured": True,
            "logged_in": bool(auth.get("token")),
            "user": {
                "id": str(user.get("id") or ""),
                "name": str(user.get("name") or ""),
                "avatar": str(user.get("avatar") or ""),
                "email": str(user.get("email") or auth.get("email") or ""),
            } if user else None,
            "login_url": "%s/login" % self.base,
        }

    def _token(self):
        token = str(self._auth().get("token") or "")
        if not token:
            raise UpstreamError("请先用若道账号登录后再发言", status=401)
        return token

    def login(self, email, password):
        """用若道账号密码换站内令牌；密码不落地，只留令牌和昵称。"""
        email = str(email or "").strip()
        password = str(password or "")
        if not email or not password:
            raise UpstreamError("请填写若道账号的邮箱和密码", status=400)
        if "@" not in email:
            raise UpstreamError("若道账号用的是邮箱，请填写完整邮箱地址",
                                status=400)
        status, obj, _ = util.http_json(
            self.base + "/api/auth/login", method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": util.DEFAULT_UA},
            body={"email": email, "password": password},
            timeout=self.timeout)
        data = obj.get("data") if isinstance(obj, dict) else None
        token = ""
        if isinstance(data, dict):
            token = str(data.get("token") or data.get("accessToken") or "")
        if not token and isinstance(obj, dict):
            token = str(obj.get("token") or "")
        if status != 200 or not token:
            message = ""
            if isinstance(obj, dict):
                message = str(obj.get("message") or obj.get("errorCode") or "")
            if "invalid email or password" in message.lower():
                message = "邮箱或密码不对"
            elif status == 429:
                message = "尝试太频繁了，过一会儿再试"
            elif status >= 500:
                message = "若道服务暂时不可用（HTTP %s）" % status
            raise UpstreamError("登录失败：%s" % (message or ("HTTP %s" % status)),
                                status=401)
        self._save_auth({"token": token, "email": email,
                         "obtained_at": int(time.time())})
        user = self._fetch_user()
        if user:
            auth = self._auth()
            auth["user"] = user
            self._save_auth(auth)
        self.log("INFO", "若道社区已登录：%s" % (
            (user or {}).get("name") or email))
        return {"ok": True, "user": user, "auth": self.auth_status()}

    def _fetch_user(self):
        token = str(self._auth().get("token") or "")
        if not token:
            return None
        status, obj, _ = util.http_json(
            self.base + "/api/users/me",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json",
                     "User-Agent": util.DEFAULT_UA},
            timeout=self.timeout)
        if status != 200 or not isinstance(obj, dict):
            return None
        data = obj.get("data")
        if isinstance(data, dict) and isinstance(data.get("user"), dict):
            data = data["user"]
        if not isinstance(data, dict):
            return None
        name = (data.get("displayName") or data.get("nickname")
                or data.get("name") or data.get("username") or "")
        email = data.get("email") or data.get("maskedEmail") or ""
        return {
            "id": str(data.get("id") or data.get("userId") or ""),
            "name": str(name or ""),
            "avatar": str(data.get("avatarUrl") or data.get("avatar") or ""),
            "email": str(email or ""),
        }

    def logout(self):
        self._save_auth({})
        self._forget_cache()
        self.log("INFO", "已退出若道社区账号")
        return True

    def _forget_cache(self):
        with self._lock:
            self._cache = None
            self._cache_at = 0.0

    # -- 读取 ------------------------------------------------------------
    def snapshot(self, refresh=False):
        now = time.time()
        with self._lock:
            cached = self._cache
            fresh = cached is not None and now - self._cache_at < CACHE_TTL
        if cached is not None and fresh and not refresh:
            return self._decorate(dict(cached, cached=True, stale=False,
                                       warning=""))
        try:
            payload = self._fetch()
        except Exception as exc:
            self.log("WARN", "读取若道社区讨论失败：%s" % exc)
            with self._lock:
                cached = self._cache
            if cached is not None:
                return self._decorate(dict(
                    cached, cached=True, stale=True,
                    warning="暂时无法连接若道社区，正在显示上次获取的内容"))
            if isinstance(exc, UpstreamError):
                raise
            raise UpstreamError("暂时无法连接若道社区，请检查网络后重试",
                                status=502)
        with self._lock:
            self._cache = payload
            self._cache_at = time.time()
        return self._decorate(dict(payload, cached=False, stale=False,
                                   warning=""))

    def _decorate(self, payload):
        # 登录状态永远取实时值，避免被评论缓存带着一起过期
        payload["auth"] = self.auth_status()
        return payload

    def _comments_url(self):
        return "%s/api/content-market/items/%s/comments" % (
            self.base, urllib.parse.quote(self.slug))

    def _fetch(self):
        url = "%s?page=1&size=%d" % (self._comments_url(), PAGE_SIZE)
        headers = {"User-Agent": util.DEFAULT_UA, "Accept": "application/json"}
        token = str(self._auth().get("token") or "")
        if token:
            headers["Authorization"] = "Bearer " + token
        status, obj, _ = util.http_json(url, headers=headers,
                                        timeout=self.timeout)
        if status in (401, 403) and token:
            # 令牌失效时丢掉它，匿名再读一次：读公开评论本来就不需要登录
            self._save_auth({})
            headers.pop("Authorization", None)
            status, obj, _ = util.http_json(url, headers=headers,
                                            timeout=self.timeout)
        if status != 200:
            raise UpstreamError("若道社区暂时不可用（HTTP %s）" % status,
                                status=502)
        if not isinstance(obj, dict) or not obj.get("success"):
            raise UpstreamError("若道社区返回了无法识别的数据", status=502)
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        comments = []
        for raw_comment in items:
            comment = self._comment(raw_comment)
            if comment:
                comments.append(comment)
        try:
            total = int(data.get("total"))
        except Exception:
            total = len(comments)
        return {
            "source": "ruodao",
            "item": {
                "slug": self.slug,
                "title": self._item_title(),
                "url": self.community_url(),
                "docs_url": self.docs_url(),
            },
            "total": max(total, len(comments)),
            "comments": comments,
            "fetched_at": int(time.time()),
        }

    def _item_title(self):
        with self._lock:
            cached = self._cache
        item = (cached or {}).get("item") if isinstance(cached, dict) else None
        if isinstance(item, dict) and item.get("title"):
            return str(item["title"])
        return ITEM_TITLE

    def _comment(self, raw, depth=0):
        if not isinstance(raw, dict):
            return None
        replies = []
        if depth < 1:
            children = raw.get("replies")
            if isinstance(children, list):
                for child in children:
                    reply = self._comment(child, depth + 1)
                    if reply:
                        replies.append(reply)
        return {
            "id": str(raw.get("id") or ""),
            "author": _clean_text(raw.get("userDisplayName"), 40) or "社区用户",
            "body": html_to_text(raw.get("body")),
            "created_at": _timestamp(raw.get("createdAt")),
            "reply_to": _clean_text(raw.get("replyToUserDisplayName"), 40),
            "owner": bool(raw.get("owner")),
            "replies": replies,
        }

    # -- 写入 ------------------------------------------------------------
    def post(self, body, parent_id=""):
        text = _clean_text(body, MAX_BODY + 1)
        if len(text) > MAX_BODY:
            raise UpstreamError("评论最多 %d 字" % MAX_BODY, status=400)
        if not text:
            raise UpstreamError("评论内容不能为空", status=400)
        now = time.time()
        with self._lock:
            if now - self._last_post < POST_INTERVAL:
                raise UpstreamError("发言太快了，稍等一下再发", status=429)
        token = self._token()
        payload = {"body": text}
        parent_id = str(parent_id or "").strip()
        if parent_id:
            payload["parentCommentId"] = parent_id
        status, obj, _ = util.http_json(
            self._comments_url(), method="POST",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json",
                     "Content-Type": "application/json",
                     "User-Agent": util.DEFAULT_UA},
            body=payload, timeout=self.timeout)
        if status in (401, 403):
            self._save_auth({})
            raise UpstreamError("若道登录已失效，请重新登录", status=401)
        if status != 200:
            message = ""
            if isinstance(obj, dict):
                message = str(obj.get("message") or obj.get("errorCode") or "")
            raise UpstreamError(message or ("发表失败（HTTP %s）" % status),
                                status=400 if status < 500 else 502)
        with self._lock:
            self._last_post = now
            self._cache = None
        self.log("INFO", "在若道社区发表了%s" % ("回复" if parent_id else "评论"))
        return {"ok": True,
                "result": obj.get("data") if isinstance(obj, dict) else None}

    def delete(self, comment_id):
        comment_id = str(comment_id or "").strip()
        if not comment_id:
            raise UpstreamError("评论 ID 为空", status=400)
        token = self._token()
        url = "%s/api/content-market/comments/%s" % (
            self.base, urllib.parse.quote(comment_id))
        status, obj, _ = util.http_json(
            url, method="DELETE",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json",
                     "User-Agent": util.DEFAULT_UA},
            timeout=self.timeout)
        if status in (401, 403):
            self._save_auth({})
            raise UpstreamError("若道登录已失效，请重新登录", status=401)
        if status not in (200, 204):
            message = ""
            if isinstance(obj, dict):
                message = str(obj.get("message") or "")
            raise UpstreamError(message or ("删除失败（HTTP %s）" % status),
                                status=400 if status < 500 else 502)
        self._forget_cache()
        self.log("INFO", "删除了自己在若道社区的一条评论")
        return {"ok": True}
