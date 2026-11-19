const platforms = {
  chrome: { name: "Google Chrome", mark: "chrome", data: "浏览器书签", browser: true },
  github: { name: "GitHub", mark: "github", data: "本人可访问的 Star 仓库" },
  bilibili: { name: "哔哩哔哩", mark: "bilibili", data: "本人视频收藏夹" },
  youtube: { name: "YouTube", mark: "youtube", data: "播放列表、稍后观看和点赞视频" },
  xiaohongshu: { name: "小红书", mark: "书", data: "本人收藏的笔记和专辑" },
  douyin: { name: "抖音", mark: "douyin", data: "本人收藏的视频（分组待验证）" },
  zhihu: { name: "知乎", mark: "知", data: "本人私密收藏夹" },
  edge: { name: "Microsoft Edge", mark: "e", data: "浏览器收藏夹", browser: true },
};
const conversations = [];
let activeChat = null;
let currentView = "chat";
let mcpServer;
let modelSettings;
let sources = [];
let items = [];
let nextCursor = null;
let sourcesInitialized = false;
let libraryInitialized = false;
let libraryRequest = 0;
let librarySearchTimer;
let sessionToken = "";
let archiveEnabled = false;
let extensionDir = "";
let toastTimer;
const byId = (id) => document.getElementById(id);
const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const question = byId("question");
const scope = byId("scope");
const messages = byId("messages");
const dialog = byId("detail-dialog");
const dialogContent = byId("dialog-content");

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Recall-Token": sessionToken },
    body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || "请求失败，请重试。");
    error.code = data.error?.code;
    throw error;
  }
  return data;
}

function platformIcon(id) {
  const platform = platforms[id] || { mark: "bookmark" };
  const mark = platform.mark;
  return `<span class="platform ${escapeHtml(id)}" aria-hidden="true">${mark.length > 1 ? icon(mark) : mark}</span>`;
}

function sourceFor(id) {
  return sources.find(source => source.source_id === `${id}-local` && source.sync_enabled) ||
    sources.find(source => source.provider === id && source.kind === "bookmarks_live") ||
    sources.find(source => source.provider === id && source.kind !== "manual") ||
    sources.find(source => source.provider === id);
}

function statusFor(source) {
  if (!source) return "未连接";
  if (source.sync_state === "needs_login") return "需要登录";
  if (source.sync_state === "paused") return "已暂停";
  if (source.latest_attempt === "failed") return "同步失败";
  if (source.last_success_at && source.kind === "bookmarks_html") return source.sync_enabled ? "已启用扫描" : "已导入";
  if (source.last_success_at && source.latest_attempt === "partial") return "部分同步";
  if (source.last_success_at) return "已连接";
  return "待同步";
}

function connectionStatus(source) {
  const status = statusFor(source);
  return `<span class="row-status${status === "已连接" ? " connected" : ""}"><span class="status-dot"></span>${status}</span>`;
}

function timeLabel(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", {hour12: false});
}

function syncLabel(source, platform) {
  if (!source) return platform.browser ? "可直接扫描本机书签" : "尚未授权";
  if (source.sync_state === "paused") return "已暂停 · 不会自动检查";
  if (source.sync_state === "needs_login") return "保留旧收藏 · 需重新授权";
  if (source.sync_enabled && source.kind === "bookmarks_html") return "只读文件扫描 · 约每 15 秒";
  if (source.sync_enabled && source.refresh_mode === "poll") {
    return source.next_run_at ? `下次检查 ${timeLabel(source.next_run_at)}` : "后台轮询已启用";
  }
  if (source.refresh_mode === "event") return "浏览器扩展推送变更";
  if (source.refresh_mode === "poll") return "自动同步未启用";
  return "手动导入";
}

async function refreshSources() {
  const data = await api("/api/sources");
  if (sourcesInitialized && JSON.stringify(sources) === JSON.stringify(data.sources)) return;
  sources = data.sources;
  sourcesInitialized = true;
  renderAccounts();
  renderChatNote();
}

