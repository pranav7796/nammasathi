"use strict";

// Accounts, and each person's own Swiggy connection. Hidden on a site without accounts.
(() => {
  const dialog = document.querySelector("#account-dialog");
  const accountButton = document.querySelector("#account-button");
  const modeChoice = document.querySelector("#mode-choice");
  const modeLive = document.querySelector("#mode-live");
  const modeDemo = document.querySelector("#mode-demo");
  const modeLiveDetail = document.querySelector("#mode-live-detail");
  const connectionNotice = document.querySelector("#connection-notice");
  const message = document.querySelector("#account-message");
  const accountsNotice = document.querySelector("#accounts-notice");
  const views = ["signin", "register", "forgot", "reset", "account"];
  let me = { enabled: false };
  let resetToken = "";

  const byId = (id) => document.querySelector(`#${id}`);

  function showView(name) {
    for (const view of views) byId(`account-view-${view}`).hidden = view !== name;
    say("");
  }

  function say(text, isError = false) {
    message.textContent = text;
    message.hidden = !text;
    message.dataset.tone = isError ? "error" : "info";
  }

  async function call(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try {
      data = await response.json();
    } catch {
      data = {};
    }
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? "Please check what you entered." : data.detail;
      throw new Error(detail || "That did not work. Please try again.");
    }
    return data;
  }

  function submit(formId, handler) {
    byId(formId).addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.target;
      const button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      say("");
      try {
        await handler(new FormData(form), form);
      } catch (error) {
        say(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  }

  function renderOrders(orders) {
    const list = byId("account-orders");
    list.replaceChildren();
    byId("account-orders-empty").hidden = orders.length > 0;
    for (const order of orders) {
      const row = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = order.items.join(", ") || "Table booking";
      const detail = document.createElement("span");
      const when = new Date(order.created_at).toLocaleString();
      const state = order.needs_attention
        ? "Status unknown: please check the Swiggy app and do not order again"
        : order.order_status || order.status.replace("_", " ");
      detail.textContent = `₹${order.total_inr} · ${state} · ${when}`;
      row.append(title, detail);
      if (order.needs_attention) row.dataset.tone = "attention";
      list.append(row);
    }
  }

  const CAPABILITY = { grocery: "Groceries", food: "Meal", dining: "Table" };

  function renderDemoOrders(orders) {
    const list = byId("account-demo-orders");
    list.replaceChildren();
    byId("account-demo-empty").hidden = orders.length > 0;
    for (const order of orders) {
      const row = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = `${CAPABILITY[order.capability] || "Demo"}: ${order.summary || order.reference}`;
      const detail = document.createElement("span");
      const total = order.capability === "dining" ? "free table" : `₹${order.total_inr}`;
      detail.textContent = `Demo only, not real · ${total} · ${new Date(order.created_at).toLocaleString()}`;
      row.append(title, detail);
      list.append(row);
    }
  }

  function renderHistory(turns) {
    const list = byId("account-history");
    list.replaceChildren();
    byId("account-history-clear").hidden = turns.length === 0;
    if (turns.length === 0) {
      const empty = document.createElement("li");
      empty.textContent = "Nothing yet.";
      list.append(empty);
      return;
    }
    for (const turn of turns) {
      const row = document.createElement("li");
      if (turn.said) {
        const said = document.createElement("span");
        said.className = "said";
        said.textContent = turn.said.startsWith("[") ? "(tapped a button)" : `You: ${turn.said}`;
        row.append(said);
      }
      const replied = document.createElement("span");
      replied.className = "replied";
      replied.textContent = `Saathi: ${turn.reply}`;
      row.append(replied);
      list.append(row);
    }
  }

  async function loadList(url, key, render) {
    const response = await fetch(url);
    if (response.ok) render((await response.json())[key]);
  }

  function renderSwiggy() {
    const live = me.live || {};
    const card = byId("account-swiggy");
    card.hidden = !live.open;
    if (!live.open) return;
    const staging = live.environment === "staging";
    const status = byId("account-swiggy-status");
    const connect = byId("account-connect");
    const disconnect = byId("account-disconnect");
    byId("account-swiggy-env").hidden = !staging;
    if (live.connected) {
      status.textContent = "Connected. Saathi uses this connection only for you.";
    } else if (live.available === false && !me.user?.live_access) {
      status.textContent = "Live Swiggy is invite-only for now. You can use the demo shops any time.";
    } else if (!me.user?.email_verified) {
      status.textContent = "Please verify your email first.";
    } else {
      status.textContent = "Not connected. You sign in on Swiggy's own page; Saathi never sees your OTP.";
    }
    const canConnect = me.user?.email_verified && me.user?.live_access && !live.connected;
    connect.hidden = !canConnect;
    disconnect.hidden = !live.connected;
    byId("account-swiggy-limits").textContent = live.connected
      ? [
          live.cart_writes_enabled ? "" : "Saathi can look, but cannot change your cart yet.",
          live.checkout_enabled ? "" : "Placing orders is switched off.",
          live.booking_enabled ? "" : "Booking tables is switched off.",
        ].filter(Boolean).join(" ")
      : "";
  }

  function renderMode() {
    const live = me.live || {};
    const available = live.available === true;
    modeChoice.hidden = !available;
    modeLiveDetail.textContent = live.environment === "staging"
      ? "Swiggy's test environment, with test data"
      : "Real items and prices. Real orders, only after your yes.";
    if (!available) setMode(false);
    connectionNotice.hidden = !(me.user && me.user.live_access && live.open && !live.connected);
  }

  function setMode(useLive) {
    modeLive.setAttribute("aria-pressed", String(useLive));
    modeDemo.setAttribute("aria-pressed", String(!useLive));
    window.saathiSetLive?.(useLive);
  }

  function render() {
    window.dispatchEvent(new CustomEvent("saathi:me", { detail: me }));
    accountButton.hidden = !me.enabled;
    const waitlist = document.querySelector("#waitlist");
    if (waitlist) waitlist.hidden = !me.enabled || me.available === false;
    accountsNotice.hidden = me.available !== false;
    accountsNotice.textContent = me.message || "";
    if (!me.enabled || me.available === false) {
      if (me.available === false) accountButton.hidden = true;
      return;
    }
    accountButton.textContent = me.user ? "Account" : "Sign in";
    renderMode();
    if (!me.user) return;
    byId("account-email").textContent = me.user.email;
    byId("account-verified").textContent = me.user.email_verified
      ? "Email verified"
      : me.email_available
        ? "Email not verified yet. Check your inbox."
        : "Email not verified yet.";
    byId("account-resend").hidden = me.user.email_verified || !me.email_available;
    renderSwiggy();
  }

  async function refresh() {
    const response = await fetch("/api/account/me");
    if (!response.ok) return;
    const wasAvailable = me.live?.available === true;
    me = await response.json();
    render();
    if (!wasAvailable && me.live?.available === true) setMode(true);
    // A returning person starts in the language they last used.
    const saved = me.preferences?.language;
    if (saved && !firstLoadDone) window.saathiSetLanguage?.(saved);
    firstLoadDone = true;
  }
  let firstLoadDone = false;
  window.saathiAccountRefresh = refresh;
  window.saathiOpenAccount = () => void openAccount();

  async function openAccount() {
    await refresh();
    if (!me.user) {
      byId("register-open").hidden = me.registration_open === false;
      showView("signin");
    } else {
      showView("account");
      await Promise.all([
        loadList("/api/account/orders", "orders", renderOrders),
        loadList("/api/account/demo-orders", "orders", renderDemoOrders),
        loadList("/api/account/history", "turns", renderHistory),
      ]);
    }
    if (!dialog.open) dialog.showModal();
  }

  accountButton.addEventListener("click", () => void openAccount());
  byId("account-close").addEventListener("click", () => dialog.close());
  byId("connection-notice-open").addEventListener("click", () => void openAccount());
  modeLive.addEventListener("click", () => setMode(true));
  modeDemo.addEventListener("click", () => setMode(false));
  for (const [id, view] of [
    ["register-open", "register"],
    ["forgot-open", "forgot"],
    ["signin-from-register", "signin"],
    ["signin-from-forgot", "signin"],
  ]) {
    byId(id).addEventListener("click", () => showView(view));
  }

  submit("signin-form", async (data, form) => {
    await call("/api/account/login", { email: data.get("email"), password: data.get("password") });
    form.reset();
    await openAccount();
  });

  submit("register-form", async (data, form) => {
    await call("/api/account/register", {
      email: data.get("email"),
      password: data.get("password"),
      consent: data.get("consent") === "on",
    });
    form.reset();
    await openAccount();
  });

  submit("forgot-form", async (data) => {
    const result = await call("/api/account/forgot", { email: data.get("email") });
    if (result.status === "sent") {
      say("If that email has an account, a reset link is on its way.");
    } else {
      const contact = result.support_email ? ` Write to ${result.support_email}.` : "";
      say(`This site cannot send email yet, so a reset needs a person's help.${contact}`, true);
    }
  });

  submit("reset-form", async (data, form) => {
    await call("/api/account/reset", { token: resetToken, password: data.get("password") });
    form.reset();
    resetToken = "";
    showView("signin");
    say("Your password is changed. Please sign in.");
  });

  submit("password-form", async (data, form) => {
    await call("/api/account/password", {
      current_password: data.get("current"),
      new_password: data.get("next"),
    });
    form.reset();
    say("Password changed. Other devices were signed out.");
  });

  submit("delete-form", async (data) => {
    await call("/api/account/delete", { password: data.get("password") });
    window.location.assign("/");
  });

  byId("account-history-clear").addEventListener("click", async () => {
    try {
      await call("/api/account/history/clear");
      renderHistory([]);
      say("Your conversation history is cleared.");
    } catch (error) {
      say(error.message, true);
    }
  });

  byId("account-resend").addEventListener("click", async () => {
    try {
      const result = await call("/api/account/resend-verification");
      say(result.status === "sent" ? "Verification email sent." : "Email could not be sent.");
    } catch (error) {
      say(error.message, true);
    }
  });

  byId("account-disconnect").addEventListener("click", async () => {
    try {
      await call("/auth/logout");
      setMode(false);
      await refresh();
      say("Swiggy disconnected. The session at Swiggy was ended too.");
    } catch (error) {
      say(error.message, true);
    }
  });

  async function signOut(url) {
    try {
      await call(url);
      window.location.assign("/");
    } catch (error) {
      say(error.message, true);
    }
  }
  byId("account-signout").addEventListener("click", () => void signOut("/api/account/logout"));
  byId("account-signout-all").addEventListener(
    "click",
    () => void signOut("/api/account/logout-everywhere"),
  );

  // Links in email carry their token in the fragment, which never reaches a server log.
  async function handleFragment() {
    const hash = window.location.hash.slice(1);
    if (!hash) return;
    history.replaceState(null, "", window.location.pathname + window.location.search);
    if (hash === "signin") {
      await openAccount();
    } else if (hash === "connected") {
      await openAccount();
      say("Swiggy is connected. Saathi will read back every order and wait for your yes.");
    } else if (hash.startsWith("verify=")) {
      try {
        await call("/api/account/verify", { token: hash.slice("verify=".length) });
        await openAccount();
        say("Your email is verified.");
      } catch (error) {
        await openAccount();
        say(error.message, true);
      }
    } else if (hash.startsWith("reset=")) {
      resetToken = hash.slice("reset=".length);
      showView("reset");
      dialog.showModal();
    }
  }

  // An email link opened in a tab already on this site changes only the fragment.
  window.addEventListener("hashchange", () => void handleFragment());
  void refresh().then(handleFragment);
})();
