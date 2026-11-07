// "My day": reminders, notes, memory, contacts, connected apps, and the task board's live updates.
//
// The server builds every widget (GET /api/personal/panel, GET /api/memory/panel, the `ui` of a reply
// and of /api/tasks/*). This file only draws them with saathiGenUI and turns an action id into one
// REST call. Nothing here decides a price, a permission or whether something was sent: the server
// checks every call again, and the board shows only what it records.
(() => {
  const gui = window.saathiGenUI;
  const dialog = document.querySelector("#personal-dialog");
  const panel = document.querySelector("#personal-widgets");
  const inline = document.querySelector("#genui-inline");
  const errorLine = document.querySelector("#personal-error");
  const notice = document.querySelector("#genui-message");
  if (!gui || !dialog || !panel || !inline || !errorLine) return;

  const IST = "+05:30";
  const POLL_MS = 1000;
  const POLL_LIMIT = 150;
  let poller = null;

  const say = (message) => {
    errorLine.textContent = message || "";
    errorLine.hidden = !message;
  };
  const tell = (message) => {
    if (!notice) return;
    notice.textContent = message || "";
    notice.hidden = !message;
  };

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

  function dueFrom(values) {
    const date = values.date || "";
    const time = values.time || "";
    if (!date && !time) return null;
    const day = date || new Date().toLocaleDateString("en-CA");
    return `${day}T${time || "09:00"}:00${IST}`;
  }

  // Personal things (reminders, notes, memory): plain REST calls.
  async function perform(actionId, values) {
    const [action, target] = actionId.split(":");
    if (action === "reminder.create") {
      await call("/api/personal/reminders", "POST", { text: values.text, due: dueFrom(values) });
    } else if (action === "reminder.done") {
      await call(`/api/personal/reminders/${target}/done`, "POST");
    } else if (action === "reminder.undo") {
      await call(`/api/personal/reminders/${target}/undo`, "POST");
    } else if (action === "reminder.delete") {
      await call(`/api/personal/reminders/${target}`, "DELETE");
    } else if (action === "note.create") {
      await call("/api/personal/notes", "POST", { body: values.body });
    } else if (action === "note.delete") {
      await call(`/api/personal/notes/${target}`, "DELETE");
    } else if (action === "memory.forget") {
      await call(`/api/memory/${encodeURIComponent(target)}`, "DELETE");
    } else if (action === "memory.save") {
      await call(`/api/memory/${encodeURIComponent(values.key)}`, "PUT", { value: values.value });
    } else {
      return false;
    }
    return true;
  }

  async function refreshPanel() {
    const { ui } = await call("/api/personal/panel");
    let memory = [];
    try {
      memory = (await call("/api/memory/panel")).ui || [];
    } catch (error) {
      memory = []; // memory is optional; the rest of the panel still works
    }
    gui.render(panel, [...ui, ...memory], { onAction });
    const live = ui.filter((widget) => inline.querySelector(`[data-genui-id="${widget.id}"]`));
    if (live.length) gui.render(inline, live, { onAction });
    await refreshExtras();
  }

  // Task board: render what the server says, and keep asking while anything is still running.
  // Widgets belong to one workflow: starting a different one clears the last one's, so a WhatsApp
  // composer can never linger beside an email list.
  let shownFor = "";
  function show(ui, plan, workflow) {
    const kind = (workflow && workflow.kind) || "";
    const key = (plan && plan.id) || kind;
    if (key && key !== shownFor) {
      gui.clear(inline);
      window.clearInterval(poller);
    }
    if (key) shownFor = key;
    if (!key && !(Array.isArray(ui) && ui.length)) {
      gui.clear(inline); // an order or a fresh start: nothing from another workflow stays
      shownFor = "";
    }
    if (Array.isArray(ui) && ui.length) gui.render(inline, ui, { onAction });
    if (plan && plan.active && plan.id) watch(plan.id);
  }

  function watch(planId) {
    window.clearInterval(poller);
    let polls = 0;
    poller = window.setInterval(async () => {
      polls += 1;
      try {
        const data = await call(`/api/tasks/plans/${planId}`);
        gui.render(inline, data.ui || [], { onAction });
        if (!data.plan || !data.plan.active || polls >= POLL_LIMIT) window.clearInterval(poller);
      } catch (error) {
        window.clearInterval(poller);
      }
    }, POLL_MS);
  }

  async function taskAction(actionId, values) {
    const data = await call("/api/tasks/action", "POST", {
      action_id: actionId,
      values: values || {},
    });
    tell(data.result === "ok" ? "" : data.message || "");
    show(data.ui, data.plan);
    if (data.result !== "ok") throw new Error(data.message || "That did not work.");
  }

  async function onAction({ actionId, values }) {
    say("");
    const verb = actionId.split(":")[0];
    try {
      if (/^(task|plan|email)\./.test(verb)) {
        await taskAction(actionId, values);
      } else if (await perform(actionId, values || {})) {
        await refreshPanel();
      }
    } catch (error) {
      if (dialog.open) say(error.message);
      throw error; // lets the widget re-enable its controls
    }
  }

  // Connected apps, contacts: small plain sections, built with textContent only.
  const connections = document.querySelector("#connections");
  const contacts = document.querySelector("#contacts-list");
  const contactForm = document.querySelector("#contact-form");

  function line(text, className) {
    const p = document.createElement("p");
    p.className = className || "small-note";
    p.textContent = text;
    return p;
  }

  async function refreshExtras() {
    if (connections) {
      connections.replaceChildren();
      try {
        const google = await call("/api/google/status");
        const wa = await call("/api/whatsapp/status").catch(() => ({ configured: false }));
        if (!google.configured) {
          connections.append(
            line("Gmail and Calendar aren't set up on this server yet, so those use clearly labelled demo data.")
          );
        }
        for (const [key, label] of [["gmail", "Gmail"], ["calendar", "Google Calendar"]]) {
          const row = document.createElement("div");
          row.className = "conn-row";
          const info = (google[key] || {});
          const name = document.createElement("span");
          name.textContent = info.connected ? `${label}: connected` : `${label}: not connected (demo)`;
          row.append(name);
          if (google.configured) {
            const act = document.createElement("button");
            act.type = "button";
            act.className = "text-button";
            act.textContent = info.connected ? "Disconnect" : "Connect";
            act.addEventListener("click", async () => {
              try {
                if (info.connected) {
                  await call("/api/google/disconnect", "POST", { service: key });
                  await refreshExtras();
                } else {
                  const { url } = await call(`/api/google/connect?service=${key}`);
                  window.location.href = url;
                }
              } catch (error) {
                say(error.message);
              }
            });
            row.append(act);
          }
          connections.append(row);
        }
        connections.append(
          line(
            wa.configured
              ? "WhatsApp: sends from this site's business number once you approve each message."
              : "WhatsApp: not set up on this server, so messages are demo only."
          )
        );
      } catch (error) {
        connections.append(line("Connections aren't available right now."));
      }
    }
    if (contacts) {
      contacts.replaceChildren();
      try {
        const { contacts: saved } = await call("/api/contacts");
        if (!saved.length) contacts.append(line("No contacts yet. Add the people you message often."));
        for (const person of saved) {
          const row = document.createElement("div");
          row.className = "conn-row";
          const text = document.createElement("span");
          text.textContent = [person.name, person.phone, person.email].filter(Boolean).join(" · ");
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "text-button";
          remove.textContent = "Delete";
          remove.setAttribute("aria-label", `Delete ${person.name}`);
          remove.addEventListener("click", async () => {
            try {
              await call(`/api/contacts/${person.id}`, "DELETE");
              await refreshExtras();
            } catch (error) {
              say(error.message);
            }
          });
          row.append(text, remove);
          contacts.append(row);
        }
      } catch (error) {
        contacts.append(line("Contacts aren't available right now."));
      }
    }
  }

  contactForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(contactForm);
    const body = { name: String(form.get("name") || "").trim() };
    const phone = String(form.get("phone") || "").trim();
    const email = String(form.get("email") || "").trim();
    if (phone) body.phone = phone;
    if (email) body.email = email;
    say("");
    try {
      await call("/api/contacts", "POST", body);
      contactForm.reset();
      await refreshExtras();
    } catch (error) {
      say(error.message);
    }
  });

  async function open() {
    say("");
    try {
      await refreshPanel();
    } catch (error) {
      say(error.message);
    }
    if (!dialog.open) dialog.showModal();
  }

  window.saathiOpenPersonal = open;
  document.querySelector("#myday-manage")?.addEventListener("click", open);
  document.querySelector("#personal-close")?.addEventListener("click", () => dialog.close());

  // account.js announces who is signed in. The button only makes sense for a signed-in person.
  window.addEventListener("saathi:me", (event) => {
    const user = event.detail && event.detail.user;
    if (user) resume();
  });

  // After a reload, a board that was still working keeps updating.
  let resumed = false;
  async function resume() {
    if (resumed) return;
    resumed = true;
    try {
      const data = await call("/api/tasks/current");
      if (data.plan && data.plan.active) show(data.ui, data.plan);
    } catch (error) {
      /* no board to resume */
    }
  }

  // app.js hands over the `ui` and `plan` of a reply.
  window.saathiShowWidgets = (ui, plan, workflow) => show(ui, plan, workflow);

})();
