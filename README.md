# Recall AI

> 把散落在各平台的收藏，收进一个本地、可搜索、能对话的个人知识库。

## 这是什么

你的收藏散落在 Chrome 书签、Edge 收藏夹、B 站、知乎、小红书、抖音、YouTube、GitHub Stars……想找的时候，只能挨个平台去翻。

**Recall AI 是一个完全运行在本地的收藏聚合与检索服务**：把各平台的收藏同步/导入到一个本地 SQLite 库，提供统一的全文检索；内置网页工作台；对话接 DeepSeek（或本地 Ollama），你可以用自然语言问「我收藏过的那个讲日本电商的链接是哪个」，它先检索你的收藏，再把相关片段交给模型回答。

两个明确的边界：

- **只找你自己的东西**：搜索只覆盖已导入或已同步的数据，不混入平台全站搜索；条目只有元数据时，结果也只是元数据。
- **收藏存储在本机**：使用 DeepSeek 时，会发送你的问题、最近对话和检索到的相关收藏片段；使用本地 Ollama 时，这些内容留在本机。

## 功能特性

- **多平台收藏聚合**：Chrome / Edge 书签、GitHub Stars、B 站收藏、知乎收藏（API / 网页两种方式）、小红书、抖音、YouTube（稍后观看 / 喜欢 / 播放列表）、RSS；也支持 CSV 批量导入和单条链接录入
- **统一检索**：中文分词友好（两字词、`C++`、`%` 都能查），可按来源、平台、收藏夹、保存时间过滤，游标分页
- **AI 对话**：DeepSeek API 或本地 Ollama；密钥不可用时自动降级为纯本地检索
- **本地媒体识别**：图片 OCR、音频/视频转写，片段参与检索（可选组件）
- **MCP 服务**：暴露四个只读工具，可接入任何支持 MCP 的 AI 客户端（如 Claude Desktop）
- **浏览器扩展**：Chrome / Edge 书签变动实时同步（Manifest V3 + native messaging）
- **定时自动同步**：`recall watch` 前台常驻，按来源调度刷新
- **备份与恢复**：SQLite 在线备份接口，升级前自动备份

## 快速开始

