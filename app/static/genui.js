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
  const EMAIL_ID_RE = /^[A-Za-z0-9_-]{1,100}$/;
  const SUFFIX_RE = /^[a-z0-9_.-]{1,40}$/;
  const FIELD_RE = /^[a-z][a-z0-9_]{0,31}$/;
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
  const ISO_RE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;
  const ISO_OFFSET_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;
  const STATUSES = ["pending", "running", "waiting_for_input", "waiting_for_approval", "completed", "failed", "cancelled"];
  const PLAN_KINDS = ["event", "reminder", "email", "task"];
  const PRIORITIES = ["low", "normal", "high"];
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
  // A list whose entries must differ in one property (ids and keys become action suffixes).
  const distinct = (item, min, max, prop) => S.any((v, d) => {
    const e = S.arr(item, min, max).check(v, d);
    if (e) return e;
    const seen = {};
    for (const x of v) {
      if (own(seen, x[prop])) return `duplicate ${prop} "${x[prop]}"`;
      seen[x[prop]] = true;
    }
    return "";
  });

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
          approve_action_id: S.opt(S.str(1, 200)), view_action_id: S.opt(S.str(1, 200)), edit_action_id: S.opt(S.str(1, 200)),
        }),
        0, 100,
      ),
      demo: S.opt(S.bool()),
      summary: S.opt(S.str(0)),
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
      state: S.oneOf(["draft", "sent", "copied", "failed"]),
      detail: S.opt(S.str(0)),
      demo: S.bool(),
      editable: S.opt(S.bool()),
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
      demo: S.opt(S.bool()),
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
    email_list: {
      title: S.str(1),
      items: distinct(
        S.obj({
          id: S.re(EMAIL_ID_RE, 100), sender: S.str(1, 300), subject: S.str(0, 500), snippet: S.str(0, 1000),
          received: S.re(ISO_RE, 40), unread: S.bool(), important: S.bool(), needs_reply: S.bool(),
        }),
        0, 10, "id",
      ),
      empty_text: S.str(0),
      demo: S.bool(),
    },
    event_preview: {
      title: S.str(1),
      fields: S.obj({
        title: S.str(1, 300), date: dateKey, start: S.re(TIME_RE), minutes: S.int(1, 1440),
        attendees: S.arr(S.str(1, 320), 0, 10),
      }),
      conflicts: S.arr(S.obj({ title: S.str(1, 300), start: S.re(TIME_RE), end: S.re(TIME_RE) }), 0, 10),
      state: S.oneOf(["draft", "created", "failed", "cancelled"]),
      detail: S.opt(S.str(0)),
      approve: action,
      cancel: S.opt(action),
      demo: S.bool(),
      editable: S.bool(),
    },
    day_planner: {
      title: S.str(1),
      date: dateKey,
      items: distinct(
        S.obj({
          id: S.re(SUFFIX_RE, 40), kind: S.oneOf(PLAN_KINDS), title: S.str(1, 300), time: S.opt(S.re(TIME_RE)),
          done: S.bool(), priority: S.oneOf(PRIORITIES), detail: S.opt(S.str(0, 500)),
        }),
        0, 40, "id",
      ),
      demo: S.bool(),
      editable: S.bool(),
    },
    memory_list: {
      title: S.str(1),
      items: distinct(S.obj({ key: S.re(SUFFIX_RE, 40), label: S.str(1, 200), value: S.str(0, 1000) }), 0, 30, "key"),
      empty_text: S.str(0),
      consent_text: S.str(1),
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

  /* ---- shared helpers for the richer widgets */
  const DEMO_BADGE = "Demo: nothing real happens";
  function headRow(title, demo, demoText) {
    const head = h("div", "genui-row");
    add(head, titleEl(title, "h3", "genui-title genui-grow"));
    if (demo) add(head, badge(demoText || DEMO_BADGE, "genui-badge-demo"));
    return head;
  }
  // A text + icon status label, so a state never relies on colour alone.
  function stateChip(cls, icon, text) {
    const chip = h("span", `genui-state ${cls}`);
    const ic = h("span", "genui-state-icon", icon);
    ic.setAttribute("aria-hidden", "true");
    return add(chip, ic, h("span", "", text));
  }
  // Fire one action and rest the given buttons until the widget is replaced (or the call fails).
  function fireOnce(ctx, root, locks, wid, actionId, values) {
    for (const b of locks) b.disabled = true;
    root.setAttribute("aria-busy", "true");
    const r = ctx.act(wid, actionId, values);
    if (isThenable(r)) {
      r.then(null, () => {
        for (const b of locks) b.disabled = !!b.__off;
        root.removeAttribute("aria-busy");
      });
    }
  }
  function relTime(iso, now) {
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return iso;
    const mins = Math.round((now - t) / 60000);
    if (mins < -1) return fmtDateTime(iso);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours} h ago`;
    const days = Math.floor(hours / 24);
    if (days === 1) return "yesterday";
    if (days < 7) return `${days} days ago`;
    return fmtDateTime(iso);
  }

  /* ---- task board */
  const STATUS_VIEW = {
    pending: ["○", "Pending"],
    running: ["↻", "Running"],
    waiting_for_input: ["?", "Waiting for you"],
    waiting_for_approval: ["!", "Needs your approval"],
    completed: ["✓", "Done"],
    failed: ["✕", "Failed"],
    cancelled: ["–", "Cancelled"],
  };
  const CANCELLABLE = ["pending", "running", "waiting_for_input", "waiting_for_approval"];

  BUILDERS.task_board = (w, ctx) => {
    const root = h("div", "genui-board");
    root.setAttribute("aria-live", "polite");
    add(root, headRow(w.title, w.demo, "Demo: nothing real is done"));
    if (w.summary) add(root, h("p", "genui-board-summary", w.summary));
    if (!w.tasks.length) add(root, h("p", "genui-empty", "No tasks yet."));
    const ul = h("ul", "genui-list genui-tasks");
    for (const t of w.tasks) {
      const li = h("li", `genui-list-item genui-task genui-status-${t.status}`);
      const mark = h("span", `genui-task-mark genui-mark-${t.status}`, STATUS_VIEW[t.status][0]);
      mark.setAttribute("aria-hidden", "true");
      const main = h("div", "genui-grow genui-task-main");
      const chip = h("span", `genui-chip genui-chip-${t.status}`, STATUS_VIEW[t.status][1]);
      add(main, h("span", "genui-item-title", t.title), chip);
      if (t.detail) add(main, h("span", "genui-item-detail", t.detail));
      add(li, mark, main);
      const bar = h("div", "genui-actions genui-actions-tight genui-task-actions");
      const mk = (label, cls, key, actionId, then) => {
        const b = button(label, cls, `${key}:${t.id}`);
        b.setAttribute("aria-label", `${label}: ${t.title}`);
        b.addEventListener("click", () => then(b, actionId));
        add(bar, b);
      };
      if (t.approve_action_id && t.status === "waiting_for_approval") {
        mk("Approve", "genui-btn-primary", "approve", t.approve_action_id, (b, id) => {
          if (b.disabled) return;
          fireOnce(ctx, root, [b], w.id, id, { task_id: t.id });
        });
      }
      if (t.view_action_id) mk("View", "genui-btn-secondary", "view", t.view_action_id, (b, id) => ctx.act(w.id, id, { task_id: t.id }));
      if (t.edit_action_id) mk("Edit", "genui-btn-secondary", "edit", t.edit_action_id, (b, id) => ctx.act(w.id, id, { task_id: t.id }));
      if (t.retry_action_id && t.status === "failed") mk("Retry", "genui-btn-secondary", "retry", t.retry_action_id, (b, id) => ctx.act(w.id, id, { task_id: t.id }));
      if (t.cancel_action_id && CANCELLABLE.indexOf(t.status) !== -1) mk("Cancel", "genui-btn-danger", "cancel", t.cancel_action_id, (b, id) => ctx.act(w.id, id, { task_id: t.id }));
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
  const COMPOSER_STATE = {
    draft: ["✎", "Draft, not sent"],
    copied: ["⧉", "Copied, not sent"],
    sent: ["✓", "Sent"],
    failed: ["✕", "Failed"],
  };
  BUILDERS.composer = (w, ctx) => {
    const root = h("div", `genui-composer genui-composer-${w.state}`);
    const [icon, label] = COMPOSER_STATE[w.state];
    // A demo is never described as sent: the label says so plainly.
    const stateText = w.demo && w.state === "sent" ? "Demo: not really sent" : label;
    const head = headRow(w.channel === "email" ? "Draft email" : "Draft message", w.demo, "Demo: nothing is really sent");
    add(head, stateChip(`genui-state-${w.state}`, icon, stateText));
    add(root, head);
    if (w.detail) {
      const d = h("p", "genui-detail", w.detail);
      d.setAttribute("role", "status");
      add(root, d);
    }
    const live = w.state === "draft" && w.editable !== false;
    const inputs = {};
    const mk = (name, labelText, tag) => {
      const id = uid("genui-c");
      const key = `${w.id}/${name}`;
      const wrap = h("div", "genui-field");
      const lab = h("label", "genui-label", labelText);
      lab.setAttribute("for", id);
      const input = h(tag, "genui-input");
      attrs(input, { id, name, "data-key": key });
      if (tag === "textarea") input.setAttribute("rows", "8");
      else input.setAttribute("type", "text");
      input.setAttribute("maxlength", MAX_TEXT);
      return { wrap: add(wrap, lab, input), input, key };
    };
    const parts = [];
    if (w.to !== undefined && w.to !== null) parts.push(["to", "To", "input", w.to]);
    if (w.subject !== undefined && w.subject !== null) parts.push(["subject", "Subject", "input", w.subject]);
    parts.push(["body", "Message", "textarea", w.body]);
    const current = {};
    for (const [name, labelText, tag, value] of parts) {
      current[name] = value;
      if (live) {
        const f = mk(name, labelText, tag);
        f.input.value = value;
        const prev = ctx.top.prev[f.key];
        if (prev) { f.input.value = String(prev.value); ctx.top.dirty[f.key] = prev; }
        const remember = () => { ctx.top.dirty[f.key] = { kind: "text", value: f.input.value }; };
        f.input.addEventListener("input", remember);
        inputs[name] = f.input;
        add(root, f.wrap);
      } else {
        const dl = h("dl", "genui-details genui-composer-read");
        add(dl, h("dt", "", labelText), h("dd", name === "body" ? "genui-composer-body" : "", value));
        add(root, dl);
      }
    }
    const values = () => {
      const out = {};
      for (const k of Object.keys(current)) out[k] = live ? inputs[k].value : current[k];
      return out;
    };
    const msg = h("p", "genui-help", "");
    msg.setAttribute("aria-live", "polite");
    const bar = h("div", "genui-actions");
    const locks = [];
    if (w.state === "draft") {
      add(root, h("p", "genui-help", w.demo ? "This is a demo. Nothing is really sent." : "Nothing is sent until you choose Send. Check the draft and edit it first."));
      const { style: sendVariant } = w.send;
      const send = button(w.send.label, `genui-btn-${sendVariant || "primary"}`, "send");
      if (w.send.disabled) { send.disabled = true; send.__off = true; }
      send.addEventListener("click", () => {
        if (send.disabled) return;
        fireOnce(ctx, root, locks, w.id, w.send.action_id, values());
      });
      locks.push(send);
      add(bar, send);
      const m = /^task\.approve:(.+)$/.exec(w.send.action_id);
      if (live && m) {
        const save = button("Save changes", "genui-btn-secondary", "save");
        save.addEventListener("click", () => {
          if (save.disabled) return;
          ctx.act(w.id, `task.edit:${m[1]}`, values());
          msg.textContent = "Your edits were sent to the draft.";
        });
        add(bar, save);
      }
    }
    if (w.copy_enabled) {
      const copy = button("Copy", "genui-btn-secondary", "copy");
      copy.addEventListener("click", () => copyText(composeText(w.channel, values()), msg));
      add(bar, copy);
    }
    return add(root, bar, msg);
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
    add(root, headRow(w.title, w.demo, "Demo: example calendar"));
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

  /* ---- email list */
  BUILDERS.email_list = (w, ctx) => {
    const root = h("div");
    add(root, headRow(w.title, w.demo, "Demo: example emails"));
    if (!w.items.length) {
      add(root, h("p", "genui-empty", w.empty_text || "No emails to show."));
      return root;
    }
    const now = Date.now();
    const ul = h("ul", "genui-list genui-emails");
    for (const m of w.items) {
      const li = h("li", `genui-list-item genui-email${m.unread ? " genui-unread" : ""}`);
      const main = h("div", "genui-grow");
      const top = h("div", "genui-email-top");
      const when = h("time", "genui-email-time", relTime(m.received, now));
      attrs(when, { datetime: m.received, title: fmtDateTime(m.received) });
      add(top, h("span", "genui-email-sender", m.sender), when);
      add(main, top, h("span", "genui-email-subject", m.subject || "(no subject)"));
      if (m.snippet) add(main, h("span", "genui-item-detail genui-email-snippet", m.snippet));
      const chips = h("div", "genui-chips");
      if (m.unread) add(chips, stateChip("genui-tag-unread", "●", "Unread"));
      if (m.important) add(chips, stateChip("genui-tag-important", "★", "Important"));
      if (m.needs_reply) add(chips, stateChip("genui-tag-reply", "↩", "Needs reply"));
      if (chips.children.length) add(main, chips);
      add(li, main);
      const bar = h("div", "genui-actions genui-actions-tight");
      const open = button("Open", "genui-btn-secondary", `open:${m.id}`);
      open.setAttribute("aria-label", `Open email from ${m.sender}: ${m.subject}`);
      open.addEventListener("click", () => ctx.act(w.id, `email.open:${m.id}`, {}));
      const reply = button("Reply", "genui-btn-primary", `reply:${m.id}`);
      reply.setAttribute("aria-label", `Reply to ${m.sender}: ${m.subject}`);
      reply.addEventListener("click", () => ctx.act(w.id, `email.reply:${m.id}`, {}));
      add(bar, open, reply);
      add(li, bar);
      add(ul, li);
    }
    return add(root, ul);
  };

  /* ---- event preview */
  const EVENT_STATE = {
    draft: ["✎", "Draft, not created"],
    created: ["✓", "Created"],
    failed: ["✕", "Failed"],
    cancelled: ["–", "Cancelled"],
  };
  const splitLines = (v) => String(v).split(/\r?\n/).map((x) => x.trim()).filter((x) => x);

  BUILDERS.event_preview = (w, ctx) => {
    const root = h("div", `genui-event-preview genui-event-${w.state}`);
    const f = w.fields;
    const [icon, label] = EVENT_STATE[w.state];
    const stateText = w.demo && w.state === "created" ? "Demo: not really created" : label;
    const head = headRow(w.title, w.demo, "Demo: nothing is really created");
    add(head, stateChip(`genui-state-${w.state}`, icon, stateText));
    add(root, head);
    if (w.conflicts.length) {
      const box = h("div", "genui-conflicts");
      box.setAttribute("role", "group");
      box.setAttribute("aria-label", "Clash with your calendar");
      const ic = h("span", "genui-state-icon", "!");
      ic.setAttribute("aria-hidden", "true");
      add(box, add(h("p", "genui-conflicts-title"), ic, h("span", "", " This overlaps with something already in your calendar:")));
      const ul = h("ul", "genui-conflict-list");
      for (const c of w.conflicts) add(ul, h("li", "", `${c.title}, ${c.start}–${c.end}`));
      add(root, add(box, ul));
    }
    if (w.detail) {
      const d = h("p", "genui-detail", w.detail);
      d.setAttribute("role", "status");
      add(root, d);
    }
    const draft = w.state === "draft";
    let form = null;
    if (draft && w.editable) {
      const defs = [
        { name: "title", label: "Title", kind: "text", required: true, value: f.title, max_length: 300 },
        { name: "date", label: "Date", kind: "date", required: true, value: f.date },
        { name: "start", label: "Time", kind: "time", required: true, value: f.start },
        { name: "minutes", label: "Length in minutes", kind: "number", required: true, value: f.minutes, min: 1, max: 1440 },
        { name: "attendees", label: "People (one per line)", kind: "textarea", value: f.attendees.join("\n"), max_length: 1000 },
      ];
      form = { el: h("form", "genui-form-el"), fields: defs.map((d) => buildField(d, ctx, w.id)) };
      form.el.setAttribute("novalidate", "novalidate");
      form.el.addEventListener("submit", (ev) => { if (ev && typeof ev.preventDefault === "function") ev.preventDefault(); });
      for (const fl of form.fields) add(form.el, fl.wrap);
      add(root, form.el);
    } else {
      const dl = h("dl", "genui-details");
      add(dl, h("dt", "", "Title"), h("dd", "", f.title));
      add(dl, h("dt", "", "Date"), h("dd", "", fmtDay(f.date)));
      add(dl, h("dt", "", "Time"), h("dd", "", f.start));
      add(dl, h("dt", "", "Length"), h("dd", "", `${f.minutes} minutes`));
      add(dl, h("dt", "", "People"), h("dd", "", f.attendees.length ? f.attendees.join(", ") : "Just you"));
      add(root, dl);
    }
    if (!draft) return root;
    const bar = h("div", "genui-actions");
    const locks = [];
    const msg = h("p", "genui-help", "");
    msg.setAttribute("aria-live", "polite");
    const collect = () => {
      if (!form) return { title: f.title, date: f.date, start: f.start, minutes: f.minutes, attendees: f.attendees.slice() };
      let firstBad = null;
      const out = {};
      for (const fl of form.fields) {
        let problem = fl.problem();
        const v = fl.read();
        if (!problem && fl.name === "minutes" && !Number.isInteger(v)) problem = "Enter whole minutes.";
        if (!problem && fl.name === "date" && dateKey.check(v)) problem = "Pick a real date.";
        if (!problem && fl.name === "start" && !TIME_RE.test(v)) problem = "Pick a time.";
        if (!problem && fl.name === "title" && String(v).trim() === "") problem = "This is required.";
        if (!problem && fl.name === "attendees") {
          const lines = splitLines(v);
          if (lines.length > 10) problem = "At most 10 people.";
          else if (lines.some((x) => x.length > 320)) problem = "Each line can have at most 320 characters.";
        }
        fl.show(problem);
        if (problem && !firstBad) firstBad = fl;
        out[fl.name] = fl.name === "attendees" ? splitLines(v) : fl.name === "title" ? String(v).trim() : v;
      }
      if (firstBad) {
        msg.textContent = "Please fix the highlighted fields.";
        if (typeof firstBad.input.focus === "function") firstBad.input.focus();
        return null;
      }
      msg.textContent = "";
      return out;
    };
    const { style: approveVariant } = w.approve;
    const approve = button(w.approve.label, `genui-btn-${approveVariant || "primary"}`, "approve");
    if (w.approve.disabled) { approve.disabled = true; approve.__off = true; }
    approve.addEventListener("click", () => {
      if (approve.disabled) return;
      const values = collect();
      if (!values) return;
      fireOnce(ctx, root, locks, w.id, w.approve.action_id, values);
    });
    locks.push(approve);
    add(bar, approve);
    if (w.cancel) {
      const { style: cancelVariant } = w.cancel;
      const cancel = button(w.cancel.label, `genui-btn-${cancelVariant || "secondary"}`, "cancel");
      if (w.cancel.disabled) { cancel.disabled = true; cancel.__off = true; }
      cancel.addEventListener("click", () => {
        if (cancel.disabled) return;
        fireOnce(ctx, root, locks, w.id, w.cancel.action_id, {});
      });
      locks.push(cancel);
      add(bar, cancel);
    }
    return add(root, bar, msg);
  };

  /* ---- day planner */
  const KIND_VIEW = { event: ["◷", "Event"], reminder: ["🔔", "Reminder"], email: ["✉", "Email"], task: ["☐", "Task"] };
  const PRIORITY_LABEL = { low: "Low", normal: "Normal", high: "High" };

  BUILDERS.day_planner = (w, ctx) => {
    const root = h("div", "genui-planner");
    add(root, headRow(w.title, w.demo, "Demo: example plan"));
    const doneCount = w.items.filter((x) => x.done).length;
    const sum = h("p", "genui-board-summary", `${fmtDay(w.date)}. ${doneCount} of ${w.items.length} done.`);
    sum.setAttribute("aria-live", "polite");
    add(root, sum);
    if (!w.items.length) add(root, h("p", "genui-empty", "Nothing planned yet."));
    const ol = h("ol", "genui-list genui-plan");
    w.items.forEach((it, i) => {
      const li = h("li", `genui-list-item genui-plan-item genui-prio-${it.priority}${it.done ? " genui-done" : ""}`);
      const main = h("div", "genui-plan-main");
      if (w.editable) {
        const id = uid("genui-pl");
        const cb = h("input", "genui-checkbox");
        attrs(cb, { type: "checkbox", id, name: `done_${it.id}`, "data-key": `toggle:${it.id}` });
        cb.checked = it.done;
        cb.setAttribute("aria-label", `Done: ${it.title}`);
        cb.addEventListener("change", () => ctx.act(w.id, `plan.toggle:${it.id}`, {}));
        add(main, cb);
      }
      const text = h("div", "genui-grow");
      add(text, h("span", "genui-item-title", it.title));
      const meta = h("div", "genui-chips");
      const [kIcon, kText] = KIND_VIEW[it.kind];
      add(meta, stateChip(`genui-kind genui-kind-${it.kind}`, kIcon, kText));
      if (it.time) add(meta, h("span", "genui-time-chip", it.time));
      if (it.priority === "high") add(meta, stateChip("genui-tag-high", "▲", "High priority"));
      if (!w.editable) add(meta, h("span", "genui-item-detail", it.done ? "Done" : "To do"));
      add(text, meta);
      if (it.detail) add(text, h("span", "genui-item-detail", it.detail));
      add(main, text);
      add(li, main);
      if (w.editable) {
        const bar = h("div", "genui-actions genui-actions-tight genui-plan-controls");
        const sid = uid("genui-ps");
        const pwrap = h("div", "genui-plan-prio");
        const plabel = h("label", "genui-plan-prio-label", "Priority");
        plabel.setAttribute("for", sid);
        const sel = h("select", "genui-input genui-plan-select");
        attrs(sel, { id: sid, name: `priority_${it.id}`, "data-key": `priority:${it.id}` });
        sel.setAttribute("aria-label", `Priority for ${it.title}`);
        for (const p of PRIORITIES) {
          const op = h("option", "", PRIORITY_LABEL[p]);
          op.setAttribute("value", p);
          op.value = p;
          add(sel, op);
        }
        sel.value = it.priority;
        sel.addEventListener("change", () => ctx.act(w.id, `plan.priority:${it.id}`, { priority: sel.value }));
        add(pwrap, plabel, sel);
        const up = button("↑ Up", "genui-btn-secondary", `up:${it.id}`);
        up.setAttribute("aria-label", `Move up: ${it.title}`);
        if (i === 0) up.disabled = true;
        up.addEventListener("click", () => { if (!up.disabled) ctx.act(w.id, `plan.move:${it.id}`, { direction: "up" }); });
        const down = button("↓ Down", "genui-btn-secondary", `down:${it.id}`);
        down.setAttribute("aria-label", `Move down: ${it.title}`);
        if (i === w.items.length - 1) down.disabled = true;
        down.addEventListener("click", () => { if (!down.disabled) ctx.act(w.id, `plan.move:${it.id}`, { direction: "down" }); });
        add(bar, pwrap, up, down);
        add(li, bar);
      }
      add(ol, li);
    });
    add(root, ol);
    if (w.editable) {
      const form = h("form", "genui-form-el genui-plan-add");
      form.setAttribute("novalidate", "novalidate");
      const titleField = buildField({ name: "title", label: "Add to your day", kind: "text", required: true, max_length: 200 }, ctx, w.id);
      const timeField = buildField({ name: "time", label: "Time (optional)", kind: "time" }, ctx, w.id);
      const submit = button("Add", "genui-btn-primary", "add");
      submit.setAttribute("type", "submit");
      const status = h("p", "genui-help", "");
      status.setAttribute("aria-live", "polite");
      add(form, titleField.wrap, timeField.wrap, add(h("div", "genui-actions"), submit), status);
      form.addEventListener("submit", (ev) => {
        if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
        const msg = titleField.problem() || timeField.problem();
        titleField.show(titleField.problem());
        timeField.show(timeField.problem());
        if (msg) {
          status.textContent = "Please fix the highlighted fields.";
          return;
        }
        const values = { title: String(titleField.read()).trim() };
        const t = String(timeField.read());
        if (t) values.time = t;
        status.textContent = "";
        ctx.act(w.id, "plan.add", values);
        for (const [fl, name] of [[titleField, "title"], [timeField, "time"]]) {
          fl.input.value = "";
          ctx.top.dirty[`${w.id}/${name}`] = { kind: name === "time" ? "time" : "text", value: "" };
        }
      });
      add(root, form);
    }
    return root;
  };

  /* ---- memory list */
  BUILDERS.memory_list = (w, ctx) => {
    const root = h("div", "genui-memory");
    add(root, titleEl(w.title));
    add(root, h("p", "genui-consent", w.consent_text));
    if (!w.items.length) {
      add(root, h("p", "genui-empty", w.empty_text || "Nothing is remembered yet."));
      return root;
    }
    const ul = h("ul", "genui-list genui-memories");
    for (const m of w.items) {
      const li = h("li", "genui-list-item genui-memory-item");
      const main = h("div", "genui-grow");
      add(main, h("span", "genui-item-detail genui-memory-label", m.label), h("span", "genui-item-title genui-memory-value", m.value));
      add(li, main);
      const forget = button("Forget", "genui-btn-danger", `forget:${m.key}`);
      forget.setAttribute("aria-label", `Forget: ${m.label}`);
      forget.addEventListener("click", () => {
        if (forget.disabled) return;
        fireOnce(ctx, li, [forget], w.id, `memory.forget:${m.key}`, {});
      });
      const form = h("form", "genui-memory-form");
      form.setAttribute("novalidate", "novalidate");
      const id = uid("genui-m");
      const key = `${w.id}/mem:${m.key}`;
      const lab = h("label", "genui-label", `Change ${m.label}`);
      lab.setAttribute("for", id);
      const input = h("input", "genui-input");
      attrs(input, { id, type: "text", name: `value_${m.key}`, maxlength: 500, "data-key": key });
      const prev = ctx.top.prev[key];
      if (prev) { input.value = String(prev.value); ctx.top.dirty[key] = prev; }
      input.addEventListener("input", () => { ctx.top.dirty[key] = { kind: "text", value: input.value }; });
      const err = h("p", "genui-error", "");
      err.setAttribute("role", "alert");
      err.hidden = true;
      const save = button("Save", "genui-btn-primary", `save:${m.key}`);
      save.setAttribute("type", "submit");
      save.setAttribute("aria-label", `Save: ${m.label}`);
      form.addEventListener("submit", (ev) => {
        if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
        const v = String(input.value).trim();
        if (!v) {
          err.textContent = "Type the new value first.";
          err.hidden = false;
          input.setAttribute("aria-invalid", "true");
          return;
        }
        err.hidden = true;
        input.removeAttribute("aria-invalid");
        ctx.act(w.id, "memory.save", { key: m.key, value: v });
        input.value = "";
        ctx.top.dirty[key] = { kind: "text", value: "" };
      });
      add(form, lab, add(h("div", "genui-memory-row"), input, save), err);
      add(li, forget, form);
      add(ul, li);
    }
    return add(root, ul);
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
