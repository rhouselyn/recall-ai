"""在 Windows 当前用户下注册 Recall 的 Native Messaging 主机（开发规格 §5.6）。

用法：
  python register_windows.py --extension-id <ID> --python <venv python.exe> --browser all
清单只允许指定扩展 ID；主机路径写到当前用户的注册表，不需要管理员权限。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HOST_NAME = "com.recallai.bookmarks"
REGISTRY_PATHS = {
    "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
}


def write_launcher(
    manifest_dir: Path, python_path: Path, source_id: str | None, db_path: str | None
) -> Path:
    """Windows 的主机清单 path 指向可执行文件；用 .cmd 包装 Python 模块入口。"""
    launcher = manifest_dir / "recall-native-host.cmd"
    if source_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", source_id):
        raise ValueError("来源 ID 只能包含字母、数字、点、下划线和连字符。")
    if any(char in str(value) for value in (python_path, db_path or "") for char in ('"', '%', '\n', '\r')):
        raise ValueError("启动器路径包含不支持的字符。")
    lines = ["@echo off", "setlocal DisableDelayedExpansion", "chcp 65001 >nul"]
    if db_path:
        lines.append(f'set "RECALL_DB={db_path}"')
    command = f'"{python_path}" -m recall.native_host'
    if source_id:
        command += f' --source "{source_id}"'
    lines.append(command)
    launcher.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return launcher


def build_manifest(
    python_path: Path, extension_id: str, source_id: str | None,
    launcher: Path,
) -> dict:
    origin = f"chrome-extension://{extension_id}/"
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("扩展 ID 必须是浏览器显示的 32 位字母。")
    return {
        "name": HOST_NAME,
        "description": "Recall AI 本地书签写入主机",
        "path": str(launcher),
        "type": "stdio",
        # Chrome / Edge 只认 allowed_origins；不写 Firefox 专用的 allowed_extensions。
        "allowed_origins": [origin],
    }


def register(browser: str, manifest_path: Path) -> None:
    try:
        import winreg
    except ImportError as exc:
        raise RuntimeError("仅支持 Windows 注册") from exc
    key_path = REGISTRY_PATHS[browser] + "\\" + HOST_NAME
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValue(key, "", winreg.REG_SZ, str(manifest_path))
    print(f"已注册 {browser}: HKCU\\{key_path} -> {manifest_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extension-id", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--browser", choices=["chrome", "edge", "all"], default="all")
    parser.add_argument("--source", default=None, help="可选的固定来源 ID，扩展不能覆盖")
    parser.add_argument("--db", default=None, help="可选地固定 RECALL_DB")
    parser.add_argument("--manifest-out", default=None)
    parser.add_argument("--no-register", action="store_true", help="只生成清单，不写注册表")
    args = parser.parse_args(argv)

    manifest_dir = Path(args.manifest_out) if args.manifest_out else Path(__file__).parent
    manifest_dir = manifest_dir.resolve()  # 浏览器要求清单 path 是绝对路径
    manifest_dir.mkdir(parents=True, exist_ok=True)
    launcher = write_launcher(manifest_dir, Path(args.python), args.source, args.db)
    manifest_path = manifest_dir / f"{HOST_NAME}.json"
    manifest_path.write_text(
        json.dumps(
            build_manifest(Path(args.python), args.extension_id, args.source, launcher),
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    browsers = ["chrome", "edge"] if args.browser == "all" else [args.browser]
    if args.no_register:
        print(f"已生成清单：{manifest_path}（未写入注册表）")
        return 0
    for browser in browsers:
        register(browser, manifest_path)
    print("提示：扩展 ID 以浏览器实际加载后的值为准；清单只允许该 ID 连接主机。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
