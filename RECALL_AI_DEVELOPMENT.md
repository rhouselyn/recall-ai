# Recall AI 项目开发文档

**版本 0.2** · **2026-11-10** · 开发规格  
**项目愿景：** From Information to Knowledge, From Knowledge to Action  
**第一阶段：** 把自己的收藏汇入本地，用同一套工具查找、浏览和追溯来源。

## 1. 做成什么样

用户在本机接入 Chrome、Edge、GitHub、知乎、B 站、小红书和抖音自己的收藏来源。浏览器书签改动触发本地更新，平台账号通过本地同步进程持续检查新收藏。用户可以问 Agent「我以前收藏过哪些关于日本电商的内容？」或写普通 Python loop。两种方式都查同一个本地库，返回标题、链接、收藏夹、上一次刷新时间和实际覆盖范围。

第一阶段检验的是「自己的东西能不能找回来」。搜索只覆盖已导入或已同步的数据，不把平台全站搜索混入结果。若还没导入 B 站收藏，回答应该说「当前没有 B 站收藏数据」，不能说「你从未在 B 站收藏过相关内容」。如果仅保存了标题和链接，返回的证据也到此为止。

| 优先级 | 交付 | 验收定义 |
| --- | --- | --- |
| 必做 | Chrome / Edge 书签、GitHub Stars、本地链接或 CSV、跨渠道查询 | 导入真实导出文件；授权 GitHub 同步本人可见的 Stars；按来源、收藏夹和关键词查到结果并打开原链接 |
| 必做 | 本地 Python 工具、命令行与 MCP stdio | 无 LLM、无平台网络时，已入库的数据可由 CLI 和 Python 查出；MCP 客户端可发现四个只读工具并查出同一条记录 |
| 必做 | 同步可观察、导入可重跑、个人数据可管理 | 知道哪些来源已入库、是否只导入了选中条目、上一次更新是否失败；重复导入不重复计数；可删除本地来源、备份和恢复 |
| 必做 | 事件和后台刷新 | Chrome、Edge 监听书签改动。GitHub 走官方接口轮询。每个通过真人验证的平台连接接入同一个本地刷新进程，并报告延迟、断线和覆盖 |
| 分批接入 | 知乎、B 站、小红书、抖音的本人私密收藏 | 逐个平台用真实私密夹或仅自己可见的收藏验收。连接器失效时保留旧副本，用户仍可通过 CSV 和分享链接补充 |
| 一期增量 | 图片 OCR、音视频转文字 | 复用本机已有模型，先用本地文件跑通。已授权可读的媒体可独立排队提取，收藏元数据先可查 |

一期交付覆盖收藏目录、收藏关系、持续刷新和本地媒体文本提取。通用网页正文、语义检索、行动计划和分享在后续阶段完成。连接云端 Agent 时，宿主可能把工具结果传给模型，由用户配置开放范围。

## 2. 各渠道接入决策

`account_sync` 记录本人授权读取，`browser_sync` 记录浏览器书签事件同步，`file_import` 记录导出文件，`manual_capture` 记录用户选择的链接和清单。入口决定覆盖说明，内容平台由链接或平台接口确定。例如，Chrome 书签中的一条 B 站视频仍记作 `platform=bilibili` 和 `source_id=chrome-personal`。