function renderAccounts() {
  byId("account-list").innerHTML = Object.entries(platforms).map(([id, platform]) => {
    const source = sourceFor(id);
    const directLogin = !source && !platform.browser && id !== "github" && id !== "zhihu";
    const action = directLogin ? "打开登录页" : source ? "管理来源" :
      platform.browser ? "连接本机书签" : id === "github" ? "接入指南" : "连接选项";
    return `<article class="account-card"><div class="account-card-head">${platformIcon(id)}
      <div><h2>${platform.name}</h2><p>${platform.data}</p></div></div>
      <div class="sync-meta">${connectionStatus(source)}<span>${escapeHtml(syncLabel(source, platform))}</span>
      ${source ? `<span>${source.visible_entry_count} 条 · 上次成功 ${escapeHtml(timeLabel(source.last_success_at))}</span>` : ""}</div>
      <button type="button" class="secondary" data-${directLogin ? "login" : "account"}="${id}">${action}<span class="sr-only"> ${platform.name}</span></button></article>`;
  }).join("");
}

function renderMcpConfig() {
  if (!mcpServer) return;
  const selected = Array.from(byId("mcp-sources").querySelectorAll("input:checked"), input => input.value);
  const env = {...mcpServer.env, RECALL_ALLOWED_SOURCES: selected.join(","),
    RECALL_INCLUDE_RESTRICTED: byId("mcp-private").checked ? "1" : "0"};
  byId("mcp-config").value = JSON.stringify({mcpServers: {recall: {...mcpServer, env}}}, null, 2);
  byId("copy-mcp").disabled = !selected.length;
}

async function showSettings() {
  const result = await api("/api/settings/mcp");
  const selected = new Set(Array.from(byId("mcp-sources").querySelectorAll("input:checked"), input => input.value));
  mcpServer = result.server;
  byId("mcp-sources").innerHTML = result.sources.map(id =>
    `<label><input type="checkbox" value="${escapeHtml(id)}"${selected.has(id) ? " checked" : ""}> ${escapeHtml(id)}</label>`).join("") || "暂无可接入的收藏来源。";
  renderMcpConfig();
}

function renderChatNote() {
  if (!modelSettings) return;
  const hints = [];
  if (sourcesInitialized && !sources.some(source => source.visible_entry_count > 0)) {
    hints.push("在「账号连接」同步账号，或到「收藏库」导入书签。");
  }
  const notes = { auto: "在「设置」配置问答模型。", search: "", ollama: "问答使用本机 Ollama。",
    cloud: modelSettings.allow_private ? "公开、私密收藏及近期对话可发往已配置的 API。" :
      "公开命中使用已配置的 API。私密命中尝试使用本机 Ollama。" };
  if (notes[modelSettings.mode]) hints.push(notes[modelSettings.mode]);
  const note = byId("chat-note");
  note.textContent = hints.join(" ");
  note.hidden = !hints.length;
}

function renderModelMode() {
  const mode = byId("model-mode").value;
  byId("model-cloud-fields").hidden = mode !== "cloud";
  byId("model-ollama-fields").hidden = mode !== "ollama";
  for (const id of ["model-url", "model-name", "model-key", "model-private"]) byId(id).disabled = mode !== "cloud";
  byId("test-model").hidden = mode === "search";
}

function useModelSettings(settings) {
  modelSettings = settings;
  byId("model-mode").value = settings.mode === "auto" ? "cloud" : settings.mode;
  byId("model-url").value = settings.base_url;
  byId("model-name").value = settings.mode === "ollama" ? byId("model-name").placeholder : settings.model;
  byId("model-local-name").value = settings.mode === "ollama" ? settings.model : "";
  byId("model-key").value = "";
  byId("model-key").placeholder = settings.has_key ? "已有密钥，留空继续使用" : "粘贴服务提供的密钥";
  byId("model-private").checked = settings.allow_private;
  byId("model-options").disabled = false;
  renderModelMode();
  renderChatNote();
}

