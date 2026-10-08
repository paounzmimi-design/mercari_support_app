"use strict";
document.addEventListener("click", async (event) => {
  const toggle = event.target.closest("button[data-toggle]");
  if (toggle) {
    const field = document.getElementById(toggle.dataset.toggle);
    field.type = field.type === "password" ? "text" : "password";
    toggle.textContent = field.type === "password" ? "表示する" : "隠す";
    return;
  }
  const button = event.target.closest("button[data-copy], button[data-save]");
  if (!button) return;
  const field = document.getElementById(button.dataset.copy || button.dataset.save);
  const status = button.nextElementSibling;
  if (!field || !status) return;
  if (button.dataset.save) {
    const url = URL.createObjectURL(new Blob(["出品・手残り管理の復旧コード\n他人に共有しないでください。\n\n" + field.value + "\n"], {type: "text/plain;charset=utf-8"}));
    const link = document.createElement("a");
    link.href = url;
    link.download = "recovery-code.txt";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = " 保存を開始しました。ダウンロード先にファイルがあるか確認してください";
    return;
  }
  try {
    if (!navigator.clipboard) throw new Error("unavailable");
    await navigator.clipboard.writeText(field.value);
    status.textContent = " コピーしました";
  } catch (_) {
    field.focus();
    field.select();
    if (field.setSelectionRange) field.setSelectionRange(0, field.value.length);
    let copied = false;
    try { copied = document.execCommand("copy"); } catch (_) { /* manual fallback */ }
    status.textContent = copied ? " コピーしました" : " 自動コピーできません。選択した文字を手動でコピー：パソコンは Ctrl+C（Macは⌘+C）、スマホは長押し→コピー。復旧コードは「ファイルに保存」も使えます";
  }
});
