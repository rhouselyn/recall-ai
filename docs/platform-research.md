# 收藏数据接入调研

核查日期为 2026-11-10。本文以官方开发文档、产品帮助和公开源码为依据。使用本人账号的授权读取、单次文件导入和手动录入分别标记来源与覆盖范围。本次核查未使用用户凭证，真实账号返回、浏览器导出样本和平台数据副本格式需在对应阶段验证。

本文保留官方接口与手动导入的基础路线。持续刷新和第三方私密收藏接入以新版开发规格为准。 [开发规格](../RECALL_AI_DEVELOPMENT.md) · [第三方接口与 RSS 源码调研](private-favorites-research.md)

## 路线一览

| 来源 | 已确认的官方能力 | 项目接入决策 |
| --- | --- | --- |
| Chrome | 书签管理器导出 HTML，桌面扩展可读取书签树。 [Chrome 帮助](https://support.google.com/chrome/answer/96816) · [扩展 API](https://developer.chrome.com/docs/extensions/reference/api/bookmarks) | 首期由用户导出并导入 HTML。浏览器内一键读取作为独立扩展能力。 |
| Edge | 收藏夹导出 HTML，桌面扩展支持 Chromium `bookmarks` API。 [Microsoft Learn 导出步骤](https://learn.microsoft.com/en-us/deployedge/edge-learnmore-reset-data-in-cloud#back-up-your-favorites) · [扩展 API 列表](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/api-support) | 与 Chrome 共用 HTML 导入器，导入时区分浏览器和资料档。 |
| GitHub | 已授权用户可列出自己 Star 的仓库。 [Starring REST API](https://docs.github.com/en/rest/activity/starring#list-repositories-starred-by-the-authenticated-user) | 首个 API 适配器，覆盖令牌可见的仓库。 |
| 知乎 | Access Secret 可查询本人公开范围内的收藏夹与收藏，支持查看指定收藏夹内容。 [收藏夹列表](https://developer.zhihu.com/docs?key=user_favlists) · [收藏夹内容](https://developer.zhihu.com/docs?key=favlist_contents) | 第二个 API 适配器，标注授权子集。通过手动导入补充私密或未返回的条目。 |
| 哔哩哔哩 | 开放平台提供账号授权与创作者数据。App 有个人信息下载入口，收藏夹成员与文件格式待验证。 [开放平台文档](https://open.bilibili.com/doc/) · [隐私政策](https://www.bilibili.com/blackboard/privacy-pc.html) | 首期接收用户主动选择的链接。获取脱敏副本样本后再决定是否增加格式适配。 |
| 抖音 | 有 OAuth 与应用权限目录。App 可申请个人信息下载，收藏覆盖和文件格式待验证。 [开放平台权限表](https://open.douyin.com/platform/resource/docs/accession-guide/type-and-permission) · [隐私政策](https://www.douyin.com/draft/douyin_agreement/douyin_agreement_privacy.html?id=6773901168964798477) | 首期接收用户主动选择的视频链接。个人信息文件需样本验证。 |
| 小红书 | 账号开放平台目前只开放 `basic_info`。App 的个人页可查看收藏。 [Scope 文档](https://openaccount.xiaohongshu.com/docs/scope) · [隐私政策](https://agree.xiaohongshu.com/h5/terms/ZXXY20230227002/-1) | 首期接收用户主动选择的笔记链接。收藏批量导出和授权读取待官方证据。 |

## Chrome 与 Edge

- Chrome 桌面端从书签管理器的更多菜单导出书签 HTML。Edge 桌面端按 `Ctrl+Shift+O`，在更多选项中导出收藏夹 HTML。导入作用域是用户提供的文件和选定的来源实例，重复导入沿用该实例。 [Chrome 帮助](https://support.google.com/chrome/answer/96816) · [Edge 导出步骤](https://learn.microsoft.com/en-us/deployedge/edge-learnmore-reset-data-in-cloud#back-up-your-favorites)
- Chromium 的 HTML 写入器使用 Netscape Bookmark 文件头，用 `H3` 与嵌套 `DL` 表达文件夹，用链接 `A` 的 `HREF` 表达地址。`ADD_DATE` 为 Unix 秒，`ICON` 可包含图标数据。导入器应把 HTML 当数据解析，提取标题、链接、文件夹层级与可选时间，忽略图标和可执行内容。至少用 Chrome 和 Edge 各一份真实导出样本核对嵌套、非 ASCII 文本、时间、空文件夹和重复链接。 [Chromium HTML 导出源码](https://chromium.googlesource.com/chromium/src.git/%2B/refs/heads/main/chrome/browser/bookmarks/bookmark_html_writer.cc)
- 后续扩展可在用户触发时调用 `chrome.bookmarks.getTree()` 读取当前资料档。`bookmarks` 权限的浏览器提示覆盖读写，扩展代码应只调用读取方法。树节点 `id` 在当前资料档内唯一，`dateAdded` 可缺省且为 Unix 毫秒。同名顶层目录可能同时对应本地与账号数据，资料档隔离与文件夹路径应保留，支持时可使用 `folderType` 和 `syncing` 区分。 [书签 API](https://developer.chrome.com/docs/extensions/reference/api/bookmarks) · [Chrome 权限提示](https://developer.chrome.com/docs/extensions/reference/permissions-list) · [Chrome 书签同步变更](https://developer.chrome.com/blog/bookmarks-sync-changes) · [Edge API 兼容性](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/api-support)

## GitHub 仓库 Stars

- 调用 `GET https://api.github.com/user/starred`。官方列出细粒度个人访问令牌和 GitHub App 用户令牌，所需账号权限为 `Starring: read`。个人本地版可先使用用户自行创建的细粒度 PAT，按资源所有者和仓库访问范围控制覆盖。组织可能要求审批或限制 PAT，私有仓库覆盖应以实际可见范围验证。 [Starring REST API](https://docs.github.com/en/rest/activity/starring#list-repositories-starred-by-the-authenticated-user) · [PAT 管理](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) · [组织 PAT 策略](https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/setting-a-personal-access-token-policy-for-your-organization)
- 使用 `Accept: application/vnd.github.star+json` 获取 `starred_at` 与 `repo`，以仓库 `id` 作为稳定源标识，以 `html_url` 作为阅读链接。显式指定受支持的 API 版本，例如官方示例的 `2026-03-10`。按需设 `per_page=100`，沿响应 `Link` 的 `rel="next"` 请求下一页。发布前用授权测试账号验证实际媒体类型、改名、空列表与多页响应。 [Starring REST API](https://docs.github.com/en/rest/activity/starring#list-repositories-starred-by-the-authenticated-user) · [API 版本](https://docs.github.com/en/rest/about-the-rest-api/api-versions) · [REST 分页](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)
- `401` 按需要重新授权处理。`403` 需区分权限与限流，`403`/`429` 遇限流应按 `Retry-After` 或限额重置时间退避，保留上一批成功数据。GitHub 2026 年针对仓库 stargazers 和 watchers 的访问限制公告所列端点与 `/user/starred` 分开。GitHub 产品中还有 Topics 和 Lists，本适配器的覆盖范围是该端点返回的仓库。 [GitHub 限流说明](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) · [访问限制公告](https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/) · [Stars 产品帮助](https://docs.github.com/en/get-started/exploring-projects-on-github/saving-repositories-with-stars)

## 知乎本人收藏

官方文档中心的“知乎用户数据”提供三个读取接口。正文可从各页面打开，也可在文档站公开提供的文档数据中按 `key` 查阅。下文依据公开接口约定，尚待使用本人 Access Secret 联调。 [知乎数据开放平台](https://developer.zhihu.com/) · [文档站公开文档数据](https://developer.zhihu.com/console/api/v3/docs)

| 请求 | 参数和响应 | 覆盖限制 |
| --- | --- | --- |
| `GET /api/v1/user/favlists` | `Limit` 默认 `20`。读取 `Data.Items[].UrlToken`、`Url`、`Title`、`Description`、`IsPublic`。 | 文档将范围限定为公开收藏夹，列出的请求参数只有 `Limit`。达到请求上限时保留范围提示。 [收藏夹列表](https://developer.zhihu.com/docs?key=user_favlists) |
| `GET /api/v1/user/favlist_contents` | 将列表返回的 `UrlToken` 用作请求参数 `FavlistUrlToken`。使用 `Offset` 和 `Limit`，按 `Data.Paging.IsEnd` 与字符串 `NextOffset` 翻页。 | 文档承诺指定收藏夹的公开内容。游标缺失、重复或出错时，结束本次同步并保留旧副本。 [收藏夹内容](https://developer.zhihu.com/docs?key=favlist_contents) |
| `GET /api/v1/user/collections` | `Limit` 默认 `20`。读取 `Data.Items` 的内容链接、标题、摘要、`FavTime` 及 `Favlists`。 | 近期窗口，适合按需读取与最小联调。 [近期收藏](https://developer.zhihu.com/docs?key=user_collections) |

- 单人版由用户在开放平台个人中心创建 Access Secret。请求发送 `Authorization: Bearer <access_secret>` 与秒级 Unix 时间戳 `X-Request-Timestamp`。第三方应用替其他已授权用户读取时需另申请 OAuth，增加 `X-OAuth-Token`，对应收藏权限为公开收藏夹。 [知乎鉴权](https://developer.zhihu.com/docs?key=authorization) · [知乎 OAuth 应用集成](https://developer.zhihu.com/docs?key=zhihu_oauth_integrated)
- 导入时用 `UrlToken` 表示收藏夹标识，用内容链接或可确认的稳定内容标识归并条目，保留同一条目与多个收藏夹的关系。`FavTime` 是收藏时间，`CreatedAt` 是内容创建时间。字段 `FavoriteCount` 只描述内容被收藏的计数。 [收藏夹列表](https://developer.zhihu.com/docs?key=user_favlists) · [收藏夹内容字段](https://developer.zhihu.com/docs?key=favlist_contents)
- 范围标为 `authorized_subset`，私密收藏夹和全账号枚举能力待官方证据。除 HTTP 状态外检查响应 `Code`。`20001` 为鉴权失败，`30001` 为频率限制，`30002` 为配额限制。用户数据服务有每日免费额度与后续按成功调用计费的规则，额度可变，联调和同步应限制页数并报告中断位置。 [收藏接口错误码](https://developer.zhihu.com/docs?key=favlist_contents) · [额度说明](https://developer.zhihu.com/docs?key=quota) · [服务价格](https://developer.zhihu.com/docs?key=billing_price)
- 官方 Zhihu CLI 支持本人收藏，使用 Access Secret，文档将个人数据访问限定为按需。知乎官方 MCP 的文档列出站内搜索、全网搜索、热榜和直答。项目的知乎适配器依据上述收藏 REST 接口实现。 [Zhihu CLI](https://developer.zhihu.com/docs?key=zhihu_cli) · [知乎 MCP](https://developer.zhihu.com/docs?key=unified_mcp)

## 哔哩哔哩、抖音与小红书

**哔哩哔哩。** 开放平台公开目录包括账号授权、用户信息和用户授权后的数据能力。开放平台隐私政策列出创作者稿件的收藏数，本次查阅的公开文档没有确认可列出用户自己收藏夹及其中条目的接口。App 的个人信息下载入口为「我的 → 设置 → 个人信息收集清单 → 个人信息下载」。可先导入用户从收藏页主动选取的链接，待用户提供脱敏样本后，验证个人信息副本是否含收藏夹成员、原始链接和收藏时间。 [开放平台文档](https://open.bilibili.com/doc/) · [开放平台隐私政策](https://open.bilibili.com/agreement/privacy-policy) · [哔哩哔哩隐私政策](https://www.bilibili.com/blackboard/privacy-pc.html?visit_id=1brx8eqpnf7k)

**抖音。** 公开文档说明 Web OAuth 授权，并列出应用可申请的权限。SDK 提到深度合作伙伴在用户主动授权时可申请包括收藏音乐列表在内的额外数据。本次未确认通用的本人收藏视频列表接口。App 可从「我 → ≡ → 设置 → 个人信息管理」申请下载，用户向抖音提供邮箱以接收下载链接。隐私政策记载平台收集收藏行为，副本是否含收藏视频清单、分组及可解析链接仍待样本核实。首期接收用户主动复制的收藏视频链接。 [OAuth 授权说明](https://open.douyin.com/platform/resource/docs/develop/permission/overall-permission/) · [开放平台权限表](https://open.douyin.com/platform/resource/docs/accession-guide/type-and-permission) · [SDK 能力](https://open.douyin.com/platform/resource/docs/develop/summarize/sdk) · [抖音隐私政策](https://www.douyin.com/draft/douyin_agreement/douyin_agreement_privacy.html?id=6773901168964798477)

**小红书。** 官方账号开放平台现阶段只开放 `basic_info`，用于头像、昵称等基本身份信息。API 参考提供登录授权、Token 管理和基本资料，收藏读取权限和接口仍待确认。官方隐私政策为大陆账号提供「我 → 笔记/收藏/赞过」访问入口。用户从收藏页选择笔记，再复制分享链接或手动填写标题与链接。批量收藏副本的稳定格式仍待核实。 [授权范围](https://openaccount.xiaohongshu.com/docs/scope) · [API 参考](https://openaccount.xiaohongshu.com/docs/api-reference) · [小红书用户隐私政策](https://agree.xiaohongshu.com/h5/terms/ZXXY20230227002/-1)

上述手动来源共用一个导入入口，接收用户明确提交的链接、标题与可选分组。输入接受 `http` 和 `https`，安全回跳链接和去重身份分开，处理来源站点已知的追踪字段前先验证打开行为。手动输入与数据副本均按所交付批次计入覆盖范围，缺失条目不触发删除。若未来启用新的授权源，验收证据应包括官方权限与端点、本人账号的小样本、翻页规则及已知缺失范围。

## 本地 MCP 查询层

项目 MCP 可通过 `stdio` 启动，工具只查询本地索引。工具输入以 JSON Schema 约束，可标注 `readOnlyHint: true`，只读由服务端入口和存储访问权限落实。标准输出用于 MCP 消息，诊断走标准错误。选型时用目标客户端验证协议兼容性和调用结果。 [MCP 传输规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports) · [MCP 工具规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) · [MCP 工具注解说明](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)