async function submitModel(test) {
  if (!byId("model-form").reportValidity()) return;
  const mode = byId("model-mode").value;
  const data = { mode };
  if (mode === "ollama") data.model = byId("model-local-name").value;
  if (mode === "cloud") Object.assign(data, { model: byId("model-name").value, base_url: byId("model-url").value,
    api_key: byId("model-key").value, allow_private: byId("model-private").checked });
  const panel = byId("model-options");
  const status = byId("model-status");
  panel.disabled = true;
  status.textContent = test ? "正在发送测试消息…" : "正在保存…";
  status.className = "settings-status";
  try {
    const result = await api("/api/settings/model" + (test ? "/test" : ""), data);
    if (!test) useModelSettings(result);
    status.textContent = test ? "连接成功。保存配置后即可用于问答。" : "模型配置已保存。";
  } catch (error) {
    status.className = "settings-status dialog-error";
    status.textContent = error.message;
  } finally { panel.disabled = false; }
}

async function renderLibrary(append = false) {
  const request = ++libraryRequest;
  const params = new URLSearchParams();
  const query = byId("library-query").value.trim();
  const platform = byId("library-source").value;
  if (query) params.set("query", query);
  if (platform) params.set(platforms[platform]?.browser ? "browser" : "platform", platform);
  if (append && nextCursor) params.set("cursor", nextCursor);
  let result;
  try { result = await api("/api/items?" + params); }
  catch (error) {
    if (request !== libraryRequest) return;
    if (append && error.code === "STALE_CURSOR") return renderLibrary();
    throw error;
  }
  if (request !== libraryRequest) return;
  const updated = append ? items.concat(result.items) : result.items;
  if (libraryInitialized && JSON.stringify(updated) === JSON.stringify(items) && nextCursor === result.next_cursor) return;
  items = updated;
  nextCursor = result.next_cursor;
  libraryInitialized = true;
  byId("library-count").textContent = `已显示 ${items.length} 条收藏`;
  byId("library-list").innerHTML = items.map(entry => `
    <button type="button" class="library-row" data-record="${escapeHtml(entry.item_id)}">
      ${platformIcon(entry.platform)}<span><strong>${escapeHtml(entry.title)}</strong>
      <small>${escapeHtml((entry.sources || []).map(s => [s.source_id, ...s.path].join(" / ")).join("、"))}</small></span>
      ${icon("chevron")}</button>`).join("") || '<p class="empty-library">没有匹配的收藏。可以连接账号、导入收藏或添加链接。</p>';
  if (nextCursor) byId("library-list").insertAdjacentHTML("beforeend", '<button class="secondary" type="button" id="load-more">加载更多</button>');
}

function changeView(view) {
  currentView = view;
  for (const button of document.querySelectorAll(".nav-link")) {
    const active = button.dataset.view === view;
    byId(`view-${button.dataset.view}`).hidden = !active;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  renderHistory();
  if (view === "library") renderLibrary().catch(showError);
  if (view === "accounts") refreshSources().catch(showError);
  if (view === "settings") showSettings().catch(showError);
  window.scrollTo(0, 0);
}

function renderHistory() {
  byId("history-list").replaceChildren(...conversations.slice(0, 12).map(chat => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-entry";
    button.textContent = chat.title;
    if (chat === activeChat && currentView === "chat") button.setAttribute("aria-current", "true");
    button.onclick = () => { activeChat = chat; changeView("chat"); renderChat(); };
    return button;
  }));
}

function playbackLabel(segment) {
  const offset = segment.locator?.start_seconds;
  if (!Number.isFinite(offset)) return segment.label;
  const seconds = Math.floor(offset);
  const time = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  return `${segment.label} · ${segment.locator.video_part > 1 ? `P${segment.locator.video_part} ` : ""}${time}`;
}

function citationSegment(segment, url) {
  return `<a href="${escapeHtml(segment.seek_url || segment.url || url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(playbackLabel(segment))}</a><span>${escapeHtml(segment.text)}</span>`;
}

