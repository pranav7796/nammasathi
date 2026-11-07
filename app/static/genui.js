/*
 * Namma Saathi generative UI renderer (browser side).
 * Contract: docs/genui-spec.md. The server builds and validates widgets; this file
 * validates again (defence in depth) and builds plain DOM from the data.
 *
 * Safety rules for this file:
 *  - DOM is built with createElement, textContent and setAttribute only.
 *  - Nothing from a widget becomes markup, an event handler string or an inline style.
 *  - The only href values set from data are the share_panel links, built here from
 *    validated text; the .ics download uses a blob address we create ourselves.
 *  - The browser never computes prices or totals; it shows the strings it was given.
 */
(function (global) {
  "use strict";

  const MAX_TEXT = 2000;
  const MAX_WIDGETS = 12;
  const MAX_DEPTH = 3;
  const MAX_CHILDREN = 40;
  const MAX_ROWS = 50;
  const SHARE_MAX = 1500;
  const ID_RE = /^[a-z0-9_.:-]{1,64}$/;
  const FIELD_RE = /^[a-z][a-z0-9_]{0,31}$/;
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
  const ISO_RE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;
  const ISO_OFFSET_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;
  const STATUSES = ["pending", "running", "waiting_for_input", "waiting_for_approval", "completed", "failed", "cancelled"];
  const FIELD_KINDS = ["text", "textarea", "select", "checkbox", "date", "time", "number"];

  /* ---------------------------------------------------------------- validation */
  // A check returns "" when the value is fine, otherwise a short reason.
  const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  const S = {
    str: (min = 0, max = MAX_TEXT) => ({
      check: (v) => (typeof v !== "string" ? "must be text" : v.length < min || v.length > max ? `length must be ${min}..${max}` : ""),
    }),
    re: (re, max = MAX_TEXT) => ({
      check: (v) => (typeof v === "string" && v.length <= max && re.test(v) ? "" : "has the wrong format"),
    }),
    oneOf: (list) => ({ check: (v) => (list.indexOf(v) !== -1 ? "" : "is not an allowed value") }),
    int: (min, max) => ({ check: (v) => (Number.isInteger(v) && v >= min && v <= max ? "" : `must be a whole number ${min}..${max}`) }),
    num: (min, max) => ({
      check: (v) => (typeof v === "number" && Number.isFinite(v) && (min === undefined || v >= min) && (max === undefined || v <= max) ? "" : "must be a number in range"),
    }),
    bool: () => ({ check: (v) => (typeof v === "boolean" ? "" : "must be true or false") }),
    any: (f) => ({ check: f }),
    opt: (s) => ({ optional: true, check: (v, d) => (v === undefined || v === null ? "" : s.check(v, d)) }),
    arr: (item, min, max) => ({
      check: (v, d) => {
        if (!Array.isArray(v)) return "must be a list";
        if (v.length < min || v.length > max) return `needs ${min}..${max} entries`;
        for (let i = 0; i < v.length; i += 1) {
          const e = item.check(v[i], d);
          if (e) return `[${i}] ${e}`;
        }
        return "";
      },
    }),
    obj: (shape) => ({
      check: (v, d) => {
        if (!v || typeof v !== "object" || Array.isArray(v)) return "must be an object";
        for (const k of Object.keys(v)) if (!own(shape, k)) return `unknown property "${k}"`;
        for (const k of Object.keys(shape)) {
          if (!own(v, k)) {
            if (shape[k].optional) continue;
            return `missing "${k}"`;
          }
          const e = shape[k].check(v[k], d);
          if (e) return `${k}: ${e}`;
        }
        return "";
      },
    }),
  };
  // A list that may be omitted and defaults to empty.
  const list = (item, max) => S.opt(S.arr(item, 0, max));

  const action = S.obj({
    label: S.str(1, 60),
    action_id: S.str(1, 200),
    style: S.opt(S.oneOf(["primary", "secondary", "danger"])),
    disabled: S.opt(S.bool()),
  });
  const field = S.obj({
    name: S.re(FIELD_RE),
    label: S.str(1, 200),
    kind: S.oneOf(FIELD_KINDS),
    required: S.opt(S.bool()),
    value: S.opt(S.any((v) => (["string", "number", "boolean"].indexOf(typeof v) !== -1 ? "" : "bad value"))),
    options: S.opt(S.arr(S.obj({ value: S.str(0, 200), label: S.str(1, 200) }), 1, 30)),
    min: S.opt(S.num()),
    max: S.opt(S.num()),
    max_length: S.opt(S.int(1, MAX_TEXT)),
    help: S.opt(S.str(0, 500)),
  });
  const fieldChecked = S.any((v, d) => {
    const e = field.check(v, d);
    if (e) return e;
    if (v.kind === "select" && !v.options) return "select needs options";
    if (v.kind !== "select" && v.options) return "only select takes options";
    return "";
  });
  const sub = S.str(1, 100);
  const dateKey = S.any((v) => {
    if (typeof v !== "string" || !DATE_RE.test(v)) return "must be YYYY-MM-DD";
    const [y, m, d] = v.split("-").map(Number);
    const t = new Date(Date.UTC(y, m - 1, d));
    return t.getUTCFullYear() === y && t.getUTCMonth() === m - 1 && t.getUTCDate() === d ? "" : "not a real date";
  });
  const money = S.str(1, 32);

  const SCHEMAS = {
    heading: { text: S.str(0), level: S.opt(S.int(1, 3)) },
    text: { text: S.str(0), tone: S.opt(S.oneOf(["normal", "muted", "warning"])) },
    card: {
      title: S.str(0),
      body: S.opt(S.str(0)),
      children: S.opt({
        check: (v, depth) => {
          if (!Array.isArray(v)) return "must be a list";
          if (v.length > MAX_CHILDREN) return `at most ${MAX_CHILDREN} children`;
          for (let i = 0; i < v.length; i += 1) {
            const e = checkWidget(v[i], depth + 1);
            if (e) return `children[${i}] ${e}`;
          }
          return "";
        },
      }),
      actions: list(action, 8),
    },
    list: {
      title: S.opt(S.str(0)),
      items: S.arr(S.obj({ id: sub, title: S.str(0), detail: S.opt(S.str(0)), badge: S.opt(S.str(0, 100)) }), 0, 100),
      empty_text: S.opt(S.str(0)),
    },
    action_panel: { title: S.str(0), actions: S.arr(action, 1, 8) },
    form: {
      title: S.str(0),
      fields: S.any((v, d) => {
        const e = S.arr(fieldChecked, 1, 12).check(v, d);
        if (e) return e;
        const seen = {};
        for (const f of v) {
          if (own(seen, f.name)) return `duplicate field name "${f.name}"`;
          seen[f.name] = true;
        }
        return "";
      }),
      submit: action,
    },
    task_board: {
      title: S.str(0),
      tasks: S.arr(
        S.obj({
          id: sub, title: S.str(0), status: S.oneOf(STATUSES), detail: S.opt(S.str(0)),
          retry_action_id: S.opt(S.str(1, 200)), cancel_action_id: S.opt(S.str(1, 200)),
        }),
        0, 100,
      ),
    },
    progress: { label: S.str(0), value: S.num(0, 100) },
    cart: {
      title: S.str(0),
      lines: S.arr(S.obj({ id: sub, name: S.str(0, 300), quantity: S.int(0, 99), unit_price_inr: money, detail: S.opt(S.str(0)) }), 0, 100),
      total_inr: money,
      fees_inr: S.opt(money),
      editable: S.opt(S.bool()),
      demo: S.bool(),
    },
    table: {
      title: S.opt(S.str(0)),
      columns: S.arr(S.str(0, 200), 1, 8),
      rows: S.any((v, d) => {
        const e = S.arr(S.arr(S.str(0, 500), 0, 8), 0, MAX_ROWS).check(v, d);
        return e;
      }),
    },
    confirmation: {
      title: S.str(0),
      details: S.arr(S.obj({ label: S.str(0, 200), value: S.str(0, 500) }), 0, 40),
      consequence: S.str(0),
      confirm: action,
      reject: action,
      demo: S.bool(),
    },
    composer: {
      channel: S.oneOf(["email", "message"]),
      to: S.opt(S.str(0, 500)),
      subject: S.opt(S.str(0, 500)),
      body: S.str(0),
      send: action,
      copy_enabled: S.opt(S.bool()),
    },
    calendar_view: {
      title: S.str(0),
      days: S.arr(
        S.obj({
          date: dateKey,
          events: S.arr(S.obj({ id: sub, title: S.str(0, 300), start: S.re(TIME_RE), end: S.re(TIME_RE), detail: S.opt(S.str(0)) }), 0, 100),
        }),
        0, 31,
      ),
    },
    reminder_list: {
      title: S.str(0),
      items: S.arr(S.obj({ id: sub, text: S.str(0, 500), due: S.opt(S.re(ISO_RE, 40)), done: S.bool() }), 0, 100),
    },
    note_list: {
      title: S.str(0),
      items: S.arr(S.obj({ id: sub, body: S.str(0), updated: S.re(ISO_RE, 40) }), 0, 100),
    },
    share_panel: {
      title: S.str(0),
      summary: S.str(1),
      calendar: S.opt(
        S.obj({
          title: S.str(1, 300), start: S.re(ISO_OFFSET_RE, 40), end: S.re(ISO_OFFSET_RE, 40),
          location: S.opt(S.str(0, 500)), description: S.opt(S.str(0)),
        }),
      ),
    },
    status: {
      kind: S.oneOf(["loading", "success", "empty", "error"]),
      message: S.str(0),
      retry: S.opt(action),
    },
  };

  function checkWidget(w, depth) {
    if (!w || typeof w !== "object" || Array.isArray(w)) return "widget must be an object";
    if (typeof w.type !== "string" || !own(SCHEMAS, w.type)) return "unknown type";
    if (typeof w.id !== "string" || !ID_RE.test(w.id)) return "bad id";
    if (depth > MAX_DEPTH) return `nesting deeper than ${MAX_DEPTH}`;
    const shape = Object.assign({ id: S.re(ID_RE), type: S.str(1, 40) }, SCHEMAS[w.type]);
    return S.obj(shape).check(w, depth);
  }

  function validate(widget) {
    try {
      const error = checkWidget(widget, 1);
      return { ok: !error, error: error || null };
    } catch (e) {
      return { ok: false, error: "validation failed" };
    }
  }

  /* ------------------------------------------------------------------ helpers */
  let uidCounter = 0;
  const uid = (p) => `${p}-${(uidCounter += 1)}`;

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function attrs(n, map) {
    for (const k of Object.keys(map)) n.setAttribute(k, String(map[k]));
    return n;
  }
  function add(parent, ...kids) {
    for (const k of kids) if (k) parent.appendChild(k);
    return parent;
  }
  const timer = (fn, ms) => {
    const t = setTimeout(fn, ms);
    if (t && typeof t.unref === "function") t.unref();
    return t;
  };
  const isThenable = (r) => !!r && typeof r.then === "function";

  function fmtDateTime(iso) {
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return iso;
      return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    } catch (e) {
      return iso;
    }
  }
  function fmtDay(date) {
    try {
      const [y, m, d] = date.split("-").map(Number);
      return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
    } catch (e) {
      return date;
    }
  }

  // Buttons: control key lets us restore focus after a widget is replaced.
  function button(label, cls, key) {
    const b = h("button", `genui-btn ${cls || ""}`.trim(), label);
    b.setAttribute("type", "button");
    if (key) b.setAttribute("data-key", key);
    return b;
  }
  function actionButton(a, wid, ctx, extra) {
    const { style: variant } = a;
    const b = button(a.label, `genui-btn-${variant || "secondary"}`, `act:${a.action_id}`);
    if (a.disabled) b.disabled = true;
    b.addEventListener("click", () => {
      if (b.disabled) return;
      ctx.act(wid, a.action_id, extra ? extra() : {});
    });
    return b;
  }
  function badge(text, cls) {
    return h("span", `genui-badge ${cls || ""}`.trim(), text);
  }
  function titleEl(text, tag, cls) {
    return text ? h(tag || "h3", cls || "genui-title", text) : null;
  }

  /* ---------------------------------------------------------------- components */
  const BUILDERS = {};

  BUILDERS.heading = (w) => {
    const level = w.level || 2;
    return h(`h${level + 1}`, `genui-heading genui-h${level}`, w.text);
  };

  BUILDERS.text = (w) => h("p", `genui-text genui-tone-${w.tone || "normal"}`, w.text);

  BUILDERS.card = (w, ctx) => {
    const root = h("div", "genui-card-body");
    add(root, titleEl(w.title));
    if (w.body) add(root, h("p", "genui-text", w.body));
    if (w.children && w.children.length) {
      const kids = h("div", "genui-children");
      for (const c of w.children) add(kids, buildWidget(c, ctx));
      add(root, kids);
    }
    if (w.actions && w.actions.length) {
      const bar = h("div", "genui-actions");
      for (const a of w.actions) add(bar, actionButton(a, w.id, ctx));
      add(root, bar);
    }
    return root;
  };

  BUILDERS.list = (w) => {
    const root = h("div");
    add(root, titleEl(w.title));
    if (!w.items.length) {
      add(root, h("p", "genui-empty", w.empty_text || "Nothing here yet."));
      return root;
    }
    const ul = h("ul", "genui-list");
    for (const it of w.items) {
      const li = h("li", "genui-list-item");
      const main = h("div", "genui-grow");
      add(main, h("span", "genui-item-title", it.title));
      if (it.detail) add(main, h("span", "genui-item-detail", it.detail));
      add(li, main, it.badge ? badge(it.badge) : null);
      add(ul, li);
    }
    return add(root, ul);
  };

  BUILDERS.action_panel = (w, ctx) => {
    const root = h("div");
    add(root, titleEl(w.title));
    const bar = h("div", "genui-actions");
    for (const a of w.actions) add(bar, actionButton(a, w.id, ctx));
    return add(root, bar);
  };

  /* ---- form */
  function buildField(f, ctx, wid) {
    const id = uid("genui-f");
    const key = `${wid}/${f.name}`;
    const wrap = h("div", `genui-field genui-field-${f.kind}`);
    const label = h("label", "genui-label");
    label.setAttribute("for", id);
    label.textContent = f.label;
    if (f.required) add(label, h("span", "genui-req", " (required)"));
    const err = h("p", "genui-error", "");
    err.setAttribute("id", `${id}-err`);
    err.setAttribute("role", "alert");
    err.hidden = true;

    let input;
    const initial = f.value === undefined || f.value === null ? "" : f.value;
    if (f.kind === "textarea") {
      input = h("textarea", "genui-input");
      input.setAttribute("rows", "4");
      input.value = String(initial);
    } else if (f.kind === "select") {
      input = h("select", "genui-input");
      const blank = h("option", "", "Choose...");
      blank.setAttribute("value", "");
      blank.value = "";
      add(input, blank);
      for (const o of f.options) {
        const op = h("option", "", o.label);
        op.setAttribute("value", o.value);
        op.value = o.value;
        add(input, op);
      }
      input.value = String(initial);
    } else {
      input = h("input", "genui-input");
      input.setAttribute("type", f.kind);
      if (f.kind === "checkbox") input.checked = initial === true;
      else input.value = String(initial);
      if (f.kind === "number") {
        input.setAttribute("step", "any");
        if (typeof f.min === "number") input.setAttribute("min", f.min);
        if (typeof f.max === "number") input.setAttribute("max", f.max);
      }
    }
    const maxLength = f.max_length || 500;
    if (f.kind === "text" || f.kind === "textarea") input.setAttribute("maxlength", maxLength);
    attrs(input, { id, name: f.name, "data-key": key });
    if (f.required) input.setAttribute("aria-required", "true");
    const described = [`${id}-err`];
    let helpEl = null;
    if (f.help) {
      helpEl = h("p", "genui-help", f.help);
      helpEl.setAttribute("id", `${id}-help`);
      described.unshift(`${id}-help`);
    }
    input.setAttribute("aria-describedby", described.join(" "));

    const read = () => {
      if (f.kind === "checkbox") return input.checked === true;
      if (f.kind === "number") {
        const raw = String(input.value).trim();
        return raw === "" ? null : Number(raw);
      }
      return input.value;
    };
    // Keep what the person typed when the server replaces this widget.
    const prev = ctx.top.prev[key];
    if (prev && prev.kind === f.kind) {
      if (f.kind === "checkbox") input.checked = prev.value === true;
      else input.value = String(prev.value);
    }
    const remember = () => { ctx.top.dirty[key] = { kind: f.kind, value: read() }; };
    if (prev) ctx.top.dirty[key] = prev;
    input.addEventListener("input", remember);
    input.addEventListener("change", remember);

    const problem = () => {
      const v = read();
      if (f.required) {
        if (f.kind === "checkbox" && v !== true) return "This must be ticked.";
        if (f.kind !== "checkbox" && (v === null || (typeof v === "string" && v.trim() === ""))) return "This is required.";
      }
      if (f.kind === "number" && v !== null) {
        if (Number.isNaN(v) || !Number.isFinite(v)) return "Enter a number.";
        if (typeof f.min === "number" && v < f.min) return `Must be at least ${f.min}.`;
        if (typeof f.max === "number" && v > f.max) return `Must be at most ${f.max}.`;
      }
      if ((f.kind === "text" || f.kind === "textarea") && typeof v === "string" && v.length > maxLength) {
        return `Use at most ${maxLength} characters.`;
      }
      if (f.kind === "select" && v !== "" && !f.options.some((o) => o.value === v)) return "Choose one of the options.";
      return "";
    };
    const show = (msg) => {
      err.textContent = msg;
      err.hidden = !msg;
      if (msg) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    };

    if (f.kind === "checkbox") {
      const row = h("div", "genui-check-row");
      add(row, input, label);
      add(wrap, row, helpEl, err);
    } else {
      add(wrap, label, input, helpEl, err);
    }
    return { wrap, input, read, problem, show, name: f.name };
  }

  BUILDERS.form = (w, ctx) => {
    const root = h("form", "genui-form-el");
    root.setAttribute("novalidate", "novalidate");
    add(root, titleEl(w.title));
    const fields = w.fields.map((f) => buildField(f, ctx, w.id));
    for (const f of fields) add(root, f.wrap);
    const { style: variant } = w.submit;
    const submit = button(w.submit.label, `genui-btn-${variant || "primary"}`, "submit");
    submit.setAttribute("type", "submit");
    if (w.submit.disabled) submit.disabled = true;
    const status = h("p", "genui-help", "");
    status.setAttribute("aria-live", "polite");
    add(root, add(h("div", "genui-actions"), submit), status);
    let busy = false;
    root.addEventListener("submit", (ev) => {
      if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
      if (busy || submit.disabled) return;
      let firstBad = null;
      const values = {};
      for (const f of fields) {
        const msg = f.problem();
        f.show(msg);
        if (msg && !firstBad) firstBad = f;
        values[f.name] = f.read();
      }
      if (firstBad) {
        status.textContent = "Please fix the highlighted fields.";
        if (typeof firstBad.input.focus === "function") firstBad.input.focus();
        return;
      }
      status.textContent = "";
      busy = true;
      submit.disabled = true;
      submit.setAttribute("aria-busy", "true");
      const release = () => { busy = false; submit.disabled = !!w.submit.disabled; submit.removeAttribute("aria-busy"); };
      const r = ctx.act(w.id, w.submit.action_id, values);
      if (isThenable(r)) r.then(release, release);
      else timer(release, 800);
    });
    return root;
  };

  /* ---- task board */
  const STATUS_VIEW = {
    pending: ["○", "Pending"],
    running: ["↻", "Running"],
    waiting_for_input: ["?", "Waiting for you"],
    waiting_for_approval: ["⏸", "Needs your approval"],
    completed: ["✓", "Done"],
    failed: ["✕", "Failed"],
    cancelled: ["⊘", "Cancelled"],
  };
  const CANCELLABLE = ["pending", "running", "waiting_for_input", "waiting_for_approval"];

  BUILDERS.task_board = (w, ctx) => {
    const root = h("div");
    root.setAttribute("aria-live", "polite");
    add(root, titleEl(w.title));
    if (!w.tasks.length) add(root, h("p", "genui-empty", "No tasks yet."));
    const ul = h("ul", "genui-list genui-tasks");
    for (const t of w.tasks) {
      const li = h("li", `genui-list-item genui-task genui-status-${t.status}`);
      const main = h("div", "genui-grow");
      const chip = h("span", `genui-chip genui-chip-${t.status}`);
      add(chip, h("span", "genui-chip-icon", STATUS_VIEW[t.status][0]), h("span", "", STATUS_VIEW[t.status][1]));
      chip.firstChild.setAttribute("aria-hidden", "true");
      add(main, h("span", "genui-item-title", t.title), chip);
      if (t.detail) add(main, h("span", "genui-item-detail", t.detail));
      add(li, main);
      const bar = h("div", "genui-actions genui-actions-tight");
      if (t.retry_action_id && t.status === "failed") {
        const b = button("Retry", "genui-btn-secondary", `retry:${t.id}`);
        b.setAttribute("aria-label", `Retry: ${t.title}`);
        b.addEventListener("click", () => ctx.act(w.id, t.retry_action_id, { task_id: t.id }));
        add(bar, b);
      }
      if (t.cancel_action_id && CANCELLABLE.indexOf(t.status) !== -1) {
        const b = button("Cancel", "genui-btn-danger", `cancel:${t.id}`);
        b.setAttribute("aria-label", `Cancel: ${t.title}`);
        b.addEventListener("click", () => ctx.act(w.id, t.cancel_action_id, { task_id: t.id }));
        add(bar, b);
      }
      if (bar.children.length) add(li, bar);
      add(ul, li);
    }
    return add(root, ul);
  };

  BUILDERS.progress = (w) => {
    const root = h("div");
    const label = h("p", "genui-progress-label");
    const id = uid("genui-p");
    label.setAttribute("id", id);
    add(label, h("span", "", w.label), h("span", "genui-progress-pct", `${Math.round(w.value)}%`));
    const bar = h("progress", "genui-progress");
    attrs(bar, { role: "progressbar", max: 100, value: w.value, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": w.value, "aria-labelledby": id });
    return add(root, label, bar);
  };

  /* ---- cart */
  BUILDERS.cart = (w, ctx) => {
    const root = h("div");
    const head = h("div", "genui-row");
    add(head, titleEl(w.title, "h3", "genui-title genui-grow"));
    if (w.demo) add(head, badge("Demo: nothing real is ordered", "genui-badge-demo"));
    add(root, head);
    if (!w.lines.length) add(root, h("p", "genui-empty", "Your basket is empty."));
    const ul = h("ul", "genui-list genui-cart-lines");
    for (const line of w.lines) {
      const li = h("li", "genui-list-item genui-cart-line");
      const main = h("div", "genui-grow");
      add(main, h("span", "genui-item-title", line.name));
      if (line.detail) add(main, h("span", "genui-item-detail", line.detail));
      add(main, h("span", "genui-item-detail", `₹${line.unit_price_inr} each`));
      add(li, main);
      if (w.editable) {
        const group = h("div", "genui-qty");
        attrs(group, { role: "group", "aria-label": `Quantity of ${line.name}` });
        const minus = button("−", "genui-btn-secondary genui-qty-btn", `minus:${line.id}`);
        minus.setAttribute("aria-label", `Decrease quantity of ${line.name}`);
        const plus = button("+", "genui-btn-secondary genui-qty-btn", `plus:${line.id}`);
        plus.setAttribute("aria-label", `Increase quantity of ${line.name}`);
        const input = h("input", "genui-input genui-qty-input");
        attrs(input, { type: "number", min: 0, max: 99, step: 1, inputmode: "numeric", name: `qty_${line.id}`, "data-key": `qty:${line.id}`, "aria-label": `Quantity of ${line.name}` });
        input.value = String(line.quantity);
        const err = h("p", "genui-error", "");
        err.setAttribute("role", "alert");
        err.hidden = true;
        const sync = (q) => { minus.disabled = q <= 0; plus.disabled = q >= 99; };
        sync(line.quantity);
        const send = (q) => {
          if (!Number.isInteger(q) || q < 0 || q > 99) {
            err.textContent = "Enter a whole number from 0 to 99.";
            err.hidden = false;
            input.setAttribute("aria-invalid", "true");
            return;
          }
          err.hidden = true;
          input.removeAttribute("aria-invalid");
          input.value = String(q);
          sync(q);
          ctx.act(w.id, "cart.set_quantity", { line_id: line.id, quantity: q });
        };
        const current = () => {
          const raw = String(input.value).trim();
          return raw === "" ? NaN : Number(raw);
        };
        minus.addEventListener("click", () => { const c = current(); send((Number.isInteger(c) ? c : line.quantity) - 1); });
        plus.addEventListener("click", () => { const c = current(); send((Number.isInteger(c) ? c : line.quantity) + 1); });
        input.addEventListener("change", () => send(current()));
        add(group, minus, input, plus);
        add(li, group, err);
      } else {
        add(li, h("span", "genui-qty-static", `Qty ${line.quantity}`));
      }
      add(ul, li);
    }
    add(root, ul);
    const dl = h("dl", "genui-totals");
    if (w.fees_inr !== undefined && w.fees_inr !== null) {
      add(dl, h("dt", "", "Fees"), h("dd", "", `₹${w.fees_inr}`));
    }
    add(dl, h("dt", "genui-total-label", "Total"), h("dd", "genui-total", `₹${w.total_inr}`));
    return add(root, dl);
  };

  /* ---- table */
  BUILDERS.table = (w) => {
    const root = h("div");
    add(root, titleEl(w.title));
    const scroller = h("div", "genui-table-wrap");
    const table = h("table", "genui-table");
    const thead = h("thead");
    const hr = h("tr");
    for (const c of w.columns) {
      const th = h("th", "", c);
      th.setAttribute("scope", "col");
      add(hr, th);
    }
    add(thead, hr);
    const tbody = h("tbody");
    for (const row of w.rows) {
      const tr = h("tr");
      for (let i = 0; i < w.columns.length; i += 1) add(tr, h("td", "", row[i] === undefined ? "" : row[i]));
      add(tbody, tr);
    }
    add(table, thead, tbody);
    return add(root, add(scroller, table));
  };

  /* ---- confirmation */
  BUILDERS.confirmation = (w, ctx) => {
    const root = h("div");
    const head = h("div", "genui-row");
    add(head, titleEl(w.title, "h3", "genui-title genui-grow"));
    if (w.demo) add(head, badge("Demo: nothing real happens", "genui-badge-demo"));
    add(root, head);
    if (w.details.length) {
      const dl = h("dl", "genui-details");
      for (const d of w.details) add(dl, h("dt", "", d.label), h("dd", "", d.value));
      add(root, dl);
    }
    add(root, h("p", "genui-consequence", w.consequence));
    const bar = h("div", "genui-actions");
    let fired = false;
    let confirmBtn;
    let rejectBtn;
    // One click only: both buttons rest until the widget is replaced.
    const once = (a) => {
      const { style: variant } = a;
      const btn = button(a.label, `genui-btn-${variant || "secondary"}`, `act:${a.action_id}`);
      if (a.disabled) btn.disabled = true;
      btn.addEventListener("click", () => {
        if (fired || btn.disabled) return;
        fired = true;
        confirmBtn.disabled = true;
        rejectBtn.disabled = true;
        root.setAttribute("aria-busy", "true");
        const r = ctx.act(w.id, a.action_id, {});
        if (isThenable(r)) {
          r.then(null, () => {
            fired = false;
            confirmBtn.disabled = !!w.confirm.disabled;
            rejectBtn.disabled = !!w.reject.disabled;
            root.removeAttribute("aria-busy");
          });
        }
      });
      return btn;
    };
    confirmBtn = once(w.confirm);
    rejectBtn = once(w.reject);
    add(bar, confirmBtn, rejectBtn);
    return add(root, bar);
  };

  /* ---- composer */
  BUILDERS.composer = (w, ctx) => {
    const root = h("div");
    add(root, h("h3", "genui-title", w.channel === "email" ? "Draft email" : "Draft message"));
    const inputs = {};
    const mk = (name, labelText, tag) => {
      const id = uid("genui-c");
      const key = `${w.id}/${name}`;
      const wrap = h("div", "genui-field");
      const label = h("label", "genui-label", labelText);
      label.setAttribute("for", id);
      const input = h(tag, "genui-input");
      attrs(input, { id, name, "data-key": key });
      if (tag === "textarea") input.setAttribute("rows", "8");
      else input.setAttribute("type", "text");
      input.setAttribute("maxlength", MAX_TEXT);
      return { wrap: add(wrap, label, input), input, key };
    };
    const parts = [];
    if (w.to !== undefined && w.to !== null) parts.push(["to", "To", "input", w.to]);
    if (w.subject !== undefined && w.subject !== null) parts.push(["subject", "Subject", "input", w.subject]);
    parts.push(["body", "Message", "textarea", w.body]);
    for (const [name, label, tag, value] of parts) {
      const f = mk(name, label, tag);
      f.input.value = value;
      const prev = ctx.top.prev[f.key];
      if (prev) { f.input.value = String(prev.value); ctx.top.dirty[f.key] = prev; }
      const remember = () => { ctx.top.dirty[f.key] = { kind: "text", value: f.input.value }; };
      f.input.addEventListener("input", remember);
      inputs[name] = f.input;
      add(root, f.wrap);
    }
    const values = () => {
      const out = {};
      for (const k of Object.keys(inputs)) out[k] = inputs[k].value;
      return out;
    };
    const note = h("p", "genui-help", "Nothing is sent by Namma Saathi. Check the draft, edit it, then choose.");
    const msg = h("p", "genui-help", "");
    msg.setAttribute("aria-live", "polite");
    const bar = h("div", "genui-actions");
    const send = actionButton(w.send, w.id, ctx, values);
    send.removeAttribute("data-key");
    send.setAttribute("data-key", "send");
    add(bar, send);
    if (w.copy_enabled) {
      const copy = button("Copy", "genui-btn-secondary", "copy");
      copy.addEventListener("click", () => copyText(composeText(w.channel, values()), msg));
      add(bar, copy);
    }
    return add(root, note, bar, msg);
  };
  function composeText(channel, v) {
    const lines = [];
    if (channel === "email") {
      if (v.to) lines.push(`To: ${v.to}`);
      if (v.subject) lines.push(`Subject: ${v.subject}`);
      if (lines.length) lines.push("");
    }
    lines.push(v.body);
    return lines.join("\n");
  }
  function copyText(text, msgEl) {
    const fail = () => { msgEl.textContent = "Could not copy automatically. Select the text and copy it by hand."; };
    try {
      const clip = global.navigator && global.navigator.clipboard;
      if (!clip || typeof clip.writeText !== "function") return fail();
      const p = clip.writeText(text);
      if (isThenable(p)) p.then(() => { msgEl.textContent = "Copied."; }, fail);
      else msgEl.textContent = "Copied.";
    } catch (e) {
      fail();
    }
    return undefined;
  }

  /* ---- calendar view */
  BUILDERS.calendar_view = (w) => {
    const root = h("div");
    add(root, titleEl(w.title));
    if (!w.days.length) add(root, h("p", "genui-empty", "Nothing scheduled."));
    const grid = h("div", "genui-cal");
    for (const day of w.days) {
      const col = h("section", "genui-cal-day");
      add(col, h("h4", "genui-cal-date", fmtDay(day.date)));
      if (!day.events.length) add(col, h("p", "genui-empty", "No events"));
      const ul = h("ul", "genui-cal-events");
      for (const ev of day.events) {
        const li = h("li", "genui-event");
        add(li, h("span", "genui-event-time", `${ev.start}–${ev.end}`), h("span", "genui-item-title", ev.title));
        if (ev.detail) add(li, h("span", "genui-item-detail", ev.detail));
        add(ul, li);
      }
      add(col, ul);
      add(grid, col);
    }
    return add(root, grid);
  };

  /* ---- reminders and notes */
  BUILDERS.reminder_list = (w, ctx) => {
    const root = h("div");
    add(root, titleEl(w.title));
    if (!w.items.length) {
      add(root, h("p", "genui-empty", "No reminders."));
      return root;
    }
    const ul = h("ul", "genui-list");
    const now = Date.now();
    for (const it of w.items) {
      const due = it.due ? Date.parse(it.due) : NaN;
      const overdue = !it.done && !Number.isNaN(due) && due < now;
      const li = h("li", `genui-list-item genui-reminder${it.done ? " genui-done" : ""}${overdue ? " genui-overdue" : ""}`);
      const id = uid("genui-r");
      const cb = h("input", "genui-checkbox");
      attrs(cb, { type: "checkbox", id, name: `done_${it.id}`, "data-key": `done:${it.id}` });
      cb.checked = it.done;
      cb.addEventListener("change", () => ctx.act(w.id, `${cb.checked ? "reminder.done" : "reminder.undo"}:${it.id}`, {}));
      const label = h("label", "genui-grow");
      label.setAttribute("for", id);
      add(label, h("span", "genui-item-title", it.text));
      if (it.due) {
        const dueLine = h("span", "genui-item-detail", `Due ${fmtDateTime(it.due)}`);
        if (overdue) add(dueLine, h("span", "genui-overdue-tag", " – Overdue"));
        add(label, dueLine);
      }
      const del = button("Delete", "genui-btn-danger", `del:${it.id}`);
      del.setAttribute("aria-label", `Delete reminder: ${it.text}`);
      del.addEventListener("click", () => ctx.act(w.id, `reminder.delete:${it.id}`, {}));
      add(li, cb, label, del);
      add(ul, li);
    }
    return add(root, ul);
  };

  BUILDERS.note_list = (w, ctx) => {
    const root = h("div");
    add(root, titleEl(w.title));
    if (!w.items.length) {
      add(root, h("p", "genui-empty", "No notes."));
      return root;
    }
    const ul = h("ul", "genui-list");
    for (const it of w.items) {
      const li = h("li", "genui-list-item genui-note");
      const main = h("div", "genui-grow");
      add(main, h("span", "genui-note-body", it.body), h("span", "genui-item-detail", `Updated ${fmtDateTime(it.updated)}`));
      const del = button("Delete", "genui-btn-danger", `del:${it.id}`);
      del.setAttribute("aria-label", "Delete this note");
      del.addEventListener("click", () => ctx.act(w.id, `note.delete:${it.id}`, {}));
      add(li, main, del);
      add(ul, li);
    }
    return add(root, ul);
  };

  /* ---- status */
  BUILDERS.status = (w, ctx) => {
    const root = h("div", `genui-status-box genui-status-${w.kind}`);
    root.setAttribute("aria-live", "polite");
    const icon = h("span", `genui-status-icon${w.kind === "loading" ? " genui-spinner" : ""}`, { loading: "", success: "✓", empty: "–", error: "!" }[w.kind]);
    icon.setAttribute("aria-hidden", "true");
    const textWrap = h("div", "genui-grow");
    add(textWrap, h("span", "genui-sr-kind", { loading: "Working: ", success: "Done: ", empty: "Empty: ", error: "Problem: " }[w.kind]), h("span", "", w.message));
    add(root, icon, textWrap);
    if (w.kind === "loading") root.setAttribute("aria-busy", "true");
    if (w.kind === "error" && w.retry) add(root, actionButton(w.retry, w.id, ctx));
    return root;
  };

  /* ---- share panel: iCalendar text built locally */
  function icsEscape(s) {
    return String(s)
      // eslint-disable-next-line no-control-regex
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
      .replace(/\\/g, "\\\\")
      .replace(/;/g, "\\;")
      .replace(/,/g, "\\,")
      .replace(/\r\n|\r|\n/g, "\\n");
  }
  function icsFold(line) {
    const enc = new TextEncoder();
    const out = [];
    let cur = "";
    let bytes = 0;
    for (const ch of line) {
      const b = enc.encode(ch).length;
      if (bytes + b > 75) {
        out.push(cur);
        cur = " ";
        bytes = 1;
      }
      cur += ch;
      bytes += b;
    }
    out.push(cur);
    return out.join("\r\n");
  }
  function icsStamp(date) {
    return date.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  }
  function makeUid() {
    const c = global.crypto;
    const base = c && typeof c.randomUUID === "function" ? c.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    return `${base}@namma-saathi`;
  }
  function buildIcs(cal) {
    const start = new Date(cal.start);
    const end = new Date(cal.end);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return { error: "That date and time could not be read." };
    if (end.getTime() <= start.getTime()) return { error: "The event ends before it starts." };
    const lines = [
      "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Namma Saathi//Generative UI//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
      "BEGIN:VEVENT", `UID:${makeUid()}`, `DTSTAMP:${icsStamp(new Date())}`,
      `DTSTART:${icsStamp(start)}`, `DTEND:${icsStamp(end)}`, `SUMMARY:${icsEscape(cal.title)}`,
    ];
    if (cal.location) lines.push(`LOCATION:${icsEscape(cal.location)}`);
    if (cal.description) lines.push(`DESCRIPTION:${icsEscape(cal.description)}`);
    lines.push("END:VEVENT", "END:VCALENDAR");
    return { text: `${lines.map(icsFold).join("\r\n")}\r\n` };
  }
  function downloadText(text, filename, type) {
    const blob = new Blob([text], { type });
    const url = global.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.setAttribute("href", url);
    a.setAttribute("download", filename);
    a.setAttribute("rel", "noopener");
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    timer(() => global.URL.revokeObjectURL(url), 4000);
  }

  BUILDERS.share_panel = (w) => {
    const root = h("div");
    add(root, titleEl(w.title));
    const capped = w.summary.length > SHARE_MAX ? `${w.summary.slice(0, SHARE_MAX - 1)}…` : w.summary;
    add(root, h("p", "genui-share-text", capped));
    const msg = h("p", "genui-help", "");
    msg.setAttribute("aria-live", "polite");
    const bar = h("div", "genui-actions");
    if (w.calendar) {
      const b = button("Add to calendar", "genui-btn-primary", "ics");
      b.addEventListener("click", () => {
        const r = buildIcs(w.calendar);
        if (r.error) { msg.textContent = r.error; return; }
        try {
          downloadText(r.text, "namma-saathi-event.ics", "text/calendar;charset=utf-8");
          msg.textContent = "Calendar file created on this device.";
        } catch (e) {
          msg.textContent = "Could not create the calendar file.";
        }
      });
      add(bar, b);
    }
    const link = (label, href, newTab) => {
      const a = h("a", "genui-btn genui-btn-secondary genui-link", label);
      a.setAttribute("href", href);
      a.setAttribute("data-key", `link:${label}`);
      if (newTab) {
        a.setAttribute("target", "_blank");
        a.setAttribute("rel", "noopener noreferrer");
      }
      return a;
    };
    add(bar, link("Share on WhatsApp", `https://wa.me/?text=${encodeURIComponent(capped)}`, true));
    const subject = w.calendar ? w.calendar.title : w.title || "Namma Saathi";
    add(bar, link("Email it", `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(capped)}`, false));
    const copy = button("Copy", "genui-btn-secondary", "copy");
    copy.addEventListener("click", () => copyText(capped, msg));
    add(bar, copy);
    add(root, bar, h("p", "genui-help", "Nothing is sent by Namma Saathi. These buttons open your own calendar, WhatsApp or email app, and you decide whether to send."), msg);
    return root;
  };

  /* ------------------------------------------------------------------- engine */
  function buildWidget(w, ctx) {
    const section = h("section", `genui-widget genui-w-${w.type}`);
    section.setAttribute("data-genui-id", w.id);
    section.setAttribute("data-genui-type", w.type);
    const inner = BUILDERS[w.type](w, ctx);
    section.appendChild(inner);
    return section;
  }

  function childrenOf(n) {
    return Array.prototype.slice.call(n.children || []);
  }
  function findByKey(root, key) {
    for (const c of childrenOf(root)) {
      if (c.getAttribute && c.getAttribute("data-key") === key) return c;
      const hit = findByKey(c, key);
      if (hit) return hit;
    }
    return null;
  }

  function render(container, widgets, options) {
    const onAction = options && typeof options.onAction === "function" ? options.onAction : null;
    if (!container) return [];
    if (container.classList && container.classList.add) container.classList.add("genui");
    const list = Array.isArray(widgets) ? widgets : [];
    const rendered = [];
    const act = (widgetId, actionId, values) => {
      if (!onAction) { console.warn("genui: no onAction handler; action ignored", actionId); return undefined; }
      try {
        return onAction({ widgetId, actionId, values });
      } catch (e) {
        console.warn("genui: onAction failed");
        return undefined;
      }
    };
    list.forEach((w, index) => {
      if (index >= MAX_WIDGETS) { console.warn(`genui: skipped widget ${index}; at most ${MAX_WIDGETS} per reply`); return; }
      const v = validate(w);
      if (!v.ok) { console.warn(`genui: skipped widget ${index}: ${v.error}`); return; }
      const old = childrenOf(container).find((c) => c.getAttribute("data-genui-id") === w.id) || null;
      const prev = old && old.__genui ? old.__genui.dirty : {};
      const top = { dirty: {}, prev };
      let el;
      try {
        el = buildWidget(w, { act, top });
      } catch (e) {
        console.warn(`genui: skipped widget ${index}: could not build`);
        return;
      }
      el.__genui = top;
      let focusKey = null;
      let sel = null;
      if (old) {
        const active = document.activeElement;
        if (active && old.contains && old.contains(active) && active.getAttribute) {
          focusKey = active.getAttribute("data-key");
          try { sel = [active.selectionStart, active.selectionEnd]; } catch (e) { sel = null; }
        }
        container.replaceChild(el, old);
      } else {
        container.appendChild(el);
      }
      if (el.classList && el.classList.add) el.classList.add("genui-enter");
      if (focusKey) {
        const target = findByKey(el, focusKey);
        if (target && !target.disabled && typeof target.focus === "function") {
          target.focus();
          if (sel && typeof sel[0] === "number" && typeof target.setSelectionRange === "function") {
            try { target.setSelectionRange(sel[0], sel[1]); } catch (e) { /* not a text control */ }
          }
        }
      }
      rendered.push(w.id);
    });
    return rendered;
  }

  function clear(container) {
    if (!container) return;
    if (typeof container.replaceChildren === "function") container.replaceChildren();
    else while (container.firstChild) container.removeChild(container.firstChild);
  }

  global.saathiGenUI = Object.freeze({ render, validate, clear });
})(typeof window !== "undefined" ? window : globalThis);
