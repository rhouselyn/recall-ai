const platforms = {
  chrome: { name: "Chrome", mark: "chrome", data: "浏览器书签", browser: true },
  github: { name: "GitHub", mark: "github", data: "本人可访问的 Star 仓库" },
  bilibili: { name: "哔哩哔哩", mark: "bilibili", data: "本人视频收藏夹" },
  youtube: { name: "YouTube", mark: "youtube", data: "播放列表、稍后观看和点赞视频" },
  xiaohongshu: { name: "小红书", mark: "书", data: "本人收藏的笔记和专辑", needsId: true },
  douyin: { name: "抖音", mark: "douyin", data: "本人收藏的视频（分组待验证）", needsId: true },
  zhihu: { name: "知乎", mark: "知", data: "本人私密收藏夹", needsId: true },
  edge: { name: "Microsoft Edge", mark: "e", data: "浏览器收藏夹", browser: true },
};
const names = { chat: "对话", library: "收藏库", accounts: "账号连接" };
const conversations = [];
let activeChat = null;
let currentView = "chat";
let sources = [];
let items = [];
let nextCursor = null;
let sourcesInitialized = false;
let libraryInitialized = false;
let sessionToken = "";
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
  if (!response.ok) throw new Error(data.error?.message || "请求失败，请重试。");
  return data;
}

function platformIcon(id) {
  const platform = platforms[id] || { mark: "bookmark" };
  const mark = platform.mark;
  return `<span class="platform ${escapeHtml(id)}" aria-hidden="true">${mark.length > 1 ? icon(mark) : mark}</span>`;
}

function sourceFor(id) {
  return sources.find(source => source.provider === id && source.kind === "bookmarks_live") ||
    sources.find(source => source.provider === id && source.kind !== "manual") ||
    sources.find(source => source.provider === id);
}

function statusFor(source) {
  if (!source) return "未连接";
  if (source.sync_state === "needs_login") return "需要登录";
  if (source.sync_state === "paused") return "已暂停";
  if (source.latest_attempt === "failed") return "同步失败";
  if (source.last_success_at && source.kind === "bookmarks_html") return "已导入";
  if (source.last_success_at && source.latest_attempt === "partial") return "部分同步";
  if (source.last_success_at) return "已连接";
  return "待同步";
}

function connectionStatus(source) {
  const status = statusFor(source);
  return `<span class="row-status${status === "已连接" ? " connected" : ""}"><span class="status-dot"></span>${status}</span>`;
}

async function refreshSources() {
  const data = await api("/api/sources");
  if (sourcesInitialized && JSON.stringify(sources) === JSON.stringify(data.sources)) return;
  sources = data.sources;
  sourcesInitialized = true;
  renderAccounts();
}

function renderAccounts() {
  const count = Object.keys(platforms).filter(id => statusFor(sourceFor(id)) === "已连接").length;
  byId("connection-count").textContent = `${count} / ${Object.keys(platforms).length} 个平台已连接`;
  byId("account-summary").innerHTML = Object.entries(platforms).slice(0, 5).map(([id, platform]) => `
    <button type="button" class="summary-row" data-account="${id}">
      ${platformIcon(id)}<strong>${platform.name}</strong>${connectionStatus(sourceFor(id))}
    </button>`).join("");
  byId("account-list").innerHTML = Object.entries(platforms).map(([id, platform]) => `
    <article class="account-card"><div class="account-card-head">${platformIcon(id)}<h2>${platform.name}</h2></div>
      <p>${platform.data}</p><div class="account-card-footer">${connectionStatus(sourceFor(id))}
      <button type="button" class="secondary" data-account="${id}">${sourceFor(id) ? "查看来源" : "连接账号"}<span class="sr-only"> ${platform.name}</span></button></div></article>`).join("");
}