function renderChat() {
  const turns = activeChat?.turns ?? [];
  messages.hidden = turns.length === 0;
  byId("view-chat").classList.toggle("has-turns", turns.length > 0);
  byId("composer").setAttribute("aria-busy", String(Boolean(activeChat?.pending)));
  byId("composer").querySelector(".send").disabled = Boolean(activeChat?.pending);
  messages.replaceChildren(...turns.map(turn => {
    const article = document.createElement("article");
    article.className = `message ${turn.role}${turn === turns.at(-1) && activeChat.pending ? " pending" : ""}`;
    article.innerHTML = '<p class="message-label"></p><p class="message-body"></p>';
    article.querySelector(".message-label").textContent = turn.role === "user" ? "你" : "Recall AI";
    article.querySelector(".message-body").textContent = turn.text;
    if (turn.items?.length) {
      const refs = document.createElement("div");
      refs.className = "citations";
      refs.innerHTML = `<h3>来源 · ${turn.items.length}</h3>` + turn.items.map((item, i) =>
        `<details class="citation-card" ${i === 0 ? "open" : ""}><summary><b>[${i + 1}]</b> ${escapeHtml(item.title)}</summary>
          ${(item.snippets || []).slice(0, 3).map(segment => `<div class="citation-snippet">${citationSegment(segment, item.url)}</div>`).join("")}
          <button class="citation" type="button" data-record="${escapeHtml(item.item_id)}">查看收藏详情</button></details>`
      ).join("");
      article.append(refs);
    }
    return article;
  }));
  messages.scrollTop = messages.scrollHeight;
}

async function sendQuestion(value) {
  const text = value.trim();
  if (!text || activeChat?.pending) return;
  if (!activeChat) { activeChat = { title: text, turns: [] }; conversations.unshift(activeChat); }
  const chat = activeChat;
  chat.pending = true;
  const history = chat.turns.slice(-6).map(({ role, text: content }) => ({ role, text: content }));
  chat.turns.push({ role: "user", text });
  question.value = "";
  const pending = { role: "assistant", text: "正在查找收藏…" };
  chat.turns.push(pending);
  renderChat(); renderHistory();
  try {
    const reply = await api("/api/chat", { text, platform: scope.value, history });
    pending.text = reply.text;
    pending.items = reply.items;
  } catch (error) { pending.text = error.message; }
  finally { chat.pending = false; }
  if (chat === activeChat) renderChat();
}

function showToast(message) {
  const toast = byId("toast");
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4500);
}
function showError(error) { showToast(error.message || "操作失败，请重试。"); }
function openDialog(html) {
  dialogContent.innerHTML = html;
  if (!dialog.open) dialog.showModal();
}
function errorInDialog(error) {
  const target = dialogContent.querySelector(".dialog-error");
  if (target) target.textContent = error.message;
  else showError(error);
}

async function showRecord(id, previousEvidence = [], cursor = null) {
  const data = await api("/api/items/" + encodeURIComponent(id) +
    (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""));
  data.evidence = previousEvidence.concat(data.evidence);
  const item = data.item;
  const archiveLabels = {pending: "等待自动归档", processing: "正在自动归档", ready: "已归档",
    empty: "未提取到正文或字幕", partial: "仅归档部分内容", failed: "归档失败", unavailable: "内容不可归档"};
  const archiveErrors = {NO_CAPTIONS: "无可用字幕或转写", NETWORK_ERROR: "暂时无法读取", RATE_LIMITED: "平台限流",
    AUTH_REQUIRED: "需要重新连接账号", ACCOUNT_MISMATCH: "登录账号与来源不一致",
    PERMISSION_DENIED: "平台拒绝访问或链接不是公开地址", CONFIG_ERROR: "缺少归档或转写依赖",
    SOURCE_FORMAT_CHANGED: "未识别的响应格式", INVALID_ARGUMENT: "链接或文件不符合归档限制"};
  const retry = archiveEnabled && data.evidence.some(entry =>
    ["empty", "partial", "failed", "unavailable"].includes(entry.archive?.status));
  const evidence = data.evidence.map((entry, index) => `
    <div class="dialog-info"><strong>${escapeHtml([entry.source_id, ...entry.path].join(" / "))}</strong>
      <br>${escapeHtml(!archiveEnabled && entry.archive?.status === "pending" ? "自动归档未启用" : archiveLabels[entry.archive?.status] || "等待自动归档")}${archiveErrors[entry.archive?.error_code] ? ` · ${archiveErrors[entry.archive.error_code]}` : ""}
      ${entry.segments?.length ? `<div class="archive-action"><button class="secondary" type="button" data-read-entry="${index}">查看已提取内容</button></div>` : ""}</div>`).join("");
  openDialog(`${platformIcon(item.platform)}<h2 id="dialog-title">${escapeHtml(item.title)}</h2>
    <p class="dialog-description">${escapeHtml(item.excerpt || "入库后会自动尝试提取正文或字幕，无需手动操作。")}</p>${evidence}
    <p class="dialog-error" role="alert"></p>
    <div class="dialog-actions"><a class="secondary" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">打开原文</a>
    ${data.next_cursor ? '<button class="secondary" type="button" id="more-evidence">加载更多来源</button>' : ""}
    ${retry ? '<button class="secondary" type="button" id="retry-archive">重试归档</button>' : ""}
    <button class="secondary" type="button" id="refresh-archive">刷新状态</button></div>`);
  for (const button of dialogContent.querySelectorAll("[data-read-entry]")) {
    button.onclick = async () => {
      button.disabled = true;
      try { await showArchivedContent(item, data.evidence[Number(button.dataset.readEntry)], button); }
      catch (error) { if (dialog.open && button.isConnected) errorInDialog(error); }
      finally { button.disabled = false; }
    };
  }
  if (retry) byId("retry-archive").onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api("/api/archive/retry", { item_id: id });
      if (dialog.open && byId("retry-archive") === button) await showRecord(id);
      showToast("已加入归档队列");
    } catch (error) { errorInDialog(error); }
    finally { button.disabled = false; }
  };
  byId("refresh-archive").onclick = () => showRecord(id).catch(errorInDialog);
  if (data.next_cursor) byId("more-evidence").onclick = () =>
    showRecord(id, data.evidence, data.next_cursor).catch(errorInDialog);
}

