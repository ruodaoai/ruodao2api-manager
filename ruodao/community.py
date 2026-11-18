"""Ruodao open-source community bridge: read, comment and reply from the panel.

评论数据只有一份，写在若道（``content_market_comments``）。本模块做两件事：

1. 匿名读取公开评论（不需要登录，代码里不放任何凭证）；
2. 用若道账号走 OAuth 2.0 授权码 + PKCE 拿到 ``app:comments`` 令牌后，
   代理发表 / 回复 / 删除评论。令牌只保存在本机 SQLite，不出现在页面与日志里。

接口与约定见 https://ruodao.com/open-source/docs 。
"""

import base64
import hashlib
import datetime
import html as html_lib
import json
import re
import secrets
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
MAX_REPLY_DEPTH = 2
POST_INTERVAL = 3.0      # 本机最小发帖间隔，避免误触连点
AUTH_TTL = 900           # 授权 state 有效期
AUTH_SETTING = "community_auth"
SCOPE = "openid profile app:comments"

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "tr", "table", "section", "article",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super(_TextExtractor, self).__init__(convert_charrefs=True)
        self.chunks = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self._skip:
                self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def html_to_text(raw, limit=MAX_BODY):
    text = str(raw or "")
    if not text:
        return ""
    try:
        extractor = _TextExtractor()
        extractor.feed(text)
        extractor.close()
        text = "".join(extractor.chunks)
    except Exception:
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    text = text.replace("\u00a0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip()
             for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _clean_text(value, limit=200):
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:limit]


