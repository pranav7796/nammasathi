// Connected apps: one honest card per service. "Connected" appears only when the server has just
// asked the service itself. Built with textContent only; nothing from the server becomes markup.
(() => {
  const dialog = document.querySelector("#apps-dialog");
  const grid = document.querySelector("#apps-grid");
  const opener = document.querySelector("#apps-button");
  const badge = document.querySelector("#apps-count");
  if (!dialog || !grid || !opener) return;

  const ICON = { gmail: "mail", calendar: "calendar", whatsapp: "chat", twilio: "chat", swiggy: "bag" };
  const LABEL = {
    connected: "Connected",
    not_connected: "Not connected",
    configuration_required: "Configuration required",
    authorization_failed: "Authorization failed",
    unavailable: "Temporarily unavailable",
    loading: "Checking…",
  };
  const SVG = "http://www.w3.org/2000/svg";

  function icon(name) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG, "use");
    use.setAttribute("href", `#icon-${name}`);
    svg.append(use);
    return svg;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function call(path, method = "GET", body = null) {
    const response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "That did not work. Please try again.");
    return data;
  }

  function when(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime())
      ? ""
      : date.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  function card(app) {
    const root = el("article", "app-card");
    root.dataset.app = app.id;
    root.setAttribute("aria-label", app.name);

    const top = el("div", "app-top");
    const tile = el("div", "app-tile");
    tile.dataset.app = app.id;
    tile.append(icon(ICON[app.id] || "apps"));
    const name = el("div", "app-name");
    name.append(el("h3", "", app.name), el("p", "", app.description || ""));
    top.append(tile, name);
    root.append(top);

    const chip = el("span", "status-chip", LABEL[app.state] || app.state);
    chip.dataset.state = app.state;
    root.append(chip);

    if (app.detail) root.append(el("p", "app-detail", app.detail));
    if (app.account) root.append(el("p", "app-meta", `Account: ${app.account}`));
    if (app.last_connected && when(app.last_connected)) {
      root.append(el("p", "app-meta", `Last connected ${when(app.last_connected)}`));
    }

    if (app.setup && app.setup.length) {
      const box = el("p", "app-setup", "The site's owner needs to set these on the server: ");
      for (const name of app.setup) box.append(el("code", "", name));
      root.append(box);
    }
    if (app.notes && app.notes.length) {
      const list = el("ul");
      for (const note of app.notes) list.append(el("li", "", note));
      const details = el("details");
      details.append(el("summary", "", "Good to know"), list);
      root.append(details);
    }
    if (app.permissions && app.permissions.length) {
      const list = el("ul");
      for (const item of app.permissions) list.append(el("li", "", item));
      const details = el("details");
      details.append(el("summary", "", "What Saathi can do here"), list);
      root.append(details);
    }

    const actions = el("div", "app-actions");
    if (app.can_connect) {
      const label = app.id === "twilio" ? "Link my WhatsApp" : app.state === "authorization_failed" ? "Connect again" : "Connect";
      const connect = el("button", "primary", label);
      connect.type = "button";
      connect.addEventListener("click", () => start(app));
      actions.append(connect);
    }
    if (app.can_disconnect) {
      const off = el(
      "button",
      "secondary-button",
      app.id === "swiggy" ? "Manage" : app.id === "twilio" ? "Unlink" : "Disconnect",
    );
      off.type = "button";
      off.addEventListener("click", () => stop(app));
      actions.append(off);
    }
    if (actions.children.length) root.append(actions);
    return root;
  }

  function placeholders() {
    grid.replaceChildren();
    for (const [id, name] of [["gmail", "Gmail"], ["calendar", "Google Calendar"], ["whatsapp", "WhatsApp Business"], ["twilio", "WhatsApp (Twilio)"], ["swiggy", "Swiggy"]]) {
      grid.append(card({ id, name, description: "", state: "loading" }));
    }
  }

  async function load() {
    placeholders();
    try {
      const { apps } = await call("/api/integrations");
      grid.replaceChildren(...apps.map(card));
      const connected = apps.filter((a) => a.state === "connected").length;
      if (badge) badge.textContent = connected ? String(connected) : "";
    } catch (error) {
      grid.replaceChildren(el("p", "app-detail", error.message));
    }
  }

  async function start(app) {
    if (app.id === "twilio") {
      try {
        const link = await call("/api/twilio/link", "POST");
        await window.saathiConfirm(
          "Link your WhatsApp",
          `From your phone, send this WhatsApp message to ${link.send_to || "Saathi's number"}: \u201c${link.message}\u201d. The code works once, for 15 minutes.`,
          "I've sent it",
        );
      } catch (error) {
        window.saathiToast?.(error.message, "error");
      }
      await load();
      return;
    }
    if (app.id === "swiggy") {
      dialog.close();
      window.saathiOpenAccount?.();
      return;
    }
    try {
      const { url } = await call(`/api/google/connect?service=${encodeURIComponent(app.id)}`);
      window.location.href = url; // Google's own page asks for permission, not ours
    } catch (error) {
      window.saathiToast?.(error.message, "error");
    }
  }

  async function stop(app) {
    if (app.id === "twilio") {
      const sure = await window.saathiConfirm(
        "Unlink your WhatsApp?",
        "Messages from your number will no longer reach your account. You can link it again any time.",
        "Unlink",
      );
      if (!sure) return;
      try {
        await call("/api/twilio/unlink", "POST");
        window.saathiToast?.("Your WhatsApp number is unlinked.", "success");
      } catch (error) {
        window.saathiToast?.(error.message, "error");
      }
      await load();
      return;
    }
    if (app.id === "swiggy") {
      dialog.close();
      window.saathiOpenAccount?.();
      return;
    }
    const sure = await window.saathiConfirm(
      `Disconnect ${app.name}?`,
      "Saathi stops using it straight away and asks Google to revoke its access. You can connect again any time.",
      "Disconnect",
    );
    if (!sure) return;
    try {
      await call("/api/google/disconnect", "POST", { service: app.id });
      window.saathiToast?.(`${app.name} is disconnected.`, "success");
    } catch (error) {
      window.saathiToast?.(error.message, "error");
    }
    await load();
  }

  async function open() {
    if (!dialog.open) dialog.showModal();
    await load();
  }

  opener.addEventListener("click", open);
  document.querySelector("#apps-close")?.addEventListener("click", () => dialog.close());
  document.querySelector("#apps-refresh")?.addEventListener("click", load);
  window.saathiOpenApps = open;

  // The button is for signed-in people. Coming back from Google, say how it went.
  window.addEventListener("saathi:me", (event) => {
    opener.hidden = !(event.detail && event.detail.user);
  });
  const result = (window.location.hash || "").match(/google=(connected|error)/);
  if (result) {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    window.setTimeout(() => {
      if (result[1] === "connected") {
        window.saathiToast?.("Connected. Saathi will use it from now on.", "success");
      } else {
        window.saathiToast?.("That connection didn't finish. See the reason under Apps.", "error");
        void open();
      }
    }, 600);
  }
})();
