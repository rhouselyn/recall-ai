"""User-selected chat model, shared by the connection test and real answers."""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import RecallError
from .secrets import read_credential, write_credential

DEEPSEEK_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
OLLAMA_URL = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class ModelSettings:
    mode: str = "auto"
    base_url: str = DEEPSEEK_URL
    model: str = DEFAULT_MODEL
    api_key: str = field(default="", repr=False)
    allow_private: bool = False

    def public(self):
        return {"mode": self.mode, "base_url": self.base_url, "model": self.model,
                "has_key": bool(self.api_key), "allow_private": self.allow_private,
                "configured": self.mode != "auto"}


def load_settings(path: Path, legacy_key) -> ModelSettings:
    if path.exists():
        try:
            return ModelSettings(**json.loads(read_credential(path) or "null"))
        except (ValueError, TypeError):
            return ModelSettings(mode="search")
    if key := legacy_key():
        return ModelSettings(mode="cloud", model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
                             api_key=key, allow_private=os.environ.get("RECALL_CLOUD_INCLUDE_RESTRICTED") == "1")
    if model := os.environ.get("RECALL_OLLAMA_MODEL"):
        return ModelSettings(mode="ollama", model=model)
    return ModelSettings()


def parse_settings(data, current: ModelSettings) -> ModelSettings:
    mode = data.get("mode")
    if mode not in ("search", "cloud", "ollama"):
        raise RecallError("INVALID_ARGUMENT", "请选择模型运行方式。")
    if mode == "search":
        return ModelSettings(mode=mode)

    def setting(name, limit, label):
        value = data.get(name, "")
        if (not isinstance(value, str) or len(value) > limit
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise RecallError("INVALID_ARGUMENT", f"{label} 格式无效。")
        return value.strip()

    model = setting("model", 256, "模型名称")
    if mode == "ollama":
        return ModelSettings(mode=mode, model=model)

    base_url = setting("base_url", 2048, "API 地址").rstrip("/")
    try:
        url = urlsplit(base_url)
        valid = (url.hostname and not url.username and not url.password and "@" not in url.netloc
                 and not any(char in base_url for char in "?#\\") and not any(char.isspace() for char in base_url)
                 and (url.scheme == "https" or (
                     url.scheme == "http" and url.hostname in ("127.0.0.1", "::1", "localhost")
                 )) and (url.port is None or url.port > 0))
    except ValueError:
        valid = False
    if not valid:
        raise RecallError("INVALID_ARGUMENT", "API 地址应使用 HTTPS，本机服务可使用 HTTP。")
    base_url = base_url.removesuffix("/chat/completions").rstrip("/")
    key = setting("api_key", 4096, "API 密钥")
    if not key and current.mode == "cloud" and base_url == current.base_url:
        key = current.api_key
    if not key or not key.isascii():
        raise RecallError("INVALID_ARGUMENT", "请输入此 API 地址的有效密钥。")
    if not model:
        raise RecallError("INVALID_ARGUMENT", "请输入模型名称。")
    allow_private = data.get("allow_private", False)
    if not isinstance(allow_private, bool):
        raise RecallError("INVALID_ARGUMENT", "请选择是否允许发送私密内容。")
    return ModelSettings(mode, base_url, model, key, allow_private)


def save_settings(path: Path, settings: ModelSettings):
    try:
        write_credential(path, json.dumps(asdict(settings), ensure_ascii=False))
    except OSError as exc:
        raise RecallError("CONFIG_ERROR", "无法保存模型配置，请检查本地数据目录的写入权限。") from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_json(opener, url, *, payload=None, key="", timeout):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    try:
        request = urllib.request.Request(url, data=data, headers=headers)
        with opener.open(request, timeout=timeout) as response:
            try:
                raw = response.read()
            except http.client.IncompleteRead as exc:
                raw = exc.partial
    except urllib.error.HTTPError as exc:
        hints = {401: "密钥无效或缺少权限", 402: "账户余额不足", 403: "服务拒绝访问",
                 404: "找不到接口或模型", 429: "请求频率受限"}
        hint = hints.get(exc.code, "请检查 API 地址和模型名称")
        raise RecallError("NETWORK_ERROR", f"模型接口返回 HTTP {exc.code}（{hint}）。") from exc
    except (OSError, ValueError) as exc:
        raise RecallError("NETWORK_ERROR", "无法连接模型，请检查服务地址、网络或本机 Ollama 是否已启动。") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RecallError("SOURCE_FORMAT_CHANGED", "模型接口没有返回有效的 JSON。") from exc


def complete(settings: ModelSettings, messages, *, test=False):
    timeout = 30 if test else 120
    model = settings.model
    if settings.mode == "cloud":
        host = urlsplit(settings.base_url).hostname
        handlers = [NoRedirect()]
        if host in ("127.0.0.1", "::1", "localhost"):
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        payload = {"model": model, "stream": False, "messages": messages}
        if host == "api.openai.com":
            payload["max_completion_tokens"] = 1024
        else:
            payload["max_tokens"] = 128 if test else 1000
        if host == "api.deepseek.com":
            payload["thinking"] = {"type": "disabled"}
        result = request_json(opener, settings.base_url + "/chat/completions", payload=payload,
                              key=settings.api_key, timeout=timeout)
    else:
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
        tags = request_json(opener, OLLAMA_URL + "/api/tags", timeout=2)
        found = tags.get("models", []) if isinstance(tags, dict) else []
        if not isinstance(found, list):
            raise RecallError("SOURCE_FORMAT_CHANGED", "Ollama 未返回有效的本机模型列表。")
        installed = [name for tag in found if isinstance(tag, dict)
                     and isinstance(name := tag.get("name"), str)
                     and not (tag.get("remote_model") or tag.get("remote_host")
                              or name.endswith(("-cloud", ":cloud")))]
        if not model and installed:
            model = installed[0]
        if model not in installed and model + ":latest" in installed:
            model += ":latest"
        if model not in installed:
            raise RecallError("CONFIG_ERROR", "未找到本机 Ollama 模型，请先下载模型或检查模型名称。")
        result = request_json(opener, OLLAMA_URL + "/api/chat", timeout=timeout, payload={
            "model": model, "stream": False, "options": {"num_predict": 32 if test else 384, "temperature": 0},
            "messages": messages,
        })
    try:
        message = result["choices"][0]["message"] if settings.mode == "cloud" else result["message"]
        reply = message.get("content")
        if isinstance(reply, str) and reply.strip():
            return reply
        if test and message.get("reasoning_content"):
            return ""
    except (AttributeError, KeyError, IndexError, TypeError):
        pass
    raise RecallError("SOURCE_FORMAT_CHANGED", "模型未返回可用文本，请检查模型名称和接口兼容性。")
