# Recall AI（一期）

## 启动网页工作台

在项目目录安装依赖并启动本地服务：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[browser,media,youtube]"
.\.venv\Scripts\python.exe -m recall serve
```

也可以双击 `启动 Recall AI.bat`，或在浏览器打开 http://127.0.0.1:8765。
服务运行时自动刷新已启用的账号来源。
Chrome 和 Edge 可在网页导入书签 HTML，也可按下文安装扩展。其他平台在
「账号连接」中发起登录和同步，GitHub 使用只读 PAT。网页账号需要本机 Chrome
或 Playwright 安装的 Chromium。部分网页平台需要填写本人用户 ID，知乎私密夹
还需要填写收藏夹 ID。平台页面和第三方接口会变动，实际可读范围以同步结果为准。

YouTube 在「账号连接」中登录自己的 Google / YouTube 账号；保持网络可访问
YouTube，关闭登录窗口后开始同步。连接器使用 yt-dlp 和本机隔离浏览器会话读取
稍后观看、喜欢的视频及页面发现的播放列表，按私密数据处理。平台限制、视频
不可用或页面变化可能导致部分列表失败；不会因为部分读取失败删除旧收藏。
服务运行时约每 5 分钟检查一次；平台未提供推送，此处是轮询而非即时推送。
B 站收藏很多时按批次续传，服务运行时约每 2 分钟刷新。

对话使用 DeepSeek，发送问题、最近对话和检索到的相关收藏片段至 DeepSeek。
Windows 下可使用当前用户的 DPAPI 凭据，或通过环境变量 `DEEPSEEK_API_KEY`
提供密钥。默认模型为 `deepseek-flash`，可用 `DEEPSEEK_MODEL` 覆盖。
密钥不可用时尝试本地 Ollama，未配置模型时返回本地检索结果。
Windows 下 GitHub 只读令牌使用当前用户的 DPAPI 加密保存。其他系统暂存于
服务进程中，也可通过 `RECALL_GITHUB_TOKEN` 提供。
网页支持选择本地图片、音频或视频进行识别，单个文件上限为 20 MiB；媒体副本
保存在数据库所在目录的 `media` 子目录。更大的文件可以通过 CLI 处理。

按 `RECALL_AI_DEVELOPMENT.md` v0.2 实现的本地收藏目录。第一阶段只回答「自己的东西能不能找回来」：
搜索只覆盖**已导入或已同步**的数据，不混入平台全站搜索；只有元数据时结果也只是元数据。

## 环境与安装

Python ≥ 3.11，首验环境 Windows PowerShell。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .            # 只用 CLI / Python 即可
python -m pip install -e ".[mcp]"    # 需要 MCP stdio
python -m pip install -e ".[browser]"# 需要登录网页连接器
python -m pip install -e ".[youtube]"# 需要 YouTube 播放列表连接器（另需 browser）
python -m pip install -e ".[media]"  # 需要本地 OCR / ASR
```

数据库默认路径：Windows `%LOCALAPPDATA%\RecallAI\recall.db`；macOS
`~/Library/Application Support/RecallAI/recall.db`；Linux
`${XDG_DATA_HOME:-~/.local/share}/recall-ai/recall.db`。用 `RECALL_DB` 覆盖，
CLI 与 MCP 使用同一个解析函数。`recall init` 幂等；只读查询不会创建空库。
`PRAGMA user_version = 1`，升级前自动备份，旧程序遇到新版库会明确报错。

## 快速开始

```powershell
recall init
recall import bookmarks .\chrome-bookmarks.html --source chrome-personal --browser chrome
recall import bookmarks .\edge-favorites.html --source edge-work --browser edge --replace
recall import csv .\favorites.csv --source manual-all --platform web
recall add https://example.com/jp-market --source web-manual --platform web --title "日本电商调研"
recall sources --json
recall collections --source chrome-personal --json
recall search "日本 电商" --limit 5 --json
recall get itm_1 --json
```

HTML 默认合并，`--replace` 表示这是该来源的新完整导出（异常空快照、损坏或截断会拒绝替换）。
CSV 是 Recall 通用交换格式（UTF-8，可带 BOM，只有 `url` 必填），永远增量合并。
`--platform` 是手动来源的渠道标签，内容平台仍从链接识别。
查询参数：`--source` 可重复、`--platform`、`--collection`、`--saved-after`、
`--saved-before`、`--limit`、`--cursor`；受限条目需显式 `--include-restricted`。
CLI 失败非零退出，`--json` 模式返回 `{"error":{"code","message","retryable"}}`。

Python 普通 loop（不依赖任何 Agent 框架）：

```python
from recall import RecallTools

with RecallTools.open(db_path, allowed_sources={"chrome-personal", "github-personal"}) as tools:
    cursor = None
    while True:
        page = tools.recall_search(query="日本 电商", limit=10, cursor=cursor)
        for item in page["items"]:
            print(item["title"], item["url"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
```

四个工具：`recall_list_sources` / `recall_list_collections` / `recall_search` /
`recall_get_item`。它们完全只读，不能配置账号、传路径、取凭据、同步或联网取正文。

## MCP stdio

`RECALL_ALLOWED_SOURCES` 必须由用户明确配置，缺失即启动失败；默认不暴露 `restricted`，
可信宿主可显式设置 `RECALL_INCLUDE_RESTRICTED=1`。`pyproject.toml` 的 `[mcp]`
使用官方 SDK v2 稳定线，联调验证版本为 `mcp==2.3.0`，发布前请固定该版本。

