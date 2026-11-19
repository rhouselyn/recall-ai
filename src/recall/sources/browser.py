"""登录网页连接器共用的浏览器读取机制（开发规格 §5.5）。

每个平台账号使用独立持久化配置目录；首次打开有界面浏览器由用户自行登录。
同步进程只读取页面正常加载产生的网络响应或可见卡片，不执行写入操作。
Playwright 是可选依赖；真人账号验收必须在用户自己的机器上进行。
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import resolve_db_path
from ..errors import RecallError
from ..secrets import read_credential, write_credential

PLATFORM_START = {
    "zhihu_web": "https://www.zhihu.com/people/me/collections",
    "bilibili_favorites": "https://space.bilibili.com/",
    "xiaohongshu_favorites": "https://www.xiaohongshu.com/explore",
    "douyin_favorites": "https://www.douyin.com/user/self",
    "youtube_favorites": "https://www.youtube.com/",
}
PLATFORM_LOGIN = {
    "zhihu_web": "https://www.zhihu.com/signin?next=%2Fpeople%2Fme%2Fcollections",
    "bilibili_favorites": "https://passport.bilibili.com/login",
    "youtube_favorites": "https://www.youtube.com/signin?next=%2F",
}
PLATFORM_SESSION_DOMAINS = {
    "zhihu_web": ("zhihu.com",),
    "bilibili_favorites": ("bilibili.com",),
    "xiaohongshu_favorites": ("xiaohongshu.com", "rednote.com"),
    "douyin_favorites": ("douyin.com", "iesdouyin.com"),
    "youtube_favorites": ("youtube.com", "google.com"),
}
SESSION_FILE = "session-cookies"
logger = logging.getLogger(__name__)


def profile_dir(source_id: str, db_path: str | Path | None = None) -> Path:
    database = resolve_db_path(db_path).resolve()
    root = database.parent / "browser-profiles"
    digest = hashlib.sha256((os.path.normcase(str(database)) + "\0" + source_id).encode()).hexdigest()
    target = root / ("source-" + digest)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in source_id)
    legacy = root / safe
    if not target.exists() and legacy.is_dir() and not legacy.is_symlink():
        try:
            identity = json.loads((legacy / "identity.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            identity = {}
        # 只有确切的归属标签才允许搬迁，避免标点、大小写碰撞串号。
        if isinstance(identity, dict) and identity.get("label") == source_id:
            legacy.rename(target)
    return target


def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:
        raise RecallError(
            "CONFIG_ERROR", "登录网页连接器需要安装 playwright：python -m pip install -e '.[browser]'"
        ) from exc
    return sync_playwright


def _launch(playwright: Any, directory: Path, *, headless: bool) -> Any:
    channels = ("chrome", "msedge") if os.name == "nt" else ("chrome",)
    for channel in channels:
        try:
            return playwright.chromium.launch_persistent_context(
                str(directory), headless=headless, channel=channel
            )
        except Exception as exc:
            if "not found" not in str(exc).lower() and "doesn't exist" not in str(exc).lower():
                raise
    try:
        return playwright.chromium.launch_persistent_context(str(directory), headless=headless)
    except Exception as exc:
        if "executable doesn't exist" in str(exc).lower():
            raise RecallError("CONFIG_ERROR", "找不到登录浏览器。请安装 Chrome，或运行 python -m playwright install chromium。") from exc
        raise


def _cookie_key(cookie):
    return (cookie["name"], cookie["domain"], cookie["path"], cookie.get("partitionKey") or "")


def _session_cookies(cookies, kind):
    if not isinstance(cookies, list):
        return []
    domains = PLATFORM_SESSION_DOMAINS[kind]
    selected = []
    for cookie in cookies:
        if not isinstance(cookie, dict) or cookie.get("expires") != -1 or not all(
            isinstance(cookie.get(key), str) for key in ("name", "value", "domain", "path")
        ):
            continue
        host = cookie["domain"].lstrip(".").lower()
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            selected.append(cookie)
    return sorted(selected, key=_cookie_key)


def _restore_session_cookies(context, directory, kind):
    try:
        cookies = _session_cookies(json.loads(read_credential(directory / SESSION_FILE) or "[]"), kind)
    except ValueError:
        return []
    if cookies:
        existing = {_cookie_key(cookie) for cookie in context.cookies()}
        missing = [cookie for cookie in cookies if _cookie_key(cookie) not in existing]
        if missing:
            context.add_cookies(missing)
    return cookies


def _save_session_cookies(directory, cookies):
    path = directory / SESSION_FILE
    if cookies:
        write_credential(path, json.dumps(cookies))
    else:
        path.unlink(missing_ok=True)


def connect(source_id: str, kind: str, **options: Any) -> dict[str, Any]:
    with profile_lease(source_id, options.get("db_path")):
        return _connect(source_id, kind, **options)


@contextmanager
def profile_lease(source_id: str, db_path, *, purpose="login"):
    from datetime import timedelta
    import uuid
    from .. import catalog
    from ..clock import now, now_iso, iso
    from ..db import open_readwrite
    from ..leases import heartbeat

    conn = open_readwrite(db_path)
    owner = purpose + ":" + uuid.uuid4().hex
    try:
        with catalog.transaction(conn):
            state = catalog.get_sync_state(conn, source_id)
            if state.get("lease_owner") and (state.get("lease_until") or "") > now_iso():
                raise RecallError("DB_BUSY", "该来源正在同步或登录，请稍后重试。")
            catalog.update_sync_state(conn, source_id, lease_owner=owner,
                                      lease_until=iso(now() + timedelta(seconds=300)))
        with heartbeat(db_path, lambda connection: connection.execute(
            "UPDATE sync_state SET lease_until=? WHERE source_id=? AND lease_owner=?",
            (iso(now() + timedelta(seconds=300)), source_id, owner),
        ).rowcount):
            yield
            if purpose == "archive" and catalog.get_sync_state(conn, source_id).get("lease_owner") != owner:
                raise RecallError("DB_BUSY", "浏览器会话任务已取消。")
    finally:
        conn.execute("UPDATE sync_state SET lease_owner=NULL, lease_until=NULL "
                     "WHERE source_id=? AND lease_owner IN (?,?)", (source_id, owner, "cancelled:" + owner))
        conn.close()


def _connect(
    source_id: str,
    kind: str,
    *,
    wait_seconds: int = 180,
    headless: bool = False,
    user_id: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """首次连接：打开有界面浏览器让用户自行登录，登记资料档目录。"""
    if kind not in PLATFORM_START:
        raise RecallError("INVALID_ARGUMENT", "该来源类型不需要登录浏览器。")
    directory = profile_dir(source_id, db_path)
    directory.mkdir(parents=True, exist_ok=True)
    pw = _playwright()
    from playwright.sync_api import Error

    with pw() as playwright:
        context = _launch(playwright, directory, headless=headless)
        try:
            saved = _restore_session_cookies(context, directory, kind)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(PLATFORM_LOGIN.get(kind, PLATFORM_START[kind]),
                      wait_until="domcontentloaded", timeout=30000)
            if kind == "xiaohongshu_favorites":
                login = page.get_by_role("button", name="登录", exact=True)
                if login.count() == 1 and login.is_visible():
                    login.click(timeout=3000)
            deadline = time.monotonic() + min(max(wait_seconds, 1), 600)
            # shortcut: checkpoint while the window is open, use response events if final redirects are missed.
            while not page.is_closed():
                try:
                    current = _session_cookies(context.cookies(), kind)
                    if current != saved:
                        _save_session_cookies(directory, current)
                        saved = current
                    if time.monotonic() >= deadline:
                        break
                    page.wait_for_timeout(250)
                except Error:
                    if not page.is_closed():
                        raise
                    break
            identity = {"user_id": user_id or "", "label": source_id}
            (directory / "identity.json").write_text(
                json.dumps(identity, ensure_ascii=False), encoding="utf-8"
            )
            return {
                "source_id": source_id,
                "profile_dir": str(directory),
                "identity": identity,
                "note": "请在浏览器中确认已登录本人账号，随后执行 recall sync。",
            }
        except OSError as exc:
            raise RecallError("CONFIG_ERROR", "无法保存登录会话，请检查本机数据目录的写入权限。") from exc
        finally:
            context.close()


def load_session(source_id: str, kind: str, db_path: str | Path | None = None) -> "PlaywrightBrowserSession":
    directory = profile_dir(source_id, db_path)
    if not (directory / "identity.json").is_file():
        raise RecallError("AUTH_REQUIRED", "尚未执行 recall source connect。")
    pw = _playwright()
    playwright = pw().start()
    context = None
    try:
        context = _launch(playwright, directory, headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        session = PlaywrightBrowserSession(page, playwright, context, directory, kind)
        page.goto(PLATFORM_START[kind], wait_until="domcontentloaded", timeout=30000)
        return session
    except Exception:
        if context is not None:
            context.close()
        playwright.stop()
        raise


class PlaywrightBrowserSession:
    """实现 sources.base.BrowserSession；读取在页面上下文中完成。"""

    def __init__(self, page: Any, playwright: Any, context: Any, directory: Path, kind: str) -> None:
        self.page = page
        self._playwright = playwright
        self._context = context
        self._directory = directory
        self._kind = kind
        self._saved_cookies = _restore_session_cookies(context, directory, kind)
        self._douyin_started = False
        self._xhs_started = False
        self._douyin_identity: dict[str, Any] = {}
        page.on("response", self._observe_identity)

    def _observe_identity(self, response: Any) -> None:
        from urllib.parse import urlsplit
        url = urlsplit(response.url)
        if url.hostname not in {"www.douyin.com", "www.iesdouyin.com"} or url.path != "/aweme/v1/web/user/profile/self/":
            return
        try:
            payload = response.json()
            user = payload.get("user", {})
            if response.ok and payload.get("status_code") == 0 and isinstance(user, dict):
                self._douyin_identity = {k: str(user[k]) for k in ("uid", "sec_uid") if user.get(k)}
        except (ValueError, TypeError, AttributeError):
            return

    def close(self) -> None:
        from playwright.sync_api import Error

        try:
            current = _session_cookies(self._context.cookies(), self._kind)
            if current != self._saved_cookies:
                _save_session_cookies(self._directory, current)
        except (Error, OSError):
            logger.warning("无法更新本机保存的平台登录会话。")
        finally:
            try:
                self._context.close()
            finally:
                self._playwright.stop()

    def youtube_playlists(self) -> tuple[str, list[dict[str, str]]]:
        import hashlib
        import re
        from urllib.parse import parse_qs, urlsplit

        try:
            self.page.goto("https://www.youtube.com/feed/playlists", wait_until="domcontentloaded", timeout=30000)
            self.page.locator("#avatar-btn").first.click(timeout=15000)
            self.page.wait_for_timeout(1000)
            account = self.page.evaluate("""() => {
                const header = document.querySelector('ytd-active-account-header-renderer');
                return header?.querySelector('#email')?.textContent?.trim()
                    || header?.querySelector('#channel-handle')?.textContent?.trim() || '';
            }""")
            if not account:
                raise RecallError("AUTH_REQUIRED", "无法确认 YouTube 登录账号，请重新连接。")
            self.page.keyboard.press("Escape")
            found = [{"id": "WL", "title": "稍后观看"}, {"id": "LL", "title": "喜欢的视频"}]
            seen = {"WL", "LL"}
            # shortcut: 页面只发现当前滚动加载的播放列表，平台分页协议稳定后再扩大覆盖。
            unchanged = 0
            for _ in range(30):
                before = len(seen)
                links = self.page.locator('a[href*="list="]').evaluate_all(
                    "nodes => nodes.map(n => ({href:n.href, title:n.title || n.innerText || n.getAttribute('aria-label') || ''}))")
                for link in links:
                    values = parse_qs(urlsplit(link.get("href", "")).query).get("list", [])
                    value = values[0] if values else ""
                    if re.fullmatch(r"[A-Za-z0-9_-]{2,128}", value) and value not in seen:
                        seen.add(value)
                        found.append({"id": value, "title": str(link.get("title") or "播放列表").strip()[:200] or "播放列表"})
                self.page.mouse.wheel(0, 2500)
                self.page.wait_for_timeout(500)
                unchanged = unchanged + 1 if len(seen) == before else 0
                if unchanged >= 3:
                    break
            return hashlib.sha256(account.lower().encode()).hexdigest(), found
        except RecallError:
            raise
        except Exception as exc:
            raise RecallError("AUTH_REQUIRED", "无法读取 YouTube 登录页面，请检查网络并重新登录。") from exc

    def youtube_playlist(self, playlist_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        import http.cookiejar
        import re
        try:
            import yt_dlp
        except ImportError as exc:
            raise RecallError("CONFIG_ERROR", "YouTube 连接器需要安装：python -m pip install -e '.[youtube]'。") from exc
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,128}", playlist_id):
            raise RecallError("INVALID_ARGUMENT", "无效的 YouTube 播放列表。")
        options: dict[str, Any] = {"extract_flat": True, "skip_download": True, "quiet": True,
                                  "no_warnings": True, "socket_timeout": 20, "retries": 1}
        if limit:
            options["playlistend"] = limit
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                jar = downloader.cookiejar
                for cookie in self._context.cookies(["https://www.youtube.com", "https://www.google.com"]):
                    domain = cookie["domain"]
                    jar.set_cookie(http.cookiejar.Cookie(
                        version=0, name=cookie["name"], value=cookie["value"], port=None,
                        port_specified=False, domain=domain, domain_specified=domain.startswith("."),
                        domain_initial_dot=domain.startswith("."), path=cookie.get("path", "/"),
                        path_specified=True, secure=cookie.get("secure", False),
                        expires=int(cookie["expires"]) if cookie.get("expires", -1) > 0 else None,
                        discard=cookie.get("expires", -1) <= 0, comment=None, comment_url=None,
                        rest={}, rfc2109=False))
                info = downloader.extract_info("https://www.youtube.com/playlist?list=" + playlist_id, download=False)
                if not isinstance(info, dict) or info.get("entries") is None:
                    raise RecallError("SOURCE_FORMAT_CHANGED", "YouTube 播放列表响应格式变化。")
                return [entry for entry in info["entries"] if isinstance(entry, dict)]
        except RecallError:
            raise
        except Exception as exc:
            raise RecallError("NETWORK_ERROR", "无法读取 YouTube 播放列表，请检查网络、登录和播放列表权限。") from exc

    def identity(self) -> dict[str, Any]:
        try:
            identity = json.loads((self._directory / "identity.json").read_text(encoding="utf-8"))
            if not isinstance(identity, dict):
                raise RecallError("AUTH_REQUIRED", "登录身份不可用，请重新连接。")
            from urllib.parse import urlsplit
            if urlsplit(self.page.url).hostname == "www.douyin.com":
                if not self._douyin_identity:
                    self.page.wait_for_timeout(1500)
                actual = self._douyin_identity
                if not actual.get("uid"):
                    raise RecallError("AUTH_REQUIRED", "未观察到抖音本人身份响应，请重新登录；旧收藏保持不变。")
                if identity.get("user_id") and str(identity["user_id"]) not in actual.values():
                    raise RecallError("ACCOUNT_MISMATCH", "抖音实际登录账号与填写的用户 ID 不一致。")
                identity.update(user_id=actual["uid"], sec_uid=actual.get("sec_uid"))
            if "xiaohongshu.com" in self.page.url or "rednote.com" in self.page.url:
                state_user = self.page.evaluate("""() => {
                    const u = globalThis.__INITIAL_STATE__?.user || {};
                    const ui = u.userInfo?._rawValue || u.userInfo || {};
                    const loggedIn = u.loggedIn?._rawValue ?? u.loggedIn;
                    return { loggedIn: !!loggedIn, userId: ui.userId || ui.user_id || '' };
                }""")
                actual = str(state_user.get("userId") or "") if isinstance(state_user, dict) else ""
                if not actual:
                    from urllib.parse import urlsplit
                    import re
                    href = self.page.evaluate("""() => document.querySelector(
                        'header a[href^="/user/profile/"], .user.side-bar-component a[href^="/user/profile/"]'
                        )?.getAttribute('href') || ''""")
                    match = re.search(r"/user/profile/([^/?#]+)", urlsplit(href).path)
                    if not match:
                        raise RecallError("AUTH_REQUIRED", "无法确认小红书登录账号，请重新登录。")
                    actual = match.group(1)
                if identity.get("user_id") and identity["user_id"] != actual:
                    raise RecallError("ACCOUNT_MISMATCH", "小红书登录账号与填写的用户 ID 不一致。")
                identity["user_id"] = actual
            return identity
        except (OSError, ValueError) as exc:
            raise RecallError("AUTH_REQUIRED", "登录身份不可用，请重新连接。") from exc

    def xiaohongshu_note_text(self, url: str) -> str:
        import re
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        note_id = re.fullmatch(r"/(?:explore|discovery/item)/([0-9a-f]+)/?", parts.path)
        if parts.scheme != "https" or parts.netloc != "www.xiaohongshu.com" or not note_id:
            raise RecallError("INVALID_ARGUMENT", "不是有效的小红书笔记链接。")
        from playwright.sync_api import Error

        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page = urlsplit(self.page.url)
            if self.status() in ("login_required", "expired") or (response and response.status == 401):
                raise RecallError("AUTH_REQUIRED", "小红书会话已过期，请重新登录。")
            if response and response.status == 429:
                raise RecallError("RATE_LIMITED")
            if self.status() == "captcha" or (response and response.status in (403, 404, 410, 460, 461)):
                raise RecallError("PERMISSION_DENIED", "小红书未提供该笔记，或需要人工验证。")
            if response and response.status >= 500:
                raise RecallError("NETWORK_ERROR")
            if page.scheme != "https" or page.netloc != parts.netloc or page.path.rstrip("/") != parts.path.rstrip("/"):
                raise RecallError("PERMISSION_DENIED", "小红书没有打开所选笔记。")
            return self.page.evaluate("""id => {
                const note = globalThis.__INITIAL_STATE__?.note?.noteDetailMap?.[id]?.note;
                return note?.desc?.slice?.(0, 350000) || '';
            }""", note_id[1])
        except Error as exc:
            raise RecallError("NETWORK_ERROR", "无法加载小红书笔记页面。") from exc

    def status(self) -> str:
        try:
            url = self.page.url
            title = self.page.title()
        except Exception:
            return "expired"
        if "login" in url.lower() or "passport" in url.lower():
            return "login_required"
        if "验证" in title or "captcha" in url.lower():
            return "captcha"
        return "ok"

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        from urllib.parse import urlencode, urlsplit

        target = url if url.startswith("http") else self.page.url.split("/")[0] + "//" + self.page.url.split("/")[2] + url
        if params:
            target += ("&" if "?" in target else "?") + urlencode(params)
        path = urlsplit(target).path
        if "douyin.com" in target and path == "/aweme/v1/web/aweme/listcollection/":
            return self._douyin_collection()
        if ("xiaohongshu.com" in target or "rednote.com" in target) and path == "/api/sns/web/v2/note/collect/page":
            return self._xhs_collection(params or {})
        script = """async (u) => {
            const r = await fetch(u, {credentials: 'include'});
            if (!r.ok) return {__status: r.status};
            return await r.json();
        }"""
        try:
            payload = self.page.evaluate(script, target)
        except Exception as exc:
            raise RecallError("NETWORK_ERROR", "登录页面暂时无法读取平台响应。") from exc
        if isinstance(payload, dict) and "__status" in payload:
            status = int(payload["__status"])
            if status in (401, 403):
                raise RecallError("AUTH_REQUIRED", "页面会话无权限，请重新连接。")
            if status == 429:
                raise RecallError("RATE_LIMITED", "页面请求被限流。")
            raise RecallError("NETWORK_ERROR", "页面响应异常。")
        if isinstance(payload, dict):
            if payload.get("success") is False or payload.get("status_code", 0) not in (0, None):
                raise RecallError("PERMISSION_DENIED", "平台拒绝读取收藏，请在浏览器确认登录和权限。")
            if "bilibili.com" in target and payload.get("code", 0) != 0:
                raise RecallError("PERMISSION_DENIED", "B 站拒绝读取收藏，请确认登录状态。")
        return payload

    def _douyin_collection(self) -> Any:
        try:
            with self.page.expect_response(
                lambda response: "/aweme/v1/web/aweme/listcollection/" in response.url,
                timeout=20000,
            ) as captured:
                if not self._douyin_started:
                    self.page.goto("https://www.douyin.com/user/self", wait_until="domcontentloaded")
                    tab = self.page.get_by_role("tab", name="收藏", exact=True)
                    if tab.get_attribute("aria-selected") != "true":
                        tab.click()
                    videos = self.page.get_by_role("tab", name="视频", exact=True)
                    if videos.get_attribute("aria-selected") != "true":
                        videos.click()
                    self._douyin_started = True
                else:
                    self.page.mouse.wheel(0, 2400)
            response = captured.value
            if not response.ok:
                raise RecallError("PERMISSION_DENIED", "抖音收藏页面拒绝访问。")
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("status_code", 0) != 0:
                raise RecallError("PERMISSION_DENIED", "抖音收藏页面返回错误。")
            return payload
        except RecallError:
            raise
        except Exception as exc:
            raise RecallError("SOURCE_FORMAT_CHANGED", "无法读取抖音收藏页面，请检查登录或页面验证。") from exc

    def _xhs_collection(self, params: dict[str, Any]) -> Any:
        from urllib.parse import quote, urlsplit
        try:
            cursor = int(params.get("cursor") or 0)
            if not self._xhs_started:
                user_id = self.identity().get("user_id")
                if not user_id:
                    raise RecallError("AUTH_REQUIRED", "无法确认小红书登录账号。")
                host = urlsplit(self.page.url).hostname
                if host not in ("www.xiaohongshu.com", "www.rednote.com"):
                    raise RecallError("AUTH_REQUIRED", "没有可用的小红书登录页面。")
                target = f"https://{host}/user/profile/" + quote(user_id, safe="") + "?tab=fav&subTab=note"
                self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
                self._xhs_started = True
            else:
                self.page.mouse.wheel(0, 2400)
            self.page.wait_for_timeout(1500)
            raw = self.page.evaluate("""() => {
                const notes = globalThis.__INITIAL_STATE__?.user?.notes;
                return (notes?._rawValue ?? notes)?.[1] ?? null;
            }""")
            if not isinstance(raw, list):
                raise RecallError("SOURCE_FORMAT_CHANGED", "无法读取小红书收藏页面数据。")
            if not raw and self.page.locator(".login-container").first.is_visible():
                raise RecallError("AUTH_REQUIRED", "小红书收藏页要求重新登录。")
            notes = []
            for item in raw[cursor:]:
                if not isinstance(item, dict) or not isinstance(item.get("noteCard"), dict):
                    continue
                card = item["noteCard"]
                notes.append({
                    "note_id": item.get("id") or item.get("noteId") or card.get("noteId"),
                    "display_title": card.get("displayTitle") or card.get("title"),
                    "type": card.get("type"),
                    "xsec_token": item.get("xsecToken") or card.get("xsecToken"),
                })
            return {"data": {"notes": notes, "cursor": str(len(raw)), "has_more": len(raw) > cursor}}
        except RecallError:
            raise
        except Exception as exc:
            raise RecallError("SOURCE_FORMAT_CHANGED", "无法读取小红书收藏页面，请检查登录或页面验证。") from exc

    def visible_cards(self, selector: str) -> list[dict[str, Any]]:
        return self.page.locator(selector).evaluate_all(
            "(nodes) => nodes.map(n => ({title: n.innerText, href: n.getAttribute('href')}))"
        )
