const input = document.getElementById("sourceId");
const status = document.getElementById("status");

chrome.storage.local.get({ sourceId: "" }, (state) => {
  input.value = state.sourceId || "";
});

document.getElementById("save").addEventListener("click", () => {
  const sourceId = input.value.trim();
  if (!/^[A-Za-z0-9._-]{1,120}$/.test(sourceId)) {
    status.textContent = "来源 ID 只能包含字母、数字、点、下划线和连字符。";
    return;
  }
  chrome.runtime.sendMessage({ type: "recall-set-binding", sourceId }, (response) => {
    status.textContent = response && response.ok ? "已绑定并发起同步。" : "绑定未完成，请查看后台日志。";
  });
});
