document.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await navigator.clipboard.writeText(btn.dataset.copy || "");
      const feedback = btn.parentElement.querySelector(".copy-feedback") || btn.nextElementSibling;
      if (feedback) {
        feedback.textContent = "コピーしました";
        setTimeout(() => (feedback.textContent = ""), 2000);
      }
    });
  });

  const list = document.getElementById("photo-checklist");
  if (list) {
    const category = list.dataset.category || "その他";
    const res = await fetch(`/api/photo_checklist?category=${encodeURIComponent(category)}`);
    const data = await res.json();
    data.forEach((item) => {
      const li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    });
  }
});
