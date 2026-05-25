document.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await navigator.clipboard.writeText(btn.dataset.copy || "");
      btn.textContent = "コピー済み";
      setTimeout(() => (btn.textContent = "コピー"), 1200);
    });
  });

  const list = document.getElementById("photo-checklist");
  if (list) {
    const res = await fetch("/api/photo_checklist");
    const data = await res.json();
    data.forEach((item) => {
      const li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    });
  }
});