```json
{
  "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
  "args": ["-m", "recall.mcp_server"],
  "env": {
    "RECALL_DB": "C:\\path\\to\\recall.db",
    "RECALL_ALLOWED_SOURCES": "chrome-personal,github-personal"
  }
}
```

stdout 只用于协议；日志写 stderr，只记录来源 ID、数量和错误码。正常结果带
`structuredContent` 且保留文本结果；工具执行错误使用 `isError`。

## 账号来源与刷新

```powershell
recall source add github-personal --kind github_stars
$env:RECALL_GITHUB_TOKEN = '<细粒度 PAT>'
recall sync github-personal --full
recall watch                       # 前台持续运行
recall source pause github-personal
recall source resume github-personal
```

凭据只由同步进程从环境变量读取，不进命令参数、SQLite、MCP 配置或日志。
`recall watch` 与手动 `recall sync` 共用同一个租约；GitHub 默认每 90 秒读头部、
每 12 小时完整对账。失败按 401 / 账号不匹配 / 限流 / 格式变化分别报告，
旧副本继续可查。`--accept-empty` 用于显式接受异常空快照。

`zhihu_api`、`zhihu_web`、`bilibili_favorites`、`xiaohongshu_favorites`、
`douyin_favorites` 的连接器代码已按规格实现（分页、循环检测、额度预算、
会话校验、覆盖范围标记），但**协议字段和私密覆盖范围必须用本人账号验收**，
测试套件默认不连外网、不使用用户会话。未取得权限时请继续用 CSV 和单条链接补充。
登录浏览器连接器需要先 `recall source connect <id>`，Playwright 为可选依赖。

## 浏览器扩展（Chrome / Edge）

`extensions/bookmarks/` 是共用 Manifest V3 扩展，只申请
`bookmarks` / `storage` / `alarms` / `nativeMessaging`。事件合并约 2 秒后发送
完整书签树；`storage.local` 的待同步标记只有收到主机对同一版本的提交确认才清除。

1. 在浏览器加载未打包扩展，记下扩展 ID，在扩展选项页填写来源 ID（如 `chrome-personal`）。
2. 执行 `recall source add chrome-personal --kind bookmarks_live --provider chrome`。
3. 注册主机（当前用户，无需管理员）：
   `python extensions\bookmarks\register_windows.py --extension-id <ID> --python <venv>\Scripts\python.exe --source chrome-personal --browser all`
   只生成清单、暂不写注册表时加 `--no-register --manifest-out <目录>`。
4. 重载扩展。启动、重连、定时校验和 `onImportEnded` 会补发最新树。

主机 `python -m recall.native_host` 校验绑定、包结构和大小，写入同一个库并返回版本号。
书签落到它所在的目录；空目录会保留，书签自己的标题不会变成收藏夹名。

## 本地媒体提取

```powershell
$env:RECALL_ASR_MODEL_DIR = 'C:\Users\14815\Projects\wechat\models\para-zh'
recall media index itm_1 .\sample.png
recall media index itm_1 .\sample.wav --entry-id ent_xxx
recall media run --limit 10
```

OCR 用 `rapidocr-onnxruntime`，ASR 用 `sherpa-onnx` + PyAV 解码重采样到
16 kHz 单声道，长音频按窗口分段。片段继承收藏关系的可见范围，查询时参与
匹配并在详情标注「图片识别」/「音频转写」/「平台字幕」。模型缺失时任务标记
`unavailable`，不影响收藏元数据。同一内容存在多条关系时必须用 `--entry-id`
指定证据来源，避免把私密附件挂到公开关系上。

## 备份与维护

```powershell
recall backup .\recall-backup.db
recall restore .\recall-backup.db --to .\restored.db
recall source remove old-source --yes
```

备份用 SQLite 在线备份接口，目标已存在拒绝覆盖；恢复前先校验版本与完整性，
并要求停止读写。删除来源只清本地数据，不触碰平台账号；此前备份仍可能含已删除内容。

## 测试

```powershell
python -m unittest discover -s tests -v      # 28 个用例，全离线
python tests\mcp_stdio_probe.py              # 需要已安装 mcp SDK，走真实 stdio 往返
```

测试只用脱敏样例和临时数据库，不连外网、不使用真实账号。关键回归覆盖：
跨来源去重与证据页、受限来源不可绕过、中文两字与 `C++` / `%` 查询、
cursor 与 `STALE_CURSOR`、损坏书签与不安全 URL、GitHub 多页/断线/账号变化、
固定时钟调度与租约、书签树目录归属、媒体片段权限。

导入报告里的 `collections_added` 是本次新建的夹数，`empty_collections` 是其中
合并后确实没有条目的夹数（例如 Chrome 的空目录），两者分开统计。

## 交付状态

| 任务 | 状态 |
| --- | --- |
| R01–R05 骨架、导入、查询、CLI/Python、MCP | 已实现并有离线测试 |
| R06 GitHub Stars、R08 备份删除、R09 刷新调度 | 已实现并有模拟测试 |
| R07 / R11–R14 平台连接器、R10 扩展、R15 媒体 | 代码已实现；真账号覆盖、浏览器实测和模型权重需本机验收 |
| R16 一期联调 | 需按 §9 用本人账号与真实导出文件逐项记录 |
