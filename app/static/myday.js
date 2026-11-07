// My Day: the person's own day on one screen, from real connected data only. When a service isn't
// connected, or has stopped accepting access, the card says so and says what to do. It never fills
// a gap with made-up data. Built with textContent only.
(() => {
  const dialog = document.querySelector("#myday-dialog");
  const grid = document.querySelector("#myday-grid");
  const opener = document.querySelector("#personal-button");
  if (!dialog || !grid || !opener) return;
  const SVG = "http://www.w3.org/2000/svg";

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function icon(name) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG, "use");
    use.setAttribute("href", `#icon-${name}`);
    svg.append(use);
    return svg;
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
  const clock = (iso) =>
    new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });

  function section(title, iconName) {
    const root = el("section", "day-card");
    const heading = el("h3");
    heading.append(icon(iconName), document.createTextNode(title));
    root.append(heading);
    return root;
  }

  // A card whose data is missing: the reason, and a way to fix it.
  function gap(root, block) {
    const note = el("p", "day-empty", block.message);
    note.dataset.state = block.state;
    root.append(note);
    if (block.state !== "ok" && block.state !== "unavailable") {
      const fix = el("button", "text-button", "Open Apps");
      fix.type = "button";
      fix.addEventListener("click", () => {
        dialog.close();
        void window.saathiOpenApps?.();
      });
      root.append(fix);
    }
  }

  function calendarCard(block) {
    const root = section("Today's calendar", "calendar");
    if (!block.events.length) return gap(root, block), root;
    const list = el("ul");
    for (const event of block.events) {
      const row = el("li");
      row.dataset.now = String(event.now);
      row.append(el("span", "day-time", event.start), el("span", "", event.title));
      list.append(row);
    }
    root.append(list);
    return root;
  }

  function reminderCard(block, refresh) {
    const root = section("Reminders", "reminder");
    if (!block.items.length) root.append(el("p", "day-empty", block.message));
    else {
      const list = el("ul");
      for (const item of block.items) {
        const row = el("li");
        row.dataset.overdue = String(item.overdue);
        const done = el("input");
        done.type = "checkbox";
        done.setAttribute("aria-label", `Mark done: ${item.text}`);
        done.addEventListener("change", async () => {
          try {
            await call(`/api/personal/reminders/${item.id}/done`, "POST");
            await refresh();
          } catch (error) {
            window.saathiToast?.(error.message, "error");
            done.checked = false;
          }
        });
        const text = el("span", "", item.text);
        if (item.due) {
          const sub = el("span", "day-sub", item.overdue ? `Overdue · ${clock(item.due)}` : item.today ? `Today ${clock(item.due)}` : new Date(item.due).toLocaleDateString(undefined, { day: "numeric", month: "short" }));
          text.append(sub);
        }
        row.append(done, text);
        list.append(row);
      }
      root.append(list);
      if (block.total > block.items.length) {
        root.append(el("p", "day-sub", `${block.total - block.items.length} more in Reminders, notes, contacts.`));
      }
    }
    const form = el("form", "text-form");
    const input = el("input");
    input.type = "text";
    input.maxLength = 300;
    input.placeholder = "Add one, e.g. call Amma tomorrow 7 pm";
    input.setAttribute("aria-label", "Add a reminder");
    const add = el("button", "send-button", "Add");
    add.type = "submit";
    form.append(input, add);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      try {
        await call("/api/personal/reminders", "POST", { text, when_text: text });
        input.value = "";
        window.saathiToast?.("Saved to your reminders.", "success");
        await refresh();
      } catch (error) {
        window.saathiToast?.(error.message, "error");
      }
    });
    root.append(form);
    return root;
  }

  function emailCard(block) {
    const root = section("Important email", "mail");
    if (!block.items.length) return gap(root, block), root;
    const list = el("ul");
    for (const mail of block.items) {
      const row = el("li");
      const text = el("span", "", mail.subject || "(no subject)");
      text.append(el("span", "day-sub", `${mail.sender}${mail.needs_reply ? " · needs a reply" : ""}`));
      row.append(text);
      list.append(row);
    }
    root.append(list);
    return root;
  }

  function waitingCard(items) {
    const root = section("Waiting for you", "check");
    if (!items.length) {
      root.append(el("p", "day-empty", "Nothing is waiting for your approval."));
      return root;
    }
    const list = el("ul");
    for (const item of items) {
      const row = el("li");
      const text = el("span", "", item.title);
      text.append(el("span", "day-sub", item.status === "waiting_for_input" ? "Needs a detail from you" : "Waiting for your approval"));
      row.append(text);
      list.append(row);
    }
    root.append(list);
    root.append(el("p", "day-sub", "Open your conversation to approve or change these."));
    return root;
  }

  const WORDS = { completed: "Done", failed: "Didn't finish", cancelled: "Cancelled" };
  function activityCard(items) {
    const root = section("Recent activity", "list");
    if (!items.length) {
      root.append(el("p", "day-empty", "Nothing yet. Ask Saathi for something."));
      return root;
    }
    const list = el("ul");
    for (const item of items) {
      const row = el("li");
      const text = el("span", "", item.title);
      const note = `${WORDS[item.status] || item.status}${item.demo ? " · demo" : ""}`;
      text.append(el("span", "day-sub", note));
      row.append(text);
      list.append(row);
    }
    root.append(list);
    return root;
  }

  function servicesCard(data) {
    const root = section("Connected services", "apps");
    const wrap = el("div", "day-services");
    for (const [name, block] of [["Gmail", data.emails], ["Calendar", data.calendar]]) {
      const chip = el("span", "status-chip", `${name}: ${block.state === "ok" ? "connected" : block.state.replace("_", " ")}`);
      chip.dataset.state = block.state === "ok" ? "connected" : block.state;
      wrap.append(chip);
    }
    root.append(wrap);
    const manage = el("button", "text-button", "Manage connected apps");
    manage.type = "button";
    manage.addEventListener("click", () => {
      dialog.close();
      void window.saathiOpenApps?.();
    });
    root.append(manage);
    return root;
  }

  async function load() {
    grid.replaceChildren(el("p", "day-empty", "Loading your day…"));
    try {
      const data = await call("/api/myday");
      const date = new Date(`${data.date}T12:00:00`);
      const hour = new Date().getHours();
      const hello = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
      document.querySelector("#myday-title").textContent = hello;
      document.querySelector("#myday-date").textContent = date.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
      grid.replaceChildren(
        calendarCard(data.calendar),
        reminderCard(data.reminders, load),
        emailCard(data.emails),
        waitingCard(data.waiting),
        activityCard(data.activity),
        servicesCard(data),
      );
    } catch (error) {
      grid.replaceChildren(el("p", "day-empty", error.message));
    }
  }

  async function open() {
    if (!dialog.open) dialog.showModal();
    await load();
  }

  opener.addEventListener("click", open);
  document.querySelector("#myday-close")?.addEventListener("click", () => dialog.close());
  document.querySelector("#myday-refresh")?.addEventListener("click", load);
  document.querySelector("#myday-focus")?.addEventListener("click", () => {
    dialog.close();
    void window.saathiSay?.("Saathi, what should I focus on today?");
  });
  document.querySelector("#myday-manage")?.addEventListener("click", () => dialog.close());
  window.saathiOpenMyDay = open;
  window.addEventListener("saathi:me", (event) => {
    opener.hidden = !(event.detail && event.detail.user);
  });
})();
