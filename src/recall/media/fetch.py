"""Bounded, anonymous downloads for archive inputs."""
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import quote, urljoin, urlsplit

from ..errors import RecallError


def fetch_public(url, output, *, limit, media_types, accept=None, referer=None):
    import urllib3

    deadline = time.monotonic() + 45
    for _ in range(5):
        try:
            parts = urlsplit(url)
            port = 443 if parts.scheme == "https" else 80
            hostname = parts.hostname.encode("idna").decode("ascii") if parts.hostname else ""
            if (parts.scheme not in ("http", "https") or not hostname
                or parts.username is not None or parts.password is not None or parts.port not in (None, port)):
                raise ValueError
            records = socket.getaddrinfo(hostname.rstrip("."), port, type=socket.SOCK_STREAM)
            resolved = [ipaddress.ip_address(record[4][0]) for record in records]
            addresses = [
                address for address in resolved if address.is_global and not address.is_multicast
                and not (address.version == 6 and (address.ipv4_mapped or address.sixtofour or address.teredo))
            ]
            if not addresses:
                raise ValueError
        except ValueError as exc:
            raise RecallError("PERMISSION_DENIED", "自动归档仅访问公开 HTTP(S) 地址。") from exc
        except OSError as exc:
            raise RecallError("NETWORK_ERROR") from exc

        address = str(min(addresses, key=lambda address: address.version))
        if parts.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(address, port, server_hostname=hostname.rstrip("."),
                                               assert_hostname=hostname.rstrip("."), cert_reqs="CERT_REQUIRED")
        else:
            pool = urllib3.HTTPConnectionPool(address, port)
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parts.query:
            path += "?" + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~[]")
        host_header = f"[{hostname}]" if ":" in hostname else hostname
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecallError("NETWORK_ERROR")
            headers = {"Host": host_header, "Accept-Encoding": "identity", "User-Agent": "RecallAI-Archive/1.0"}
            if accept:
                headers["Accept"] = accept
            if referer:
                headers["Referer"] = referer
            response = pool.urlopen(
                "GET", path, headers=headers,
                redirect=False, retries=False, preload_content=False,
                timeout=urllib3.Timeout(total=remaining, connect=7, read=10),
            )
            with response:
                if response.status in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                    target = urljoin(url, response.headers["Location"])
                    if parts.scheme == "https" and urlsplit(target).scheme != "https":
                        raise RecallError("PERMISSION_DENIED", "不跟随 HTTPS 降级重定向。")
                    url = target
                    continue
                if response.status == 429:
                    raise RecallError("RATE_LIMITED")
                if response.status >= 500:
                    raise RecallError("NETWORK_ERROR")
                if response.status == 404:
                    raise RecallError("NOT_FOUND")
                if response.status != 200:
                    raise RecallError("PERMISSION_DENIED")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                if not any(content_type == kind or (kind.endswith("/") and content_type.startswith(kind))
                           for kind in media_types):
                    raise RecallError("SOURCE_FORMAT_CHANGED", "归档响应不是支持的文件类型。")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise RecallError("SOURCE_FORMAT_CHANGED", "归档仅接受未压缩响应。")
                size = 0
                for chunk in response.stream(65536, decode_content=False):
                    size += len(chunk)
                    if time.monotonic() >= deadline:
                        raise RecallError("NETWORK_ERROR")
                    if size > limit:
                        raise RecallError("INVALID_ARGUMENT", "归档文件超过大小上限。")
                    output.write(chunk)
                return
        except urllib3.exceptions.HTTPError as exc:
            raise RecallError("NETWORK_ERROR") from exc
        finally:
            pool.close()
    raise RecallError("PERMISSION_DENIED", "归档链接重定向次数过多。")