async function renderLibrary(append = false) {
  const params = new URLSearchParams();
  const query = byId("library-query").value.trim();
  const platform = byId("library-source").value;
  if (query) params.set("query", query);
  if (platform) params.set("platform", platform);
  if (append && nextCursor) params.set("cursor", nextCursor);
  const result = await api("/api/items?" + params);
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
  for (const id of Object.keys(names)) byId(`view-${id}`).hidden = id !== view;
  for (const button of document.querySelectorAll(".nav-link")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  byId("current-view").textContent = names[view];
  renderHistory();
  if (view === "library") renderLibrary().catch(showError);
  if (view === "accounts") refreshSources().catch(showError);
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

function renderChat() {
  const turns = activeChat?.turns ?? [];
  byId("suggestions").hidden = turns.length > 0;
  messages.hidden = turns.length === 0;
  messages.replaceChildren(...turns.map(turn => {
    const article = document.createElement("article");
    article.className = `message ${turn.role}`;
    article.innerHTML = '<p class="message-label"></p><p class="message-body"></p>';
    article.querySelector(".message-label").textContent = turn.role === "user" ? "你" : "Recall AI";
    article.querySelector(".message-body").textContent = turn.text;
    if (turn.items?.length) {
      const refs = document.createElement("div");
      refs.className = "citations";
      refs.innerHTML = turn.items.map((item, i) =>
        `<button class="citation" type="button" data-record="${escapeHtml(item.item_id)}">[${i + 1}] ${escapeHtml(item.title)}</button>`).join("");
      article.append(refs);
    }
    return article;
  }));
  messages.scrollTop = messages.scrollHeight;
}

async function sendQuestion(value) {
  const text = value.trim();
  if (!text) return;
  if (!activeChat) { activeChat = { title: text, turns: [] }; conversations.unshift(activeChat); }
  const chat = activeChat;
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

async function showRecord(id) {
  const data = await api("/api/items/" + encodeURIComponent(id));
  const item = data.item;
  const evidence = data.evidence.map(entry => `
    <p class="dialog-info">${escapeHtml([entry.source_id, ...entry.path].join(" / "))}
      ${entry.segments?.map(segment => `<br>${escapeHtml(segment.label)}：${escapeHtml(segment.text)}`).join("") || ""}</p>`).join("");
  openDialog(`${platformIcon(item.platform)}<h2 id="dialog-title">${escapeHtml(item.title)}</h2>
    <p class="dialog-description">${escapeHtml(item.excerpt || "暂无摘要。")}</p>${evidence}
    <div class="dialog-actions"><a class="secondary" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">打开原文</a>
    <button class="secondary" type="button" id="extract-media">提取图片或音频文字</button>
    <button class="secondary" type="button" data-close>关闭</button></div>`);
  byId("extract-media").onclick = () => showMedia(item.item_id, data.evidence);
}

function showMedia(itemId, evidence) {
  const options = evidence.map(entry => `<option value="${escapeHtml(entry.entry_id)}">${escapeHtml([entry.source_id, ...entry.path].join(" / "))}</option>`).join("");
  openDialog(`<h2 id="dialog-title">提取媒体文字</h2><p class="dialog-description">选择本地图片或音视频，识别出的文字会关联到选定来源并参与检索。</p>
    <label class="dialog-field">关联来源<select id="media-entry">${options}</select></label>
    <label class="dialog-field">媒体文件<input type="file" id="media-input" accept="image/*,audio/*,video/*"></label>
    <p class="dialog-error" role="alert"></p><div class="dialog-actions"><button type="button" class="secondary" data-close>取消</button><button type="button" class="primary" id="do-media">开始提取</button></div>`);
  byId("do-media").onclick = async event => {
    const file = byId("media-input").files[0];
    if (!file || file.size > 20 * 1024 * 1024) return errorInDialog(new Error("请选择不超过 20 MiB 的媒体文件。"));
    event.currentTarget.disabled = true;
    try {
      const content = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result.split(",")[1]);
        reader.onerror = () => reject(new Error("文件读取失败。"));
        reader.readAsDataURL(file);
      });
      const result = await api("/api/media", { item_id: itemId, entry_id: byId("media-entry").value, filename: file.name, content });
      const outcome = await waitJob(result.job_id);
      dialog.close();
      showToast(`提取完成，共 ${outcome.segments} 个文字片段`);
    } catch (error) { errorInDialog(error); event.currentTarget.disabled = false; }
  };
}