async function showArchivedContent(item, entry, trigger) {
  const path = "/api/items/" + encodeURIComponent(item.item_id) + "/content";
  const params = new URLSearchParams({entry_id: entry.entry_id});
  const first = await api(path + "?" + params);
  if (!dialog.open || !trigger.isConnected) return;
  const render = segments => segments.map(segment =>
    `<article class="archive-segment citation-snippet">${citationSegment(segment, entry.url)}</article>`).join("");
  openDialog(`<h2 id="dialog-title">${escapeHtml(item.title)}</h2>
    <p class="dialog-description">已提取内容 · ${escapeHtml([entry.source_id, ...entry.path].join(" / "))}</p>
    <div id="archive-content">${render(first.segments) || '<p class="dialog-info">暂无已提取内容。</p>'}</div>
    <p class="dialog-error" role="alert"></p>
    <div class="dialog-actions"><a class="secondary" href="${escapeHtml(entry.url)}" target="_blank" rel="noopener noreferrer">打开原文</a>
      <button class="secondary" type="button" id="back-to-record">返回收藏</button>
      <button class="secondary" type="button" id="more-content" ${first.next_cursor ? "" : "hidden"}>加载更多内容</button></div>`);
  byId("back-to-record").onclick = () => showRecord(item.item_id).catch(errorInDialog);
  let cursor = first.next_cursor;
  const more = byId("more-content");
  more.onclick = async () => {
    more.disabled = true;
    try {
      params.set("cursor", cursor);
      const result = await api(path + "?" + params);
      if (!dialog.open || byId("more-content") !== more) return;
      byId("archive-content").insertAdjacentHTML("beforeend", render(result.segments));
      cursor = result.next_cursor;
      more.hidden = !cursor;
    } catch (error) {
      if (error.code === "STALE_CURSOR" && dialog.open && byId("more-content") === more) {
        await showArchivedContent(item, entry, more).catch(errorInDialog);
      } else if (dialog.open && byId("more-content") === more) errorInDialog(error);
    } finally { more.disabled = false; }
  };
}

