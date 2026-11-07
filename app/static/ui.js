// Small shared pieces: toasts and a confirmation dialog. Plain DOM, textContent only.
(() => {
  const host = document.querySelector("#toasts");

  // A short message that fades on its own. kind: "info" | "success" | "error".
  window.saathiToast = (message, kind = "info") => {
    if (!host || !message) return;
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.dataset.kind = kind;
    const text = document.createElement("span");
    text.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", () => toast.remove());
    toast.append(text, close);
    host.append(toast);
    while (host.children.length > 3) host.firstElementChild?.remove();
    window.setTimeout(() => toast.remove(), kind === "error" ? 9000 : 5000);
  };

  // Ask before anything hard to undo. Resolves true only if the person presses the confirm button.
  window.saathiConfirm = (title, body, confirmLabel = "Confirm") =>
    new Promise((resolve) => {
      const dialog = document.querySelector("#confirm-dialog");
      if (!dialog || typeof dialog.showModal !== "function") {
        resolve(window.confirm(`${title}\n\n${body}`));
        return;
      }
      document.querySelector("#confirm-title").textContent = title;
      document.querySelector("#confirm-body").textContent = body;
      const ok = document.querySelector("#confirm-ok");
      const cancel = document.querySelector("#confirm-cancel");
      ok.textContent = confirmLabel;
      let answer = false;
      const done = () => {
        ok.removeEventListener("click", yes);
        cancel.removeEventListener("click", no);
        dialog.removeEventListener("close", done);
        resolve(answer);
      };
      const yes = () => {
        answer = true;
        dialog.close();
      };
      const no = () => dialog.close();
      ok.addEventListener("click", yes);
      cancel.addEventListener("click", no);
      dialog.addEventListener("close", done);
      dialog.showModal();
      cancel.focus();
    });

  // The header gains a shadow once the page scrolls under it.
  const bar = document.querySelector("#topbar");
  const stick = () => bar?.classList.toggle("is-stuck", window.scrollY > 4);
  window.addEventListener("scroll", stick, { passive: true });
  stick();

  // The header reports its real height too: it wraps to two rows on a phone.
  if (bar && "ResizeObserver" in window) {
    const measureBar = () => {
      if (bar.offsetHeight) {
        document.documentElement.style.setProperty("--header-h", `${bar.offsetHeight}px`);
      }
    };
    new ResizeObserver(measureBar).observe(bar);
    measureBar();
  }

  // The fixed composer reports its real height, so nothing is ever hidden behind it.
  const composer = document.querySelector("#composer");
  if (composer && "ResizeObserver" in window) {
    const measure = () => {
      if (composer.offsetHeight) {
        document.documentElement.style.setProperty("--composer-h", `${composer.offsetHeight}px`);
      }
    };
    new ResizeObserver(measure).observe(composer);
    measure();
  }
})();