Windows（首验环境，PowerShell）：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[browser,media,youtube]"
.\.venv\Scripts\python.exe -m recall init
.\.venv\Scripts\python.exe -m recall serve
```

打开 http://127.0.0.1:8765 即是网页工作台；也可以双击 `启动 Recall AI.bat`。服务运行时自动刷新已启用的账号来源。

最短路径：网页里导入一份 Chrome/Edge 书签 HTML，然后在搜索框里搜点什么；或者在「账号连接」里发起平台登录同步。

## 安装

要求 Python ≥ 3.11，首验环境 Windows PowerShell。按需选装 extras：

```powershell
python -m pip install -e .             # 只用 CLI / Python 即可
python -m pip install -e ".[mcp]"      # 需要 MCP stdio
python -m pip install -e ".[browser]"  # 需要登录网页连接器
python -m pip install -e ".[youtube]"  # 需要 YouTube 播放列表连接器（另需 browser）
python -m pip install -e ".[media]"    # 需要本地 OCR / ASR
```

数据库默认路径：

| 系统 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\RecallAI\recall.db` |
| macOS | `~/Library/Application Support/RecallAI/recall.db` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/recall-ai/recall.db` |

用 `RECALL_DB` 覆盖，CLI、网页与 MCP 共用同一解析函数。`recall init` 幂等；只读查询不会创建空库。库版本 `PRAGMA user_version = 1`，升级前自动备份，旧程序遇到新版库会明确报错。

## 使用指南

### 网页工作台

`recall serve`（或双击 `启动 Recall AI.bat`）后访问 http://127.0.0.1:8765：

- **搜索 / 对话**：主界面即搜即问；已配置 DeepSeek 密钥时，对话会发送问题、最近对话和检索片段。未配置密钥时尝试本地 Ollama；模型调用失败则返回本地检索结果。密钥通过 `DEEPSEEK_API_KEY` 配置。
- **账号连接**：GitHub 填只读 PAT；网页平台在页面里发起登录和同步（需要本机 Chrome 或 Playwright 安装的 Chromium）；部分平台需填写本人用户 ID，知乎私密夹还需收藏夹 ID。平台页面和第三方接口会变动，实际可读范围以同步结果为准。
- **媒体识别**：选择本地图片、音频或视频进行识别，单个文件上限 20 MiB；媒体副本保存在数据库所在目录的 `media` 子目录，更大的文件走 CLI。

### CLI 导入与查询

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

- HTML 默认合并，`--replace` 表示这是该来源的新完整导出（异常空快照、损坏或截断会拒绝替换）。
- CSV 是 Recall 通用交换格式（UTF-8，可带 BOM，只有 `url` 必填），永远增量合并。
- `--platform` 是手动来源的渠道标签，内容平台仍从链接识别。
- 查询过滤：`--source`（可重复）、`--platform`、`--collection`、`--saved-after`、`--saved-before`、`--limit`、`--cursor`；受限条目需显式 `--include-restricted`。
- CLI 失败非零退出，`--json` 模式返回 `{"error":{"code","message","retryable"}}`。

也可以在 Python 里直接用工具层（不依赖任何 Agent 框架）：

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

四个工具：`recall_list_sources` / `recall_list_collections` / `recall_search` / `recall_get_item`。完全只读，不能配置账号、传路径、取凭据、同步或联网取正文。

### 平台账号同步

```powershell
recall source add github-personal --kind github_stars
$env:RECALL_GITHUB_TOKEN = '<细粒度 PAT>'
recall sync github-personal --full
recall watch                       # 前台持续运行
recall source pause github-personal
recall source resume github-personal
```

- GitHub 凭据通过 `RECALL_GITHUB_TOKEN` 或网页输入，不进命令参数、SQLite、MCP 配置或日志。Windows 网页输入的 GitHub 令牌用当前用户 DPAPI 加密保存；其他系统暂存于服务进程。DeepSeek 密钥通过 `DEEPSEEK_API_KEY` 配置，也兼容读取已存在的 DPAPI 密钥文件；当前网页没有 DeepSeek 密钥设置入口。
- `recall watch` 与手动 `recall sync` 共用同一个租约；GitHub 默认每 90 秒读头部、每 12 小时完整对账。失败按 401 / 账号不匹配 / 限流 / 格式变化分别报告，旧副本继续可查。`--accept-empty` 用于显式接受异常空快照。
- 服务运行时自动轮询：GitHub 约每 90 秒、YouTube 约每 5 分钟，B 站收藏多时按批次续传、约每 2 分钟刷新。平台未提供推送，是轮询而非即时推送。
- YouTube 在「账号连接」中登录自己的 Google / YouTube 账号，关闭登录窗口后开始同步；连接器用 yt-dlp 和本机隔离浏览器会话读取稍后观看、喜欢的视频及页面发现的播放列表，按私密数据处理。平台限制、视频不可用或页面变化可能导致部分列表失败；不会因为部分读取失败删除旧收藏。
- `zhihu_api`、`zhihu_web`、`bilibili_favorites`、`xiaohongshu_favorites`、`douyin_favorites` 的连接器已按规格实现（分页、循环检测、额度预算、会话校验、覆盖范围标记），但**协议字段和私密覆盖范围必须用本人账号验收**；测试套件默认不连外网、不使用用户会话。未取得权限时请继续用 CSV 和单条链接补充。
- 登录浏览器连接器需要先 `recall source connect <id>`，Playwright 为可选依赖。

浏览器资料目录按数据库路径和来源 ID 的哈希隔离（标点、大小写不同的来源不会共用登录）。旧目录只有 `identity.json` 的归属标签完全匹配才自动迁移，否则需要重新连接；抖音必须观察到本人身份响应才能同步，手填 ID 只用于核对。

GitHub 和 B 站全量同步会持久保存分页进度与整轮已见证据，后台下一轮继续；B 站连续两轮完整结果都缺失才停用关系。GitHub 缺失的公开仓库需再次确认可访问才停用，私库或权限不明的旧证据保留并按受限内容处理；这类结果会报告 `MISSING_ENTRY_ACCESS_UNVERIFIED`。知乎首屏轮换已知收藏夹，全量保存夹内位置；仍只声明已读取的子集。

知乎 API 默认每天最多发出 **20 次请求**，同一个数据库的来源共用计数，UTC 零点重置；失败请求也计数，重启不会清零。这是本地保护上限，不代表平台免费额度。可通过来源配置 `--config daily_request_limit=20` 调整，`0` 禁止发出请求。

平台收藏夹和实时书签使用稳定夹 ID，名称仅用于展示与搜索。能唯一对应且覆盖旧夹证据时自动沿用旧 ID，保留媒体；历史同名混合或旧名称无法确定归属时保留旧证据，避免猜测迁移。

### MCP stdio

`RECALL_ALLOWED_SOURCES` 必须由用户明确配置，缺失即启动失败；默认不暴露 `restricted`，可信宿主可显式设置 `RECALL_INCLUDE_RESTRICTED=1`。`pyproject.toml` 的 `[mcp]` 固定使用经 stdio 联调验证的 `mcp==2.3.0`；已有 v1 环境需重新安装 `.[mcp]`。

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

stdout 只用于协议；日志写 stderr，只记录来源 ID、数量和错误码。正常结果带 `structuredContent` 且保留文本结果；工具执行错误使用 `isError`。

### 浏览器扩展（Chrome / Edge）

`extensions/bookmarks/` 是共用 Manifest V3 扩展，只申请 `bookmarks` / `storage` / `alarms` / `nativeMessaging`。事件合并约 2 秒后发送完整书签树；`storage.local` 的待同步标记只有收到主机对同一版本的提交确认才清除。

1. 在浏览器加载未打包扩展，记下扩展 ID，在扩展选项页填写来源 ID（如 `chrome-personal`）。
2. 执行 `recall source add chrome-personal --kind bookmarks_live --provider chrome`。
3. 注册主机（当前用户，无需管理员）：

   ```powershell
   python extensions\bookmarks\register_windows.py --extension-id <ID> --python <venv>\Scripts\python.exe --source chrome-personal --browser chrome
   ```

   Edge 使用独立来源 ID、`--browser edge` 和独立的 `--manifest-out <目录>`；`--source` 固定主机来源，必须与扩展选项一致。只生成清单、暂不写注册表时加 `--no-register --manifest-out <目录>`。
4. 重载扩展。启动、重连、定时校验和 `onImportEnded` 会补发最新树。

主机 `python -m recall.native_host` 校验绑定、包结构和大小，写入同一个库并返回版本号。书签落到它所在的目录；空目录会保留，书签自己的标题不会变成收藏夹名。

### 本地媒体提取

```powershell
$env:RECALL_ASR_MODEL_DIR = '<本地 ASR 模型目录>'
recall media index itm_1 .\sample.png
recall media index itm_1 .\sample.wav --entry-id ent_xxx
recall media run --limit 10
```

OCR 用 `rapidocr-onnxruntime`，ASR 用 `sherpa-onnx` + PyAV 解码重采样到 16 kHz 单声道，长音频按窗口分段。片段继承收藏关系的可见范围，查询时参与匹配并在详情标注「图片识别」/「音频转写」/「平台字幕」。模型缺失时任务标记 `unavailable`，不影响收藏元数据。同一内容存在多条关系时必须用 `--entry-id` 指定证据来源，避免把私密附件挂到公开关系上。

媒体文件会流式复制到按数据库隔离的 `media` 子目录，再以内容哈希登记任务；修改原文件不会改变已登记内容。旧任务若原文件已变化，报告 `MEDIA_CHANGED`，需重新执行 `media index`。音频逐帧解码，PCM 缓冲最多 30 秒；任务原子领取、每 30 秒续租，崩溃后 5 分钟可恢复，自动最多尝试 3 次。手动重新索引可重试失败或模型不可用的任务。

### 备份与维护

```powershell
recall backup .\recall-backup.db
recall restore .\recall-backup.db --to .\restored.db
recall source remove old-source --yes
```

备份用 SQLite 在线备份接口，目标已存在拒绝覆盖；恢复前先校验版本与完整性，并要求停止读写。删除来源只清本地数据，不触碰平台账号；此前备份仍可能含已删除内容。

删除来源同时清理其浏览器资料和没有其他任务引用的托管媒体副本。正在同步、登录或提取时拒绝删除；文件占用导致清理失败时返回 `cleanup_pending`，关闭占用程序后重跑同一删除命令即可。原始文件、共享副本、无法确认归属的旧文件及全局共享密钥保留；返回的 `retained_files` 列出保留的文件。SQLite 备份不包含浏览器资料或媒体文件，换目录恢复时需要重新连接账号并单独恢复媒体。

## 配置参考

所有配置均可省略，按默认行为运行。

| 环境变量 | 说明 |
| --- | --- |
| `RECALL_DB` | 数据库文件路径，覆盖默认位置 |
| `DEEPSEEK_API_KEY` | DeepSeek 对话密钥（也兼容读取已有 Windows DPAPI 密钥文件） |
| `DEEPSEEK_MODEL` | 对话模型，默认 `deepseek-flash` |
| `RECALL_OLLAMA_MODEL` | 本地 Ollama 模型 |
| `RECALL_GITHUB_TOKEN` | GitHub 只读 PAT（Windows 也可 DPAPI 保存） |
| `RECALL_ALLOWED_SOURCES` | MCP 暴露的来源白名单，逗号分隔，必填 |
| `RECALL_INCLUDE_RESTRICTED` | `1` 时 MCP 暴露 `restricted` 条目（默认不暴露） |
| `RECALL_ASR_MODEL_DIR` | 本地 ASR 模型目录 |

## 测试

```powershell
python -m unittest discover -s tests -v      # 离线 Python 回归测试
node tests/test_frontend.cjs                # 前端与扩展异步回归，无第三方依赖
python tests/benchmark_search.py            # 临时库，20,000 条合成收藏性能检查
python tests\mcp_stdio_probe.py              # 需要已安装 mcp SDK，走真实 stdio 往返
```

测试只用脱敏样例和临时数据库，不连外网、不使用真实账号。关键回归覆盖：跨来源去重与证据页、受限来源不可绕过、中文两字与 `C++` / `%` 查询、cursor 与 `STALE_CURSOR`、损坏书签与不安全 URL、GitHub 多页/断线/账号变化、固定时钟调度与租约、书签树目录归属、媒体片段权限。

导入报告里的 `collections_added` 是本次新建的夹数，`empty_collections` 是其中合并后确实没有条目的夹数（例如 Chrome 的空目录），两者分开统计。

## 项目状态

一期（按 `RECALL_AI_DEVELOPMENT.md` v0.2 实现）的目标是只回答「自己的东西能不能找回来」。

| 任务 | 状态 |
| --- | --- |
| R01–R05 骨架、导入、查询、CLI/Python、MCP | 已实现并有离线测试 |
| R06 GitHub Stars、R08 备份删除、R09 刷新调度 | 已实现并有模拟测试 |
| R07 / R11–R14 平台连接器、R10 扩展、R15 媒体 | 代码已实现；真账号覆盖、浏览器实测和模型权重需本机验收 |
| R16 一期联调 | 需按 §9 用本人账号与真实导出文件逐项记录 |

## 文档

- [RECALL_AI_DEVELOPMENT.md](RECALL_AI_DEVELOPMENT.md) — 开发规格（v0.2）
- [docs/](docs/) — 平台调研与私密收藏覆盖范围研究

## License

个人项目，暂未附开源许可证。