function showImport() {
  openDialog(`<h2 id="dialog-title">导入收藏</h2>
    <p class="dialog-description">导入浏览器导出的书签 HTML，或 Recall 格式的 CSV。</p>
    <label class="dialog-field">文件类型<select id="import-kind"><option value="csv">CSV 清单</option><option value="chrome">Chrome 书签 HTML</option><option value="edge">Edge 收藏 HTML</option></select></label>
    <label class="dialog-field">选择文件<input type="file" id="import-input" accept=".csv,.html,.htm"></label>
    <p class="dialog-error" role="alert"></p><div class="dialog-actions"><button type="button" class="secondary" data-close>取消</button><button type="button" class="primary" id="do-import">导入</button></div>`);
  byId("do-import").onclick = async event => {
    const button = event.currentTarget;
    const file = byId("import-input").files[0];
    if (!file || file.size > 20 * 1024 * 1024) return errorInDialog(new Error("请选择不超过 20 MiB 的文件。"));
    button.disabled = true;
    try {
      const content = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result.split(",")[1]);
        reader.onerror = () => reject(new Error("文件读取失败。"));
        reader.readAsDataURL(file);
      });
      const result = await api("/api/import", { kind: byId("import-kind").value, content });
      dialog.close();
      await refreshSources(); await renderLibrary();
      showToast(`导入${result.status === "partial" ? "部分完成" : "完成"}，新增 ${result.counts.entries_added} 条收藏`);
    } catch (error) { errorInDialog(error); button.disabled = false; }
  };
}

function showAdd() {
  openDialog(`<h2 id="dialog-title">添加链接</h2><p class="dialog-description">将一条链接保存到本地收藏库。</p>
    <label class="dialog-field">链接<input id="link-url" type="url" placeholder="https://"></label>
    <label class="dialog-field">标题（可选）<input id="link-title" maxlength="300"></label>
    <label class="dialog-field">收藏夹（可选）<input id="link-folder" maxlength="100"></label>
    <p class="dialog-error" role="alert"></p><div class="dialog-actions"><button type="button" class="secondary" data-close>取消</button><button type="button" class="primary" id="do-add">保存</button></div>`);
  byId("do-add").onclick = async () => {
    try {
      await api("/api/add", { url: byId("link-url").value, title: byId("link-title").value, collection: byId("link-folder").value });
      dialog.close(); await refreshSources(); await renderLibrary(); showToast("已保存到收藏库");
    } catch (error) { errorInDialog(error); }
  };
}

async function waitJob(jobId) {
  while (true) {
    await new Promise(resolve => setTimeout(resolve, 1500));
    const job = await api("/api/jobs/" + jobId);
    if (job.status === "failed") throw new Error(job.error?.message || "同步失败。");
    if (job.status === "succeeded") return job.result;
  }
}

async function openLoginPages(button, platform) {
  const progress = byId("login-progress");
  progress.hidden = false;
  progress.textContent = "正在启动登录页…";
  button.disabled = true;
  try {
    const { jobs, errors } = platform
      ? { jobs: { [platform]: (await api("/api/connect", { platform })).job_id } }
      : await api("/api/connect/pending", {});
    const states = { ...errors, ...Object.fromEntries(Object.keys(jobs).map(id => [id, "等待登录"])) };
    const render = () => {
      progress.textContent = Object.keys(states).length
        ? "请在各平台官方页面依次登录，完成一个就关闭该窗口，系统会自动同步。\n" +
          Object.entries(states).map(([id, state]) => `${platforms[id].name}：${state}`).join("\n")
        : "没有需要打开登录页的平台。GitHub 和浏览器书签请使用各自的入口。";
    };
    render();
    await Promise.all(Object.entries(jobs).map(async ([id, job]) => {
      try {
        const result = await waitJob(job);
        states[id] = result.status === "partial" ? "部分同步" : "同步完成";
      } catch (error) { states[id] = error.message; }
      render();
    }));
    await refreshSources();
  } catch (error) { progress.textContent = error.message; }
  finally { button.disabled = false; }
}

