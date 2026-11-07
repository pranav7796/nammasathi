// Reminders and notes. The server builds every widget (GET /api/personal/panel and the `ui` field of
// a reply); this file only draws them with saathiGenUI and turns an action id into a REST call.
// Prices and permissions are never decided here: each call is checked again on the server.
(() => {
  const gui = window.saathiGenUI;
  const dialog = document.querySelector("#personal-dialog");
  const panel = document.querySelector("#personal-widgets");
  const inline = document.querySelector("#genui-inline");
  const button = document.querySelector("#personal-button");
  const errorLine = document.querySelector("#personal-error");
  if (!gui || !dialog || !panel || !inline || !button || !errorLine) return;

  const IST = "+05:30";
  const say = (message) => {
    errorLine.textContent = message || "";
    errorLine.hidden = !message;
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

  // Which REST call an action id means. Anything unknown is ignored.
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
    } else {
      return false;
    }
    return true;
  }

  async function refresh() {
    const { ui } = await call("/api/personal/panel");
    gui.render(panel, ui, { onAction });
    // A reminder list already shown in the conversation follows along, in place.
    const live = ui.filter((widget) => inline.querySelector(`[data-genui-id="${widget.id}"]`));
    if (live.length) gui.render(inline, live, { onAction });
  }

  async function onAction({ actionId, values }) {
    say("");
    try {
      if (await perform(actionId, values || {})) await refresh();
    } catch (error) {
      say(error.message);
      throw error; // lets the widget re-enable its controls
    }
  }

  async function open() {
    say("");
    try {
      await refresh();
    } catch (error) {
      say(error.message);
    }
    if (!dialog.open) dialog.showModal();
  }

  button.addEventListener("click", open);
  document.querySelector("#personal-close")?.addEventListener("click", () => dialog.close());

  // account.js announces who is signed in. The button only makes sense for a signed-in person.
  window.addEventListener("saathi:me", (event) => {
    button.hidden = !(event.detail && event.detail.user);
  });

  // app.js hands over the `ui` of a reply, such as the list after "remind me to ...".
  window.saathiShowWidgets = (ui) => {
    if (Array.isArray(ui) && ui.length) gui.render(inline, ui, { onAction });
  };
})();