def _timestamp(value):
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return 0
    if dt.tzinfo is None:
        # 若道接口返回的是不带时区的北京时间
        dt = dt.replace(tzinfo=datetime.timezone(
            datetime.timedelta(hours=8)))
    return dt.timestamp()


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class CommunityClient(object):
    def __init__(self, app):
        self.app = app
        self.config = app.config
        self.store = app.store
        self.log = app.log
        self._lock = threading.Lock()
        self._cache = None
        self._cache_at = 0.0
        self._states = {}
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
    def client_id(self):
        return str(self.config.get("community_client_id") or "").strip()

    @property
    def redirect_uri(self):
        custom = str(self.config.get("community_redirect_uri") or "").strip()
        if custom:
            return custom
        return "http://%s:%d/api/community/auth/callback" % (
            "127.0.0.1", self.config.port)

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

    # -- 令牌 ------------------------------------------------------------
    def _auth(self):
        data = self.store.get_setting(AUTH_SETTING, {}) or {}
        return data if isinstance(data, dict) else {}

    def _save_auth(self, data):
        self.store.set_setting(AUTH_SETTING, data or {})

    def auth_status(self):
        auth = self._auth()
        user = auth.get("user") if isinstance(auth.get("user"), dict) else {}
        expires_at = auth.get("expires_at") or 0
        return {
            "configured": bool(self.client_id),
            "logged_in": bool(auth.get("access_token")),
            "user": {
                "id": str(user.get("userId") or ""),
                "name": str(user.get("displayName") or ""),
                "avatar": str(user.get("avatarUrl") or ""),
                "email": str(user.get("maskedEmail") or ""),
            } if user else None,
            "expires_at": expires_at,
            "redirect_uri": self.redirect_uri,
            "scope": str(auth.get("scope") or ""),
        }

    def _access_token(self):
        auth = self._auth()
        token = str(auth.get("access_token") or "")
        if not token:
            raise UpstreamError("请先用若道账号登录后再发言", status=401)
        expires_at = float(auth.get("expires_at") or 0)
        if expires_at and expires_at - 60 > time.time():
            return token
        refresh_token = str(auth.get("refresh_token") or "")
        if not refresh_token:
            self._save_auth({})
            raise UpstreamError("若道登录已过期，请重新登录", status=401)
        try:
            fresh = self._refresh(refresh_token)
        except UpstreamError:
            self._save_auth({})
            raise
        return str(fresh.get("access_token") or "")

    def _refresh(self, refresh_token):
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }).encode("utf-8")
        status, obj, _ = util.http_json(
            self.base + "/api/oauth/token", method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            body=body, timeout=self.timeout)
        if status != 200 or not isinstance(obj, dict) or not obj.get("access_token"):
            self.log("WARN", "若道令牌续期失败：HTTP %s" % status)
            raise UpstreamError("若道登录已过期，请重新登录", status=401)
        self._store_token(obj, keep_user=True)
        return obj

    def _store_token(self, payload, keep_user=True):
        auth = self._auth() if keep_user else {}
        try:
            expires_in = int(payload.get("expires_in") or 7200)
        except Exception:
            expires_in = 7200
        auth.update({
            "access_token": str(payload.get("access_token") or ""),
            "refresh_token": str(payload.get("refresh_token")
                                 or auth.get("refresh_token") or ""),
            "scope": str(payload.get("scope") or auth.get("scope") or SCOPE),
            "expires_at": time.time() + expires_in,
            "obtained_at": int(time.time()),
        })
        self._save_auth(auth)

    # -- OAuth（授权码 + PKCE，公开客户端） ------------------------------
    def start_auth(self):
        if not self.client_id:
            raise UpstreamError(
                "还没有配置若道 OAuth 客户端 ID：请到 ruodao.com/open-platform "
                "创建「公开客户端」，回调地址填 %s，再把 client_id 填到设置页"
                % self.redirect_uri, status=400)
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(18)
        with self._lock:
            now = time.time()
            self._states = {k: v for k, v in self._states.items()
                            if now - v.get("at", 0) < AUTH_TTL}
            self._states[state] = {"verifier": verifier, "at": now,
                                   "redirect_uri": self.redirect_uri}
        query = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return {"authorize_url": "%s/oauth/authorize?%s" % (self.base, query),
                "state": state, "redirect_uri": self.redirect_uri}

    def complete_auth(self, query):
        params = urllib.parse.parse_qs(query or "")
        state = (params.get("state") or [""])[0]
        code = (params.get("code") or [""])[0]
        error = (params.get("error") or [""])[0]
        if error:
            return False, "授权被取消或失败：%s" % _clean_text(
                (params.get("error_description") or [error])[0], 200)
        if not state or not code:
            return False, "回调参数不完整，请回到面板重新登录"
        with self._lock:
            pending = self._states.pop(state, None)
        if not pending:
            return False, "授权状态已失效，请回到面板重新点击登录"
        if time.time() - pending.get("at", 0) > AUTH_TTL:
            return False, "授权超时，请重新登录"
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.get("redirect_uri") or self.redirect_uri,
            "code_verifier": pending.get("verifier") or "",
            "client_id": self.client_id,
        }).encode("utf-8")
        status, obj, _ = util.http_json(
            self.base + "/api/oauth/token", method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            body=body, timeout=self.timeout)
        if status != 200 or not isinstance(obj, dict) or not obj.get("access_token"):
            message = ""
            if isinstance(obj, dict):
                message = str(obj.get("error_description")
                              or obj.get("message") or obj.get("error") or "")
            self.log("WARN", "若道授权换取令牌失败：HTTP %s %s" % (status, message))
            return False, "换取令牌失败：%s" % (message or ("HTTP %s" % status))
        self._store_token(obj, keep_user=False)
        user = self._fetch_user()
        if user:
            auth = self._auth()
            auth["user"] = user
            self._save_auth(auth)
        self.log("INFO", "若道社区已登录：%s" % (
            (user or {}).get("displayName") or "若道用户"))
        return True, "已登录若道账号，可以在这里发言了"

    def _fetch_user(self):
        auth = self._auth()
        token = str(auth.get("access_token") or "")
        if not token:
            return None
        status, obj, _ = util.http_json(
            self.base + "/api/public/apps/me",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json"},
            timeout=self.timeout)
        if status != 200 or not isinstance(obj, dict):
            return None
        data = obj.get("data")
        if not isinstance(data, dict):
            return None
        return {
            "userId": str(data.get("userId") or ""),
            "displayName": str(data.get("displayName") or ""),
            "avatarUrl": str(data.get("avatarUrl") or ""),
            "maskedEmail": str(data.get("maskedEmail") or ""),
        }

    def logout(self):
        self._save_auth({})
        with self._lock:
            self._states.clear()
        self.log("INFO", "已退出若道社区账号")
        return True

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

    def _fetch(self):
        url = "%s/api/public/apps/%s/comments?page=1&size=%d" % (
            self.base, urllib.parse.quote(self.slug), PAGE_SIZE)
        headers = {"User-Agent": util.DEFAULT_UA, "Accept": "application/json"}
        token = str(self._auth().get("access_token") or "")
        if token:
            headers["Authorization"] = "Bearer " + token
        status, obj, _ = util.http_json(url, headers=headers,
                                        timeout=self.timeout)
        if status == 401 and token:
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
        token = self._access_token()
        payload = {"body": text}
        parent_id = str(parent_id or "").strip()
        if parent_id:
            payload["parentCommentId"] = parent_id
        url = "%s/api/public/apps/%s/comments" % (
            self.base, urllib.parse.quote(self.slug))
        status, obj, _ = util.http_json(
            url, method="POST",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json",
                     "Content-Type": "application/json",
                     "User-Agent": util.DEFAULT_UA},
            body=payload, timeout=self.timeout)
        if status == 401:
            self._save_auth({})
            raise UpstreamError("若道登录已失效，请重新登录", status=401)
        if status == 403:
            raise UpstreamError("若道账号缺少 app:comments 授权，请重新登录并同意",
                                status=403)
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
        return {"ok": True, "result": obj.get("data") if isinstance(obj, dict) else None}

    def delete(self, comment_id):
        comment_id = str(comment_id or "").strip()
        if not comment_id:
            raise UpstreamError("评论 ID 为空", status=400)
        token = self._access_token()
        url = "%s/api/public/apps/comments/%s" % (
            self.base, urllib.parse.quote(comment_id))
        status, obj, _ = util.http_json(
            url, method="DELETE",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json",
                     "User-Agent": util.DEFAULT_UA},
            timeout=self.timeout)
        if status == 401:
            self._save_auth({})
            raise UpstreamError("若道登录已失效，请重新登录", status=401)
        if status not in (200, 204):
            message = ""
            if isinstance(obj, dict):
                message = str(obj.get("message") or "")
            raise UpstreamError(message or ("删除失败（HTTP %s）" % status),
                                status=400 if status < 500 else 502)
        with self._lock:
            self._cache = None
        self.log("INFO", "删除了自己在若道社区的一条评论")
        return {"ok": True}


def render_callback_page(ok, message):
    """OAuth 回调返回的小页面：提示结果并尝试自动关闭窗口。"""
    title = "授权成功" if ok else "授权未完成"
    color = "#17945c" if ok else "#c4552d"
    script = ("<script>setTimeout(function(){window.close();},1200)</script>"
              if ok else "")
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>%s · 若道 2API</title><style>"
        "body{margin:0;height:100vh;display:flex;align-items:center;"
        "justify-content:center;background:#f7f8fa;color:#1f2430;"
        "font:14px/1.7 -apple-system,'Segoe UI',sans-serif}"
        ".box{background:#fff;border:1px solid #e6e8ef;border-radius:14px;"
        "padding:28px 34px;max-width:420px;box-shadow:0 8px 30px rgba(20,25,40,.06)}"
        "h1{font-size:16px;margin:0 0 10px;color:%s}p{margin:0;color:#5a6273}"
        "small{display:block;margin-top:14px;color:#98a0b0}</style></head><body>"
        "<div class=\"box\"><h1>%s</h1><p>%s</p>"
        "<small>可以关闭此页面，回到 Ruodao2API 面板继续。</small></div>%s"
        "</body></html>" % (title, color, title, html_lib.escape(message), script)
    )