function showImport(kind = "csv") {
  openDialog(`<h2 id="dialog-title">导入收藏</h2>
    <p class="dialog-description">导入浏览器导出的书签 HTML，或 Recall 格式的 CSV。</p>
    <label class="dialog-field">文件类型<select id="import-kind"><option value="csv">CSV 清单</option><option value="chrome">Chrome 书签 HTML</option><option value="edge">Edge 收藏 HTML</option></select></label>
    <label class="dialog-field">选择文件<input type="file" id="import-input" accept=".csv,.html,.htm"></label>
    <p class="dialog-error" role="alert"></p><div class="dialog-actions"><button type="button" class="secondary" data-close>取消</button><button type="button" class="primary" id="do-import">导入</button></div>`);
  byId("import-kind").value = kind;
  byId("do-import").onclick = async event => {
    const file = byId("import-input").files[0];
    if (!file || file.size > 20 * 1024 * 1024) return errorInDialog(new Error("请选择不超过 20 MiB 的文件。"));
    event.currentTarget.disabled = true;
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
    } catch (error) { errorInDialog(error); event.currentTarget.disabled = false; }
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

function showAccount(id) {
  const platform = platforms[id];
  const source = sourceFor(id);
  const status = statusFor(source);
  const errorNames = { AUTH_REQUIRED: "需要重新登录", PERMISSION_DENIED: "平台拒绝访问", RATE_LIMITED: "平台限流", SOURCE_FORMAT_CHANGED: "平台页面格式发生变化", NETWORK_ERROR: "网络连接失败", ACCOUNT_MISMATCH: "登录账号不匹配", SNAPSHOT_INCOMPLETE: "本次同步不完整" };
  const state = source ? `<p class="dialog-info">状态：${status} · ${source.visible_entry_count} 条收藏<br>上次成功：${escapeHtml(source.last_success_at || "尚未同步")}${source.latest_attempt === "failed" ? `<br>最近错误：${escapeHtml(errorNames[source.last_error_type] || "同步失败，请重试")}` : ""}${source.coverage === "authorized_subset" ? "<br>当前只覆盖已读取到的部分收藏。" : ""}</p>` : "";
  const browserInfo = platform.browser
    ? '<p class="dialog-info">导入浏览器书签 HTML，或按 README 安装扩展以持续同步。</p>'
    : `<p class="dialog-info">${id === "github" ? "Windows 下的只读令牌会加密保存在当前用户的本机凭据中。" : "将在本机打开浏览器。完成登录后请关闭该窗口，系统会尝试同步。"}</p>
      ${id === "github" ? '<label class="dialog-field">GitHub 只读令牌<input id="account-token" type="password" autocomplete="off"></label>' : ""}
      ${platform.needsId ? '<label class="dialog-field">平台用户 ID<input id="account-user" autocomplete="off"></label>' : ""}
      ${id === "zhihu" ? '<label class="dialog-field">私密收藏夹数字 ID，多个用逗号分隔<input id="account-collections"></label>' : ""}`;
  openDialog(`${platformIcon(id)}<h2 id="dialog-title">${platform.name}</h2><p class="dialog-description">读取范围 · ${platform.data}</p>${state}${browserInfo}
    <p class="dialog-error" role="alert"></p><div class="dialog-actions">
    ${source && !platform.browser ? `<button class="secondary" type="button" id="pause-account">${source.sync_state === "paused" ? "恢复同步" : "暂停同步"}</button><button class="secondary" type="button" id="sync-account">立即同步</button>` : ""}
    ${platform.browser ? '<button class="secondary" type="button" id="setup-bookmarks">设置实时同步</button>' : ""}
    <button class="primary" type="button" id="connect-account">${platform.browser ? "导入书签" : "登录并同步"}</button></div>`);
  if (platform.browser) byId("setup-bookmarks").onclick = () => showBookmarksSetup(id);
  byId("connect-account").onclick = async event => {
    if (platform.browser) return showImport(id);
    event.currentTarget.disabled = true;
    try {
      const result = await api("/api/connect", { platform: id, token: byId("account-token")?.value || "", user_id: byId("account-user")?.value || "", collections: byId("account-collections")?.value || "" });
      const notes = dialogContent.querySelectorAll(".dialog-info");
      notes[notes.length - 1].textContent = id === "github" ? "正在同步收藏…" : "请在新窗口完成登录，然后关闭窗口。";
      await waitJob(result.job_id);
      await refreshSources(); showAccount(id); showToast("同步完成");
    } catch (error) { errorInDialog(error); event.currentTarget.disabled = false; await refreshSources(); }
  };
  if (source && !platform.browser) {
    byId("sync-account").onclick = async event => {
      event.currentTarget.disabled = true;
      try { const job = await api("/api/sync", { source_id: source.source_id }); await waitJob(job.job_id); await refreshSources(); showAccount(id); showToast("同步完成"); }
      catch (error) { errorInDialog(error); event.currentTarget.disabled = false; await refreshSources(); }
    };
    byId("pause-account").onclick = async () => {
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
    event.currentTarget.disabled = true;
    try {
      const result = await api("/api/bookmarks/register", { platform: browser, extension_id: byId("extension-id").value.trim() });
      await refreshSources();
      dialogContent.querySelector(".dialog-error").textContent = `已注册。请在扩展选项页填写 ${result.source_id} 并保存。`;
      event.currentTarget.disabled = false;
    } catch (error) { errorInDialog(error); event.currentTarget.disabled = false; }
  };
}

for (const [id, platform] of Object.entries(platforms)) {
  if (!platform.browser) { scope.add(new Option(platform.name, id)); byId("library-source").add(new Option(platform.name, id)); }
}
document.addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.view) changeView(button.dataset.view);
  if (button.dataset.prompt) sendQuestion(button.dataset.prompt);
  if (button.dataset.account) showAccount(button.dataset.account);
  if (button.dataset.record) showRecord(button.dataset.record).catch(showError);
  if (button.hasAttribute("data-close")) dialog.close();
  if (button.id === "load-more") renderLibrary(true).catch(showError);
});
document.querySelector(".brand").addEventListener("click", event => { event.preventDefault(); changeView("chat"); });
byId("new-chat").onclick = () => { activeChat = null; question.value = ""; changeView("chat"); renderChat(); question.focus(); };
byId("composer").onsubmit = event => { event.preventDefault(); sendQuestion(question.value); };
question.onkeydown = event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault(); byId("composer").requestSubmit();
  }
};
byId("library-query").oninput = () => renderLibrary().catch(showError);
byId("library-source").onchange = () => renderLibrary().catch(showError);
byId("dialog-close").onclick = () => dialog.close();
byId("add-link").onclick = showAdd;
byId("import-file").onclick = () => showImport();

(async () => {
  try { const session = await api("/api/session"); sessionToken = session.token; extensionDir = session.extension_dir; await refreshSources(); }
  catch { showToast("请先运行 recall serve，再通过 http://127.0.0.1:8765 打开。"); }
})();
setInterval(() => {
  if (!sessionToken || document.visibilityState !== "visible" || dialog.open) return;
  refreshSources().catch(() => {});
  if (currentView === "library" && items.length <= 50) renderLibrary().catch(() => {});
}, 15000);