function showAccount(id) {
  const platform = platforms[id];
  const source = sourceFor(id);
  const status = statusFor(source);
  const errorNames = { AUTH_REQUIRED: "需要重新登录", PERMISSION_DENIED: "平台拒绝访问", RATE_LIMITED: "平台限流", SOURCE_FORMAT_CHANGED: "平台页面格式发生变化", NETWORK_ERROR: "网络连接失败", ACCOUNT_MISMATCH: "登录账号不匹配", SNAPSHOT_INCOMPLETE: "本次同步不完整" };
  const state = source ? `<p class="dialog-info">状态：${status} · ${source.visible_entry_count} 条收藏<br>上次成功：${escapeHtml(source.last_success_at || "尚未同步")}${source.latest_attempt === "failed" ? `<br>最近错误：${escapeHtml(errorNames[source.last_error_type] || "同步失败，请重试")}` : ""}${source.coverage === "authorized_subset" ? "<br>当前只覆盖已读取到的部分收藏。" : ""}</p>` : "";
  const browserInfo = platform.browser
    ? '<p class="dialog-info">不用装扩展。Recall 运行时约每 15 秒扫描本机书签文件，只读取书签，不读取 Cookie 或密码。新增会入库；为保留历史，删除或移动不会清掉旧记录。</p>'
    : id === "github" ? `<ol class="dialog-steps">
        <li>登录 GitHub，<a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener noreferrer">新建细粒度个人访问令牌</a>。</li>
        <li>填写名称、设置到期时间并选择资源所有者。仓库访问选公开仓库即可；如需私有 Star，另外选择令牌有权读取的目标仓库。</li>
        <li>在账号权限中给 <strong>Starring</strong> 设置 <strong>Read-only</strong>，仓库 <strong>Metadata</strong> 保持只读。不要开启写权限。</li>
        <li>点击 Generate token，复制令牌并粘贴到下方。Windows 上同步成功后，令牌会加密保存在本机。</li>
      </ol><label class="dialog-field">GitHub 只读令牌<input id="account-token" type="password" autocomplete="off"></label>`
    : `<p class="dialog-info">在平台网页扫码或登录，完成后关闭该窗口，系统会自动尝试同步。</p>
      ${id === "zhihu" ? '<label class="dialog-field">指定知乎收藏夹（可选）<input id="account-collections" autocomplete="off" placeholder="填写 /collection/ 后的数字，多个用逗号分隔"></label>' : ""}`;
  openDialog(`${platformIcon(id)}<h2 id="dialog-title">${platform.name}</h2><p class="dialog-description">读取范围 · ${platform.data}</p>${state}${browserInfo}
    <p class="dialog-error" role="alert"></p><div class="dialog-actions">
    ${source && (!platform.browser || (source.source_id === `${id}-local` && source.sync_enabled)) ? `<button class="secondary" type="button" id="pause-account">${source.sync_state === "paused" ? "恢复同步" : "暂停同步"}</button>` : ""}
    ${source && !platform.browser ? '<button class="secondary" type="button" id="sync-account">立即同步</button>' : ""}
    ${platform.browser ? '<button class="secondary" type="button" id="setup-bookmarks">扩展推送（可选）</button>' : ""}
    <button class="primary" type="button" id="connect-account">${platform.browser ? (source?.source_id === `${id}-local` && source.sync_enabled && source.sync_state !== "paused" ? "立即扫描" : "启用本机自动扫描") : id === "github" ? "验证并同步" : "打开登录页"}</button></div>`);
  if (platform.browser) byId("setup-bookmarks").onclick = () => showBookmarksSetup(id);
  byId("connect-account").onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      if (platform.browser) await api("/api/bookmarks/import-local", { platform: id });
      else {
        const result = await api("/api/connect", { platform: id, token: byId("account-token")?.value || "", collections: byId("account-collections")?.value || "" });
        await waitJob(result.job_id);
      }
      await refreshSources();
      if (dialog.open && byId("connect-account") === button) showAccount(id);
      showToast(platform.browser ? "书签已扫描，后台自动扫描已启用" : "同步完成");
    } catch (error) {
      if (dialog.open && byId("connect-account") === button) errorInDialog(error);
      else showError(error);
      await refreshSources().catch(() => {});
    } finally { button.disabled = false; }
  };
  if (source && !platform.browser) {
    byId("sync-account").onclick = async event => {
      const button = event.currentTarget;
      button.disabled = true;
      try { const job = await api("/api/sync", { source_id: source.source_id }); await waitJob(job.job_id); await refreshSources(); showAccount(id); showToast("同步完成"); }
      catch (error) { errorInDialog(error); button.disabled = false; await refreshSources(); }
    };
  }
  const pause = byId("pause-account");
  if (source && pause) {
    pause.onclick = async () => {
      try { await api(source.sync_state === "paused" ? "/api/resume" : "/api/pause", { source_id: source.source_id }); await refreshSources(); showAccount(id); }
      catch (error) { errorInDialog(error); }
    };
  }
}