| 渠道 | 持续读取路径 | 覆盖和备选 |
| --- | --- | --- |
| Chrome | `bookmarks.getTree()` 初始化，监听改动，用 Native Messaging 交给本机写库。 | 每个浏览器配置文件独立同步，HTML 用于补导或离线迁移。 [Chrome 书签 API](https://developer.chrome.com/docs/extensions/reference/api/bookmarks) |
| Edge | 与 Chrome 共用扩展代码，注册 Edge 的 Native Messaging 主机。 | 验证本地收藏和已同步收藏的可见范围，保留 HTML 导入。 [Edge Native Messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging) |
| GitHub | `GET /user` 绑定当前用户，`GET /user/starred` 轮询本人 Stars。 | 私有仓库以该令牌的可见权限验收。Star Lists 单独评估。 [GitHub Starring REST](https://docs.github.com/en/rest/activity/starring#list-repositories-starred-by-the-authenticated-user) |
| 知乎 | 公开部分用官方 Access Secret。私密部分优先绑定本人收藏夹链接，通过本机已登录浏览器分页读取。 | 自动发现私密夹需另行验证。已知夹先验归属、登录权限和分页。 [知乎官方收藏夹](https://developer.zhihu.com/docs?key=user_favlists) · [第三方夹内读取实现](https://github.com/handsomestWei/zhihu-fetch-skill/blob/4d1f999e09156c9332aef88143c1327b5b99cbf8/scripts/zhihu_fetch/fetch/collection.py) |
| B 站 | 用独立只读连接器读取网页收藏夹目录和分页内容，登录会话绑定本人 UID。 | 优先验证私密夹和多夹成员关系。个人信息下载是可选导入路径。 [第三方接口记录](https://github.com/bilibili-plugins/bilibili-api-collect/blob/cfc5fddcc8a94b74d91970bb5b4eaeb349addc47/docs/fav/info.md) |
| 小红书 | 专用登录浏览器进入自己的「收藏」和收藏专辑，优先读取页面产生的列表数据。 | `ReaJason/xhs` 用于核对字段和游标。`xhs-favorites` 提供专辑浏览器读取参考。 [xhs 收藏方法](https://github.com/ReaJason/xhs/blob/master/xhs/core.py) · [xhs-favorites](https://github.com/ONEMULE/xhs-favorites) |
| 抖音 | 专用登录浏览器进入「我 → 收藏」。对照第三方 `listcollection` 路径验证本人已收藏视频。 | 收藏视频和收藏合集分开验证，个人信息下载需样本核实。 [TikTokDownloader 收藏实现](https://github.com/JoeanAmier/TikTokDownloader/blob/master/src/interface/collection.py) |

上线状态按每个平台真人验证的覆盖范围标记。公开 RSS 和本人私密收藏分别建来源。第三方代码和 RSS 的核查详见 [私密收藏与 RSS 调研](docs/private-favorites-research.md)，官方接口及导出依据见 [平台接入调研](docs/platform-research.md)。

## 3. 系统结构与运行方式

```text
浏览器书签事件 / HTML / CSV ──→ 写入与去重 ──→ 本地 SQLite
本人 API / 登录网页 ── recall watch ──→ 写入与去重
可访问媒体 ──→ 本地 OCR / ASR ──→ 证据片段 ──→ 本地 SQLite
Agent / Python loop / CLI ──→ 统一查询 ──→ 本地 SQLite
```

- **运行时。** Python 3.11 起，Windows PowerShell 为首验环境。模型冒烟使用本机 Python 3.11.7。macOS 和 Linux 按实际验证结果标注支持情况。
- **存储：** 标准库 `sqlite3`，文件放在本地。SQLite 事务处理导入和同步，目录关系和搜索数据一起提交。第一版用标准库解析 HTML/CSV/JSON、构建 CLI；GitHub HTTP 客户端只在同步模块内。MCP 安装为可选依赖，选 [官方 Python SDK v2 稳定线](https://py.sdk.modelcontextprotocol.io/get-started/installation/)，实现时固定已跑通的版本。使用 [stdio 传输](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)，不启动对外监听端口。
- **查询和同步分开。** Agent 读取本地已提交的数据。`recall watch` 在本机持续刷新已启用的账号源，浏览器书签事件由本地主机接收。媒体提取使用独立进程及 SQLite 待办。更新一旦提交，下次查询可见。
- **代码结构。** `src/recall/` 中的 `catalog.py` 负责读写，`ingest.py` 负责文件与链接，`sources/` 放平台连接器，`watch.py` 负责调度。`tools.py`、`cli.py`、`mcp_server.py` 提供查询入口。`media/` 放 OCR 和 ASR。`tests/fixtures/` 存脱敏样例。

数据库默认路径：Windows `%LOCALAPPDATA%\RecallAI\recall.db`；macOS `~/Library/Application Support/RecallAI/recall.db`；Linux `${XDG_DATA_HOME:-~/.local/share}/recall-ai/recall.db`。用 `RECALL_DB` 覆盖，CLI 与 MCP 要指向同一个文件。测试必须用临时路径。`recall init` 创建或幂等打开当前版本库；只读查询打开现有库，不顺手创建空库。数据库用 `PRAGMA user_version` 追踪结构版本；升级前备份，旧程序遇到新版库应报清楚版本不匹配。

## 4. 数据模型和来源保真

| 数据 | 关键字段与约束 |
| --- | --- |
| `sources` 用户接入的来源实例 | `source_id`、`kind`（`bookmarks_html`、`bookmarks_live`、`manual` 或平台账号类型）、`provider`、`label`、可空的 `remote_account_id`、`sync_enabled`。账号切换用新的来源 ID，源配置只存凭据引用。 |
| `collections`：该来源的收藏夹 | `collection_id`、`source_id`、`external_key`、`path`（字符串数组）、`access`；同一来源内 `external_key` 唯一。平台返回稳定夹 ID 时用它；HTML 和 CSV 用完整路径数组编码，避免单一斜线连接引发碰撞。没有夹信息时用 `[]`。 |
| `items`：可定位的内容 | `item_id`（本地稳定 ID）、`canonical_url`（唯一）、`platform`。内容身份按安全归一后的 URL 合并；不同 URL 是否指向同一内容，第一版只在规则明确时判断。 |
| `entries`：某条内容被收藏一次的证据 | `entry_id`、`item_id`、`collection_id`、`external_key`、`title`、`excerpt`、`note`、`link_url`、`saved_at`（可空）、`imported_at`（首次入库）、`last_seen_at`、`capture`、`access`。同一夹内按稳定条目 ID 或 canonical URL 去重，不同夹或不同来源继续保留各自证据。 |
| `import_runs` 导入与同步记录 | `run_id`、`source_id`、`mode`（`head` / `full` / `event` / `file`）、开始和结束时间、`status`、`coverage`、计数及脱敏错误码。 |
| `sync_state` 刷新进度 | 每个来源一个记录，保存最近检查、增量与完整同步时间、下次运行时间、上次失败类型、最近成功的摘要或 ETag 以及当前租约。 |

`access` 取 `standard` 或 `restricted`。受限仓库、私密夹、用户标记的敏感条目和权限不明的登录来源默认 `restricted`。条目和提取文本继承受限收藏夹的限制。浏览器夹由用户选择是否向 Agent 开放。标题、摘要、收藏夹路径、媒体片段和回跳链接都从调用方可见的关系取出，之后再搜索、聚合与排序。

**链接规则：** 只收 `http` / `https`；拒绝带用户名密码和已知凭据参数的链接，能可靠清理时保留可安全回跳的链接，否则提示用户提供安全地址。仅移除明确的跟踪参数，例如 `utm_*`；未知业务参数、锚点、视频分 P 和文章标识保留。主机名及默认端口规范化；不通过网络展开短链，不因 URL 长得像就合并两篇文章。仓库重命名时，GitHub 同步按仓库 ID 更新本来源的收藏关系；其他来源里保存的旧链接继续保留，第一版不做历史别名追踪。

**时间规则：** `saved_at` 只记录平台或用户提供的收藏时间，未知为 `null`；视频发布时间和导入时间不能拿来充当收藏时间。对外统一 UTC RFC 3339。默认展示近期导入时，用 `imported_at` 补排序，结果里仍区分二者。快照被替换时，旧收藏关系退出默认查询，未被任何关系引用的内容项随写入事务清理。

## 5. 导入和同步行为

### 5.1 HTML、CSV 与单条链接

Chrome、Edge HTML 用树结构保留嵌套目录，解析实体、引号、中文、书签添加时间和空目录；忽略脚本及浏览器私有协议。以两个浏览器实际导出文件的脱敏样例为准；不是书签 HTML 或出现无法可靠恢复的结构时，拒绝整份文件并说明原因。

CSV 是 **Recall 通用交换格式**，不假定各平台能原生导出同样的列。UTF-8，可带 BOM；只有 `url` 必填。支持 `title`、`collection`（一级显示名，可空）、`saved_at`（UTC 或带时区）、`note`、`access`（`standard` / `restricted`）。`--platform` 是该手动来源的渠道标签；条目的内容平台仍按链接识别。若没有标题，展示安全链接的主机和路径，不能把它标为平台返回的标题。示例：

```csv
url,title,collection,saved_at,note,access
https://example.com/jp-market,日本消费者调研,跨境电商,2026-10-09T06:30:00Z,先读消费渠道一节,standard
```

- 手动 CSV、单条链接永远增量合并；新 CSV 没包含的旧条目继续可查。重复导入相同文件不新增内容或收藏关系。显式空字段不覆盖同一来源已有的有效元数据；有效更新也不能把 `restricted` 意外降为 `standard`。
- HTML 默认合并，可显式 `--replace` 表示这是该来源的新完整导出；识别出结构损坏、截断或异常空快照时拒绝替换。工具无法从格式合法的文件推断它是否包含浏览器里的全部数据。确实需要清空来源时由用户执行本地删除命令。
- 输入文件不可解码、表头非法、结构损坏、超过上限时整个导入失败。合并模式下，孤立坏行可跳过并给出行号与错误码，整体标为 `partial`；不安全 URL 属于显式排除项，也要计数。替换时任何意外错误都保留旧数据。
- 首版限制：单文件 20 MiB，单次 20,000 条，URL 2,048 字符，标题 300 字符，摘要或备注 1,000 字符。结构字段超限拒绝该条，展示文字截断并提示；无效文件中的原文和完整 URL 不进日志。

### 5.2 GitHub Stars

1. 用户通过本地 CLI 触发同步。GitHub 凭据由同步进程读取 `RECALL_GITHUB_TOKEN`；不放在命令参数、MCP 客户端配置、SQLite 或日志。官方文档对细粒度令牌列出 `Starring: read`；以真实账号中可配置的最小权限和实际可见性为准。
2. 每次用 [`GET /user`](https://docs.github.com/en/rest/users/users#get-the-authenticated-user) 校验当前身份，第一次成功绑定稳定用户 ID。若已绑定来源换成了其他账号的令牌，报 `ACCOUNT_MISMATCH`，不写入或重新绑定。权限变化不能证明上一次能看到的条目已被用户取消收藏。
3. `/user/starred` 采用 [GitHub 分页规则](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)，`per_page=100`，按 `Link rel="next"` 访问同一受信 API 主机，使用 `application/vnd.github.star+json` 读取 `starred_at`；REST 版本以官方文档示例为基线，开发时通过真实请求确认后锁定。只保存白名单字段，不保存完整响应。
4. 请求串行，单次请求超时 15 秒；对超时或 5xx 最多重试两次。按官方限流响应记录可重试时间，过长就结束本次任务；401、权限 403 和登录失效提示由用户修复后重试。分页循环、响应格式变化、超页数或超条数都算同步失败，不提交半份快照。
5. 完整拉取并校验后原子提交快照。按 §5.7 执行增量与对账。未返回的旧关系可在成功对账后退出默认查询，报表称「本次不可见」。有非空旧快照且新结果为零时，要求用户显式 `--accept-empty`。GitHub 的 `star` Webhook 对仓库、组织或 GitHub App 安装范围生效，因此本人收藏的跨仓库同步使用 `/user/starred` 轮询。 [GitHub Star 事件](https://docs.github.com/en/webhooks/webhook-events-and-payloads#star)

### 5.3 知乎本人公开和私密收藏

1. 首次实测从 [知乎数据开放平台](https://developer.zhihu.com/) 个人中心获取 Access Secret，由同步进程读取 `RECALL_ZHIHU_ACCESS_SECRET`，按官方文档发送 `Authorization: Bearer` 和 `X-Request-Timestamp`。验证该授权如何确认本人身份，能读到什么、配额及字段保留限制是什么。无法可靠读取稳定账号 ID 时，CLI 要求用户为每个账号使用独立来源，不能自行认定新凭据属于同一人。
2. 用 `GET /api/v1/user/favlists` 取得公开范围内返回的夹，用响应里的 `UrlToken` 标识收藏夹；列表接口只明确了 `Limit`。调用 `GET /api/v1/user/favlist_contents` 时将该标识作为 `FavlistUrlToken`，按 `Data.Paging.IsEnd` / `NextOffset` 分页并防循环。近期收藏接口只能作抽查，不能替代按夹同步。
3. 联调先查配额与计费规则，首个测试只读取一个夹的一页。除 HTTP 状态外检查响应 `Code`，区分鉴权、频控和配额耗尽。走完本次计划的分页后合并，标记 `authorized_subset`。开启自动轮询前由用户设定每日请求上限，默认使用免费额度内的预算。超预算暂停。私密夹和未返回夹走网页连接器或手动导入。 [知乎配额与计费](https://developer.zhihu.com/docs?key=billing_price)

私密路线使用单独的 `zhihu_web` 来源。首次在隔离浏览器登录，登记本人私密收藏夹的 `/collection/<id>` 链接。自动发现依赖对已登录收藏页的实测。现有第三方 `/api/v4/members/{slug}/favlists` 路线只声明枚举公开夹。读取已知夹时验证当前会话、收藏夹归属以及可读性，解析网页请求的 `/api/v4/collections/{id}` 与 `/api/v4/collections/{id}/items`，按 `paging.is_end` 和有效的下一页位置推进。响应格式变更时可用页面可见列表并标记子集。用 `answer:<id>`、`article:<id>` 等标识内容。网页读取和官方来源同时命中时合并内容，保留各自证据。 [第三方已知夹读取实现](https://github.com/handsomestWei/zhihu-fetch-skill/blob/4d1f999e09156c9332aef88143c1327b5b99cbf8/scripts/zhihu_fetch/fetch/collection.py) · [zhurl 收藏接口](https://github.com/zly2006/zhurl/blob/main/docs/apis/interactions.md)

### 5.4 B 站私密视频收藏

1. `GET /x/web-interface/nav` 读取 `isLogin` 与 `mid`。`GET /x/v3/fav/folder/created/list-all?up_mid=<mid>` 读本人创建的收藏夹。用 `data.list[].id` 作为完整 `media_id`。逐夹请求 `GET /x/v3/fav/resource/list?media_id=<id>&pn=1&ps=20&platform=web`，读取 `data.medias`，按 `has_more` 翻页。条目按 `bvid` 或 `aid` 识别，有 `fav_time` 才填收藏时间。用 `attr` 和页面样本核对收藏夹可见性，保留跨夹关系。 [收藏夹目录](https://github.com/bilibili-plugins/bilibili-api-collect/blob/cfc5fddcc8a94b74d91970bb5b4eaeb349addc47/docs/fav/info.md) · [内容分页](https://github.com/bilibili-plugins/bilibili-api-collect/blob/cfc5fddcc8a94b74d91970bb5b4eaeb349addc47/docs/fav/list.md) · [开源实测记录](https://github.com/daymade/claude-code-skills/blob/main/bilibili-source/references/bilibili_api.md#favorites-收藏夹-login-required)
2. 连接器先用隔离浏览器让用户登录。确需直接 HTTP 且验证可用时，把用户批准使用的会话字段放入本机系统凭据库，限制在指定 B 站域名。绑定 UID 后每次启动和刷新会话时复核。原 `Nemo2011/bilibili-api` 项目已关闭，接口文档、RSSHub 和第三方调用方用来交叉核对协议。接口失败时可在本人空间收藏页试用网页采集。 [项目关闭记录](https://github.com/Nemo2011/bilibili-api/blob/3798d3b3bd3c3a93678d5a0367637a19262303ef/README.md) · [调用示例](https://github.com/adoresever/bilibili-mcp/blob/main/mcp_server.py)
3. 使用一个私密夹和一个公开夹，分别验证匿名会话、本人会话和退出后行为。样本要覆盖多页、同视频跨夹、取消收藏、登录过期和服务返回空列表。稍后再评估订阅收藏夹和其他内容类型，默认范围是本人创建的视频收藏夹。

### 5.5 小红书与抖音的登录网页连接器

共用一个浏览器读取机制。每个平台账号使用独立的持久化配置目录，首次打开有界面浏览器，由用户自行登录。同步进程读取用户页面正常加载的网络响应。响应没有可用字段时，从可见页面读取卡片并按正常翻页或滚动加载。每次确认当前身份和收藏标签，记录加载结束、页面窗口或分页上限。出现验证码、权限页或页面结构异常时暂停并保留旧结果。第三方程序只作为端点和选择器的实现参考，连接器只保留读取操作。 [Playwright 浏览器认证文档](https://playwright.dev/python/docs/auth)

| 平台 | 首次读取和持续检查 | 完成验收所需的证据 |
| --- | --- | --- |
| 小红书 | 在本人主页以 `tab=fav&subTab=note` 读平铺收藏，以 `subTab=board` 读专辑。用 `GET /api/sns/web/v2/note/collect/page` 对照 `user_id`、`num`、`cursor`，保存 `note_id`、标题、类型和安全回跳链接。专辑进 `/board/<board_id>` 读取，按实测游标或滚动结束信号保存成员关系。 | 用一个仅自己可见的账号收藏或专辑验收。含一篇图文和一条视频，并包含超过首屏的样本。平铺列表覆盖与专辑覆盖分别报告。 [xhs 列表方法](https://github.com/ReaJason/xhs/blob/f4b62d9f8e4078e631fc6e4ec8e430bc711ee9f0/xhs/core.py) · [浏览器读取源码](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/providers/playwright-provider.js#L1024-L1151) |
| 抖音 | 登录后进入「我 → 收藏」，从页面加载中识别 `/aweme/v1/web/aweme/listcollection/`，读取 `aweme_list`、`cursor`、`has_more`。保存 `aweme_id`、描述、媒体类型和回跳链接。自建收藏分组对照 `/aweme/v1/web/collects/list/` 与 `/aweme/v1/web/collects/video/list/`，用 `collects_id` 关联内容。收藏的合集另行验证。 | 手机收藏一条视频后测网页可见与本地落库。覆盖滚动多页、仅本人可见、分组成员、取消收藏和会话过期。直接 HTTP 存在近期 403 报告，首选正常网页加载。 [TikTokDownloader 收藏视频](https://github.com/JoeanAmier/TikTokDownloader/blob/master/src/interface/collection.py) · [收藏分组](https://github.com/JoeanAmier/TikTokDownloader/blob/master/src/interface/collects.py) · [403 变更记录](https://github.com/jiji262/douyin-downloader/blob/main/AGENTS.md) |

### 5.6 Chrome、Edge 书签事件与 RSS

1. Chrome / Edge 使用同一个 Manifest V3 扩展。安装时让用户绑定一个资料档来源。扩展调用 `bookmarks.getTree()` 读取初始树，保存 `node.id`、`parentId`、顺序和 `dateAdded`。浏览器资料档 UUID 与节点 ID 组成身份。浏览器同步带来的外部书签在当前资料档收到变更后进入本地库。 [书签事件与节点字段](https://developer.chrome.com/docs/extensions/reference/api/bookmarks)
2. 监听 `onCreated`、`onRemoved`、`onChanged`、`onMoved`、`onChildrenReordered`，批量导入结束后处理 `onImportEnded`。首版将变化合并后发送完整树，复用事务替换而减少逐事件合并逻辑。扩展用 `storage.local` 记录待同步标记，只有本地主机确认对应版本已提交后才清除。启动、重连或定时校验时补发最新树。记录资料档绑定和单调版本，防止旧快照覆盖。先用实际最大资料档测消息大小和耗时。 [Chrome Alarms](https://developer.chrome.com/docs/extensions/reference/api/alarms)
3. Native Messaging 主机在 Windows 按浏览器注册到当前用户，清单只允许 Recall 扩展 ID。主机校验来源绑定、包结构和大小，写入同一数据库并返回版本号。扩展仅申请 `bookmarks`、`storage`、`alarms`、`nativeMessaging` 所需权限。 [Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging) · [Edge Native Messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging)
4. RSS 适合辅助发现。RSSHub 的 B 站指定夹通常只有 20 条，知乎指定夹依赖已知夹 ID 且分页需补测，小红书用户收藏路由的私密覆盖未证实。抖音 `/douyin/collection` 使用本人登录网页读取首屏收藏视频。若启用 RSS，公开订阅单独归档。本人私密 RSS 限受控本机自托管。按 GUID 或规范链接增量合并，检查 ETag / Last-Modified，记录实际拉取时间。取消收藏由平台连接器对账。 [路由实现核查](docs/private-favorites-research.md) · [RSSHub 抖音收藏源码](https://github.com/DIYgod/RSSHub/blob/89956595df9a690fe1101d727ce4b04ff4a7064f/lib/routes/douyin/collection.ts)

### 5.7 持续刷新和恢复

`recall watch` 按配置刷新已启用的账号来源。以远端页面或接口已经能看到新增为延迟起点。本机运行、账号在线、无挑战且额度足够时，以下是首验目标，实测后再调整频率。浏览器书签使用事件路径，其余平台是近实时轮询。

| 渠道 | 日常发现 | 新收藏可查目标 | 完整对账 |
| --- | --- | --- | --- |
| Chrome / Edge | 书签事件，合并约 2 秒 | p95 ≤ 5 秒 | 启动、重连和每日校验树 |
| GitHub | 每 90 秒读 Stars 头部 | p95 ≤ 3 分钟 | 每 12 小时走完可见分页 |
| B 站 | 每 120 秒检查已选收藏夹，周期刷新夹目录 | p95 ≤ 4 分钟 | 每 6 小时分批验证收藏夹 |
| 知乎私密网页 / 小红书 / 抖音 | 每 180 秒检查本人页面，已选多夹按轮转预算 | 单个已选夹 p95 ≤ 6 分钟 | 每日分批遍历已验证可读范围 |
| 知乎公开 API | 用户设定的额度内，默认每 10 分钟检查近期窗口 | 按配额与轮询间隔实测 | 有额度时逐夹分页，每日封顶 |

- **首次全量。** 绑定本人账号，先以增量任务提交已校验的首批内容，覆盖标为 `authorized_subset`。再单独发起全量任务分页回填，完整提交后才更新完整同步时间。平台没有可靠的末页信号时保持子集标记。
- **增量。** 只有实测确认按收藏时间降序时，才按新到旧扫描，连续命中已知条目且越过上次边界后停止。用集合比对，不用单一时间戳跳过同秒或重新收藏的数据。顺序不稳定时扫描有预算的窗口，并显式报告范围。限流友好地使用服务支持的条件请求，`304` 只更新时间检查字段。每次只提交已验证的批次。
- **对账。** 增量只新增或更新。仅在账号、权限、夹归属和结束信号均确认的完整范围内对账。第三方来源至少两次成功全量都缺失再标为不可见，权限下降、格式变化、分页失败和突降只记录状态。被取消的关系退出查询，其他来源的同一内容继续保留。
- **调度。** 同一来源同一时刻只跑一个任务。`sync` 手动触发也走同一个租约，网络读取在写事务外完成，提交时检查租约和版本。独立来源轮转，每个连接器有每轮请求数、超时和每日额度。未完成对账按下一轮重新开始。休眠唤醒后每个到期来源只补一次。用户需要持续运行时，可将 `recall watch` 以当前用户身份加入 Windows 登录启动任务，保持凭据库和浏览器资料档可访问。
- **失败恢复。** 限流遵守平台返回的重试时间，临时故障指数退避并加抖动。401、账号不匹配、验证码、配额耗尽和格式变化分别报告。需要用户登录时暂停，`recall source resume <id>` 在重新确认身份后恢复。旧数据标记为过期副本。无查询内容变化的刷新不增加目录版本。
- **观察。** `recall sources --json` 提供 `refresh_mode`（`event` / `poll` / `manual`）、`last_checked_at`、`last_success_at`、`last_full_sync_at`、`next_retry_at`、`sync_state` 和覆盖范围。查询工具提供同样的覆盖摘要，并继续离线返回旧结果。`recall sync <id>` 可手动刷新，`recall source pause <id>` 可停止该源的后续检查。

## 6. 统一查询契约（v1）

Python、CLI 和 MCP 都调用同一套查询实现。工具只读；不能配置账号、传文件路径、获取 Cookie/令牌、同步、联网取正文或修改库。工具名及参数在三个入口语义一致。`item_id` 是内容的本地 ID，`collection_id` 是具体来源的收藏夹 ID；调用方先列来源和收藏夹，避免猜 ID。

| 工具名 | 输入 | 输出及用法 |
| --- | --- | --- |
| `recall_list_sources` | 无 | 来源列表及其 `kind`、`provider`、`coverage` 与刷新摘要。给 Agent 确定可查范围和时间。 |
| `recall_list_collections` | `source_id?`, `limit?`, `cursor?` | 可见收藏夹 ID、路径、来源 ID、当前可见条目数、`next_cursor`。 |
| `recall_search` | `query?`, `source_ids?`, `platform?`, `collection_id?`, `saved_after?`, `saved_before?`, `limit?`, `cursor?` | 合并后的条目列表、`next_cursor`、本次实际查询到的来源范围 `coverage`。无 `query` 即浏览，允许只按夹或渠道筛选。 |
| `recall_get_item` | `item_id`, `limit?`, `cursor?` | 条目详情、可见来源证据页、`next_cursor`。能查看同一内容从哪些地方收藏过。 |

- 查询过滤取交集；`source_ids` 内取并集。`platform` 指内容渠道（`github`、`zhihu`、`bilibili`、`xiaohongshu`、`douyin`、`web`），`source_ids` 指导入入口（如 `chrome-personal`）。时间区间 `[saved_after, saved_before)`；时间未知的条目不计入指定区间。`limit` 默认 20、最大 50。查询最长 200 字符、最多 12 个词。未知或未开放的显式来源、收藏夹、条目返回同一种 `NOT_FOUND`，避免透露隐藏对象是否存在。
- 搜索用 Unicode NFKC + `casefold()` 做匹配归一，中文和日文用字面子串匹配；空白分隔的词逐个要求命中，词可以命中同一收藏关系的标题、路径、摘要、URL 或备注。用参数化 SQL，不让输入成为 SQL 或 FTS 表达式；`%`、`_` 都按普通字符匹配。「日本 电商」是 AND 查询，「日本」这样的两字查询也能找到。中文问句由上层 Agent 拆关键词，CLI 不自动猜语义。
- 排序固定为：标题包含完整查询优先，其次所有查询词命中标题，其次其他命中；各档以 `COALESCE(saved_at, imported_at)` 倒序、`item_id` 升序打破平局。每个 `item_id` 选一条最优的可见证据作为展示。搜索只展示匹配的可见证据（最多 3 条），详情返回全部可见证据，支持分页。
- `cursor` 是不透明字符串，绑定工具、过滤器、开放范围和目录版本。稳定快照下无重复无漏项；目录版本变化返回 `STALE_CURSOR`，客户端从第一页开始。cursor 不是授权凭证，每次调用重新执行可见性检查。空列表是成功结果。
- `coverage` 区分 `live_profile`（浏览器已同步资料档）、`complete_export`、`imported_subset`、`authorized_view`、`authorized_subset` 和 `none`。每个查询来源带 §5.7 的刷新摘要及 `latest_attempt`（`never` / `succeeded` / `partial` / `failed`）。失败保留先前的覆盖状态。空结果也附上查询过的来源。Agent 据此说明使用的是哪个时间点的本地副本。

示例：`recall_search({"query":"日本 电商","limit":5})`。以下是目标响应，字段和 ID 均为示意：

```json
{
  "items": [
    {
      "item_id": "itm_1",
      "platform": "web",
      "title": "日本电商消费者调研",
      "url": "https://example.com/jp-market",
      "excerpt": null,
      "saved_at": null,
      "sources": [
        {
          "source_id": "chrome-personal",
          "collection_id": "col_1",
          "path": ["工作", "市场研究"],
          "capture": "file_import",
          "url": "https://example.com/jp-market"
        }
      ]
    }
  ],
  "next_cursor": null,
  "coverage": [
    {
      "source_id": "chrome-personal",
      "coverage": "imported_subset",
      "last_success_at": "2026-11-08T09:00:00Z",
      "latest_attempt": "succeeded"
    }
  ]
}
```

**错误形状：** `{"error":{"code":"STALE_CURSOR","message":"目录已更新，请从第一页重新查询。","retryable":true}}`。公共查询错误为 `INVALID_ARGUMENT`、`NOT_FOUND`、`STALE_CURSOR`、`DB_NOT_READY`、`DB_BUSY`、`INTERNAL_ERROR`。同步另外区分 `AUTH_REQUIRED`、`PERMISSION_DENIED`、`ACCOUNT_MISMATCH`、`RATE_LIMITED`、`NETWORK_ERROR`、`SOURCE_FORMAT_CHANGED`、`SNAPSHOT_INCOMPLETE`。Python 用同一错误码的异常；CLI 失败非零退出，JSON 模式返回同一错误体；MCP 的工具执行错误使用 `isError`，正常结果返回 `structuredContent`（兼容需要文本结果的客户端），协议层无效请求交由 SDK 处理。参照 [MCP 工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) 与 [SDK 错误约定](https://py.sdk.modelcontextprotocol.io/migration/)。

## 7. 预期使用方式

以下命令和代码是开发目标。在 Windows 用 `py -3.11 -m venv .venv` 建环境，激活后用 `python -m pip install -e ".[mcp]"` 安装开发版。只用 CLI 或 Python 可省略 `[mcp]`。`import`、`add` 第一次成功时创建本地来源，远端来源用 `source add` 登记。相同 ID 重用时须匹配来源类型与渠道。

```powershell
recall init
recall import bookmarks .\chrome-bookmarks.html --source chrome-personal --browser chrome
recall import bookmarks .\edge-favorites.html --source edge-work --browser edge
recall import csv .\bilibili-favorites.csv --source bili-manual --platform bilibili
recall import csv .\zhihu-favorites.csv --source zhihu-manual --platform zhihu
recall add https://example.com/jp-market --source web-manual --platform web --title "日本电商调研"
recall source add github-personal --kind github_stars
# 只在本地同步进程的环境中提供 RECALL_GITHUB_TOKEN。
recall sync github-personal
recall source resume github-personal
recall source add bili-personal --kind bilibili_favorites
recall source connect bili-personal
recall source resume bili-personal
recall watch
# watch 在前台持续运行，以下命令在另一个终端执行。
recall sources --json
recall collections --source chrome-personal --json
recall search "日本 电商" --limit 5 --json
recall get itm_1 --json
```

CLI 的查询参数对应工具契约：`--source` 可重复、`--platform`、`--collection`、`--saved-after`、`--saved-before`、`--limit`、`--cursor`。Python 公共入口提供相同四个函数；以下用普通循环，不依赖任何 Agent 框架：

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

支持 MCP stdio 的宿主使用以下启动参数；具体外层配置键按宿主要求填写。用虚拟环境解释器的绝对路径，避免启动到另一份 Python：

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

Agent 推荐调用顺序：先列来源确认覆盖，必要时列收藏夹；将自然语言拆成 1–3 次关键词查询；对最相关的条目取详情；回答附链接和收藏夹，明确哪些平台没接入或数据过期。标题、备注、网页片段都只是供引用的输入，不能作为 Agent 指令。只有元数据时，回答说「找到这些收藏」，不能推断文章内容或用标题代替证据。

## 8. 权限、隐私与本地维护

| 情况 | 行为 |
| --- | --- |
| MCP 暴露范围 | `RECALL_ALLOWED_SOURCES` 必须由用户明确配置；缺失时启动报配置错误。默认不暴露 `restricted`，用户需要时为可信的宿主显式设置 `RECALL_INCLUDE_RESTRICTED=1`。Agent 工具参数不能扩大范围。Python 调用方可传同样的开放范围；CLI 面向操作本机的用户，可显式查看受限条目。 |
| 账号或平台访问 | 平台授权只给本地同步进程。受信连接器使用系统凭据库或独立浏览器资料档。账户、资料档和来源 ID 隔离。只访问连接器配置的站点及核实过的媒体地址。分页和重定向逐跳核对。第三方 MCP 或下载器只作为实现参考。 |
| 输入与输出 | 不执行 HTML、备注或平台内容。输出纯文本/结构化 JSON，链接可回跳但工具不自动打开。stdout 在 MCP 模式仅用于协议；日志写 stderr，记录来源 ID、数量和错误码，不记录收藏内容、完整 URL 或响应体。 |
| 本地文件 | 采用当前系统用户的数据目录；不把数据库、备份、导出的个人文件或真实账号样例提交到仓库。SQLite 文件和备份依赖本机权限与磁盘保护。 |
| 删除与备份 | `recall source remove <id> --yes` 事务删除本地来源、收藏关系和对应记录，保留仍被其他来源引用的内容；不会触碰平台账号。`recall backup <path>` 用 SQLite 备份接口生成一致副本，目标已存在则拒绝覆盖。恢复说明要求停止读写、先验证备份版本与完整性，再恢复到指定路径。此前生成的备份仍可能含已删除内容。 |

## 9. 开发任务与验收顺序

每个任务交付可运行代码、最小必要测试和运行说明。顺序可按依赖推进；权限验证可以和本地链路并行。平台端真人验收使用用户自己的账号和授权，测试套件默认不连外网。

| 编号 | 交付 | 依赖 | 完成条件 |
| --- | --- | --- | --- |
| R01 | 项目骨架、SQLite v1、最小查询闭环 | 无 | Windows 新建环境可安装；`init` 幂等；样例库可查询；错误路径不会静默创建空库。 |
| R02 | Chrome / Edge HTML 导入 | R01 | 两份真实导出样例通过；中文多层目录、时间、实体和重复导入正确；格式坏或截断时替换不清空旧数据。 |
| R03 | 链接 / CSV 导入 | R01 | 从 B 站、小红书、抖音和知乎各收录至少一条自己选的真实收藏；可用一个 CSV 导入多个同来源条目；行错误和跳过统计可定位。 |
| R04 | 查询、来源覆盖和普通 loop | R02、R03 | 四个契约可用；中文两字、组合查询、筛选、多来源去重、排序和翻页通过；同一内容保留多条可见来源。 |
| R05 | MCP stdio | R04 | 官方 SDK 客户端启动子进程，`tools/list` 发现四个工具；成功结果和错误码与 Python 一致；未开放来源在列表、搜索、详情和分页里均不可见。 |
| R06 | GitHub 本人 Stars | R01、R04 | 模拟多页、身份变化、权限失败、限流和中途断线；真账号核对至少三条（若总量少于三条则核对全部）。同来源重跑不增重复；失败后仍可查旧副本。 |
| R07 | 知乎本人公开范围收藏接入 | 可与 R01 并行；实现依赖 R04 | 用本人 Access Secret 核对夹列表和多页夹内容；实测公开/私密范围、配额和返回字段，确认 `authorized_subset` 正确。不能取得预期权限或接口结果时记录具体阻塞与复核条件，保留手动接入。 |
| R08 | 数据维护与基础版验收 | R02–R07 | 本地来源删除、备份和恢复通过。干净 Windows 环境完成导入、CLI、MCP 与断网查询。 |
| R09 | 本地刷新进程 | R06、R08 | 用 GitHub 贯通轮询、增量、对账、租约、退避和延迟统计。自动与手动同步并发时只有一个任务提交。 |
| R10 | Chrome 和 Edge 实时扩展 | R02、R08 | 各浏览器验证新增、改名、移动、删除、跨资料档隔离、重启和离线重连。实测事件到可查延迟。 |
| R11 | B 站本人私密视频收藏 | R09 | 用本人私密夹多页样本证明可读。手机端新增后进入查询，取消收藏由对账处理。 |
| R12 | 知乎私密网页收藏 | R07、R09 | 验证专用登录资料档、私密夹发现或绑定、分页、权限状态和本地刷新。公开和私密覆盖分别报告。 |
| R13 | 小红书本人收藏及专辑 | R09、R12 的会话读取机制 | 测试手机新增图文和视频、仅自己可见、跨页以及专辑成员。对无法枚举的范围出具状态。 |
| R14 | 抖音本人收藏视频 | R09、R12 的会话读取机制 | 验证手机收藏、网页可见、自动落库与会话失效。合集用单独样本决定可支持范围。 |
| R15 | 本地 OCR 与 ASR | R04、R08 | 复用既有权重，跑通本地图、8 kHz 音频重采样、长音频分段和授权视频。提取结果可查并保留图片编号或时间段。 |
| R16 | 一期联调与发布 | R09–R15 | 逐个平台记录真账号的覆盖、延迟和限制。模拟电脑休眠、断网、令牌过期、平台改版。失败仍能查旧副本，私密数据默认只在本地可见。 |

关键回归用例：

1. 同一链接在 Chrome 两个文件夹和 Edge 一个文件夹出现。搜索仅显示一个内容项，详情有三条证据；过滤 Edge 时不出现 Chrome 路径。
2. 受限来源给公共链接加了不同标题。MCP 未开放受限来源时，受限标题不能影响命中、排序、计数或摘要；已知 ID 的详情请求也不能绕过权限。
3. GitHub 第 2 页失败，或原本有条目却返回异常空结果。旧收藏数与内容保留，状态区分上次成功与本次失败；恢复后可按新的完整快照更新。
4. 用 `日本`、`日本 电商`、`C++`、`%` 搜索可预期的数据；未知收藏时间不会进入有时间过滤的结果；查询到一半发生更新，旧游标返回 `STALE_CURSOR`。
5. 损坏的书签、含脚本链接或敏感参数的 CSV、重复源文件、损坏备份分别有可解释结果；日志不含用来检测泄露的测试秘密。
6. 对每个自动连接器记录平台可见时间、SQLite 提交时间和工具首次查到的时间。先用固定时钟与多轮模拟验证调度，再在本人账号下进行新增、取消、跨页、断线恢复验证。p95 报告同时注明样本数、实际间隔和配额限制。
7. 私密收藏中的一张图 OCR 出「供应链」，公开来源收藏了同一链接。未开放私密来源时，关键词命中、结果片段、计数和详情均不包含那张图的内容。

**一期质量门槛：** 在首验 Windows 机器用本地 SSD、20,000 条合成收藏，分别采样常用中文查询、跨来源查询和空查询，记录硬件与数据规模。热库查询目标 p95 ≤ 300 ms（不含 MCP 进程启动）；超出时先定位查询计划和排序，再考虑搜索索引。基线先用 SQLite 字面搜索；只有实测需要时引入 [FTS5](https://www.sqlite.org/fts5.html)，并保留一字、两字中文搜索的可靠路径。完成条件以实测和回归结果为准。

## 10. 后续阶段

| 阶段 | 核心交付 | 先验收什么 |
| --- | --- | --- |
| 二期：Understand | 用户触发的正文补全、可访问网页正文提取；先用标题和摘要建立评测集，再在收益可测时加入语义排序。内容片段带 `item_id`、原链接、抓取时间与段落定位。 | 用自己历史收藏写至少 30 个「记不清标题」问题，对比一期关键词搜索与语义检索的 Recall@5；报告未找到及证据不足，不把公开网页补全混成个人收藏。 |
| 三期：Activate | 新增一个目标入口：给定目标、期限和约束，生成「已有证据、未知问题、下一步」简报。例如从收藏里的日本消费市场报告、竞品和营销案例中生成调研框架。 | 用一条完整场景验收：每个事实能回到来源，每个建议标出依据或假设；缺资料时列补充调研动作。产物先可导出 Markdown；生成动作清单不等于自动执行。 |
| 四期：Share | 从个人库选条目生成可审阅专题包：分享用户选中的链接、自写注释和经审查的摘要；支持导出和导入。 | 预览内容和权限，私密收藏夹、敏感字段和受限链接默认排除；收件人可以按来源重新利用，并知道哪些是作者注释。需要多人在线协作时再引入账号与服务端。 |

补全与检索沿用 `item_id` 与来源证据。摘要区分用户备注、原文片段、OCR 或转写结果以及模型提出的建议。

## 11. 本地 OCR 和语音转文字

### 11.1 可复用资产和本机验证

| 能力 | 已有资产 | 本次检查 |
| --- | --- | --- |
| 图片 OCR | `wechat` 使用 `rapidocr-onnxruntime==1.4.4`。当前 Anaconda 安装内已有 `ch_PP-OCRv4_det_infer.onnx`、`ch_PP-OCRv4_rec_infer.onnx` 和 `ch_ppocr_mobile_v2.0_cls_infer.onnx`。 | 用 `RapidOCR()` 识别本机生成的混合中英文图片，正确识别「收藏测试」，英文 `AI` 有一处误识别。 |
| 中文 ASR | `C:\Users\14815\Projects\wechat\models\para-zh\model.int8.onnx`（81,828,675 字节）及同目录 `tokens.txt`。`sherpa-onnx==2.0.1`，使用 `OfflineRecognizer.from_paraformer`。 | 用该目录自带的 16 kHz 单声道 `test_wavs\0.wav` 实际解码，得到中文转写。 |
| 音频解码和重采样 | 当前 Python 环境有 PyAV 18.1.0 及其 libav，测试使用 `AudioResampler`。 | 用 `test_wavs\8k.wav` 验证 8 kHz 转为 16 kHz 单声道，再由 Paraformer 成功解码。 |

引擎代码分别参考 `wechat\backend\scripts\run_ocr.py`、`wechat\backend\scripts\run_asr.py` 和 `wechat\backend\app\auto_media.py`。OCR 权重随 Python 安装包提供，ASR 权重保存在 `wechat\models\para-zh`。Recall 提供可选 `media` 安装项和 `RECALL_ASR_MODEL_DIR` 配置，运行时只读模型文件。提取模块使用 Recall 自己的输入和数据库。解码后先重采样，再调用识别器。

```powershell
python -m pip install -e ".[media]"
$env:RECALL_ASR_MODEL_DIR = 'C:\Users\14815\Projects\wechat\models\para-zh'
recall media index itm_1 .\sample.wav
```

### 11.2 输入、提取与检索

1. 先跑通 `recall media index <item_id> <local_file>`。文件由用户提供，并与指定可见收藏关系绑定。同一内容存在多条关系时由调用者选择证据来源，避免把私密附件挂到公开关系。
2. 按来源显式启用媒体提取后，允许把连接器正常可读的笔记图片、平台字幕和音视频排入 SQLite 待办。收藏元数据先提交，媒体用独立 worker 处理。提取仅使用用户有权访问且能正常获得的内容。媒体失效或无法取得时保存 `unavailable`，同时保留收藏链接和元数据。
3. OCR 对笔记图片按图片编号保留文本块、坐标和置信度。视频画面 OCR 后续可按间隔抽帧启用，保留帧时间并去掉连续重复字幕。ASR 优先保留平台已有可读字幕作为独立证据，其余音频用 PyAV 解码为 16 kHz 单声道 float PCM，长音频按时间窗分段并处理重叠边界。起止时间取自原音轨窗口，字级时间戳仅在模型实际提供时展示。
4. 提取任务按 `entry_id`、媒体字节摘要、提取类型、引擎版本和参数版本去重。结果保存 `entry_id`、`kind`（`ocr` / `asr` / `caption`）、文字、图片编号或音轨时间段以及可见范围。任务状态区分 `pending`、`ready`、`empty`、`unavailable` 和 `failed`。失败可重试，模型版本改变可重新生成。来源移除时清理对应派生文本和待办。
5. 搜索把片段作为可见条目的补充字段。详情只返回有限的匹配片段和原收藏链接，标注「图片识别」或「音频转写」。加载模型和处理音视频在查询进程之外，避免影响书签刷新。首验用一张中英混合图、一段非 16 kHz 音频和一条多分段视频，记录耗时、内存、样本识别准确率和片段定位。
