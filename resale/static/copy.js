"use strict";
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-copy]");
  if (!button) return;
  const field = document.getElementById(button.dataset.copy);
  const status = button.nextElementSibling;
  if (!field || !status) return;
  try {
    if (!navigator.clipboard) throw new Error("unavailable");
    await navigator.clipboard.writeText(field.value);
    status.textContent = " コピーしました";
  } catch (_) {
    field.focus();
    field.select();
    status.textContent = " コピーできませんでした。選択した文字を手動でコピーしてください";
  }
});