function showBookmarksSetup(id) {
  const browser = id === "chrome" ? "chrome" : "edge";
  openDialog(`<h2 id="dialog-title">设置书签实时同步</h2>
    <ol class="dialog-steps"><li>在 ${platforms[id].name} 地址栏输入 <strong>${browser}://extensions</strong>，打开开发者模式。</li>
    <li>选择“加载已解压的扩展程序”，选择目录 <strong>${escapeHtml(extensionDir)}</strong>。</li>
    <li>将显示的扩展 ID 填入下方并注册，然后在扩展选项页填写来源 ID <strong>${browser}-live</strong>。</li></ol>
    <label class="dialog-field">浏览器扩展 ID<input id="extension-id" autocomplete="off" maxlength="32"></label>
    <p class="dialog-error" role="alert"></p><div class="dialog-actions"><button class="secondary" type="button" data-close>关闭</button><button class="primary" type="button" id="register-extension">注册本地主机</button></div>`);
  byId("register-extension").onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/bookmarks/register", { platform: browser, extension_id: byId("extension-id").value.trim() });
      await refreshSources();
      dialogContent.querySelector(".dialog-error").textContent = `已注册。请在扩展选项页填写 ${result.source_id} 并保存。`;
      button.disabled = false;
    } catch (error) { errorInDialog(error); button.disabled = false; }
  };
}

for (const [id, platform] of Object.entries(platforms)) {
  if (!platform.browser) scope.add(new Option(platform.name, id));
  byId("library-source").add(new Option(platform.name, id));
}
document.addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.view) changeView(button.dataset.view);
  if (button.dataset.account) showAccount(button.dataset.account);
  if (button.dataset.login) openLoginPages(button, button.dataset.login);
  if (button.id === "login-pending") openLoginPages(button);
  if (button.dataset.record) showRecord(button.dataset.record).catch(showError);
  if (button.hasAttribute("data-close")) dialog.close();
  if (button.id === "load-more") renderLibrary(true).catch(showError);
});
byId("new-chat").onclick = () => { activeChat = null; question.value = ""; changeView("chat"); renderChat(); question.focus(); };
byId("composer").onsubmit = event => { event.preventDefault(); sendQuestion(question.value); };
question.onkeydown = event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault(); byId("composer").requestSubmit();
  }
};
byId("library-query").oninput = () => {
  ++libraryRequest;
  clearTimeout(librarySearchTimer);
  librarySearchTimer = setTimeout(() => renderLibrary().catch(showError), 200);
};
byId("library-source").onchange = () => renderLibrary().catch(showError);
byId("dialog-close").onclick = () => dialog.close();
byId("add-link").onclick = showAdd;
byId("import-file").onclick = showImport;
byId("model-mode").onchange = renderModelMode;
byId("model-url").oninput = () => { byId("model-private").checked = false; };
byId("model-form").oninput = () => { byId("model-status").textContent = ""; };
byId("model-form").onsubmit = event => { event.preventDefault(); submitModel(false); };
byId("test-model").onclick = () => submitModel(true);
byId("mcp-sources").onchange = renderMcpConfig;
byId("mcp-private").onchange = renderMcpConfig;
byId("copy-mcp").onclick = async () => {
  try {
    await navigator.clipboard.writeText(byId("mcp-config").value);
    byId("mcp-status").textContent = "已复制";
  } catch {
    byId("mcp-config").select();
    byId("mcp-status").textContent = "请按 Ctrl+C 复制。";
  }
};

(async () => {
  try {
    const session = await api("/api/session");
    sessionToken = session.token;
    extensionDir = session.extension_dir;
    archiveEnabled = session.archive_enabled;
    useModelSettings(session.model);
    await refreshSources();
  }
  catch { showToast("请先运行 recall serve，再通过 http://127.0.0.1:8765 打开。"); }
})();
setInterval(() => {
  if (!sessionToken || document.visibilityState !== "visible" || dialog.open) return;
  refreshSources().catch(() => {});
  if (currentView === "library" && items.length <= 50) renderLibrary().catch(() => {});
}, 15000);
