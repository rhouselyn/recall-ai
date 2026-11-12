// Recall AI 书签事件同步（开发规格 §5.6）。
// 首版把变化合并后发送完整树；只有本机主机确认对应版本提交后才清除待同步标记。
const NATIVE_HOST = "com.recallai.bookmarks";
const DEBOUNCE_MS = 2000;
const VERIFY_ALARM = "recall-verify-tree";
const MAX_NODES = 20000;

let debounceTimer = null;
let sending = false;

async function getState() {
  return chrome.storage.local.get({
    profileUuid: null,
    sourceId: null,
    version: 0,
    pending: false,
    pendingVersion: 0,
    lastConfirmedVersion: 0
  });
}

async function ensureProfileUuid(state) {
  if (state.profileUuid) return state.profileUuid;
  const profileUuid = crypto.randomUUID();
  await chrome.storage.local.set({ profileUuid });
  return profileUuid;
}

function flatten(nodes, out) {
  for (const node of nodes) {
    out.push({
      id: String(node.id),
      parentId: node.parentId === undefined || node.parentId === null ? "" : String(node.parentId),
      title: node.title || "",
      url: node.url || "",
      dateAdded: typeof node.dateAdded === "number" ? node.dateAdded : null,
      index: typeof node.index === "number" ? node.index : null
    });
    if (out.length > MAX_NODES) throw new Error("TREE_TOO_LARGE");
    if (node.children) flatten(node.children, out);
  }
  return out;
}

function scheduleSend() {
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
    sendTree().catch((err) => console.error("[recall] send failed", String(err)));
  }, DEBOUNCE_MS);
}

async function sendTree() {
  if (sending) return;
  sending = true;
  try {
    const state = await getState();
    if (!state.sourceId) return; // 未绑定资料档来源时不发送
    const profileUuid = await ensureProfileUuid(state);
    const tree = await chrome.bookmarks.getTree();
    const nodes = flatten(tree, []);
    // 单调版本：待同步标记未清除前不重复推进版本，防止旧快照覆盖。
    const version = state.pending ? state.pendingVersion : state.version + 1;
    await chrome.storage.local.set({ pending: true, pendingVersion: version });
    const message = {
      source_id: state.sourceId,
      browser: /Edg\//.test(navigator.userAgent) ? "edge" : "chrome",
      profile_uuid: profileUuid,
      version,
      nodes
    };
    const response = await chrome.runtime.sendNativeMessage(NATIVE_HOST, message);
    if (response && response.ok && response.version === version) {
      await chrome.storage.local.set({
        pending: false,
        version,
        lastConfirmedVersion: version
      });
    } else {
      // 保留待同步标记，下一次启动/重连/定时校验补发最新树。
      console.warn("[recall] host did not confirm version", version);
    }
  } finally {
    sending = false;
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(VERIFY_ALARM, { periodInMinutes: 60, delayInMinutes: 1 });
  sendTree().catch((err) => console.error("[recall] initial send failed", String(err)));
});

chrome.runtime.onStartup.addListener(() => {
  sendTree().catch((err) => console.error("[recall] startup send failed", String(err)));
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === VERIFY_ALARM) {
    sendTree().catch((err) => console.error("[recall] verify failed", String(err)));
  }
});

chrome.bookmarks.onCreated.addListener(scheduleSend);
chrome.bookmarks.onRemoved.addListener(scheduleSend);
chrome.bookmarks.onChanged.addListener(scheduleSend);
chrome.bookmarks.onMoved.addListener(scheduleSend);
chrome.bookmarks.onChildrenReordered.addListener(scheduleSend);
chrome.bookmarks.onImportEnded.addListener(() => {
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = null;
  sendTree().catch((err) => console.error("[recall] import send failed", String(err)));
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "recall-set-binding") {
    chrome.storage.local.set({ sourceId: String(message.sourceId || "") }, () => {
      sendTree()
        .then(() => sendResponse({ ok: true }))
        .catch((err) => sendResponse({ ok: false, error: String(err) }));
    });
    return true;
  }
  return false;
});
