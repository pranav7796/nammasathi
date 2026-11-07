const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE_PATH = path.join(__dirname, "../../app/static/genui.js");
const SOURCE = fs.readFileSync(SOURCE_PATH, "utf8");

/* ---- a small but faithful DOM ------------------------------------------------ */
class Node {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.checked = false;
    this._text = "";
    const self = this;
    this.classList = {
      add: (c) => { if (!self.className.split(" ").includes(c)) self.className = `${self.className} ${c}`.trim(); },
      contains: (c) => self.className.split(" ").includes(c),
    };
  }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text; }
  set textContent(v) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = String(v); }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    this.children.push(c);
    return c;
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    assert.notEqual(i, -1, "removeChild: not a child");
    this.children.splice(i, 1);
    c.parentNode = null;
    return c;
  }
  replaceChild(n, o) {
    const i = this.children.indexOf(o);
    assert.notEqual(i, -1, "replaceChild: not a child");
    if (n.parentNode) n.parentNode.removeChild(n);
    this.children[this.children.indexOf(o)] = n;
    n.parentNode = this;
    o.parentNode = null;
    return o;
  }
  replaceChildren() { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "class") this.className = String(v); if (k === "value") this.value = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(n, f) { (this.listeners[n] = this.listeners[n] || []).push(f); }
  dispatch(type, extra = {}) {
    const ev = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra };
    for (let n = this; n; n = n.parentNode) (n.listeners[type] || []).forEach((f) => f(ev));
    return ev;
  }
  click() {
    if (this.disabled) return;
    this.dispatch("click");
    if (this.tagName === "BUTTON" && this.attributes.type === "submit") {
      for (let n = this.parentNode; n; n = n.parentNode) if (n.tagName === "FORM") { n.dispatch("submit"); break; }
    }
  }
  focus() { if (!this.disabled) this.ownerDocument.activeElement = this; }
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }
  type(v) { this.value = v; this.dispatch("input"); }
  // Simple selectors: tag, .class, [attr="v"], and combinations like tag.class[attr="v"].
  matches(sel) {
    const re = /^([a-z0-9]*)((?:\.[\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/i.exec(sel);
    if (!re) throw new Error(`unsupported selector ${sel}`);
    if (re[1] && this.tagName !== re[1].toUpperCase()) return false;
    for (const part of re[2].match(/\.[\w-]+|\[[^\]]+\]/g) || []) {
      if (part[0] === ".") { if (!this.className.split(" ").includes(part.slice(1))) return false; }
      else {
        const m = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(part);
        const v = this.getAttribute(m[1]);
        if (v === null || (m[2] !== undefined && v !== m[2])) return false;
      }
    }
    return true;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (c.matches(sel)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function makeEnv({ clipboard } = {}) {
  const doc = { activeElement: null };
  doc.createElement = (t) => new Node(doc, t);
  doc.body = new Node(doc, "body");
  const warnings = [];
  const blobs = [];
  const opened = [];
  class FakeBlob { constructor(parts, opts) { this.text = parts.join(""); this.type = opts && opts.type; blobs.push(this); } }
  const anchors = [];
  const origCreate = doc.createElement;
  doc.createElement = (t) => {
    const n = origCreate(t);
    if (t === "a") anchors.push(n);
    return n;
  };
  const win = {
    navigator: { clipboard },
    URL: { createObjectURL: () => "blob:test", revokeObjectURL() {} },
    open: (...a) => opened.push(a),
  };
  const ctx = {
    document: doc, window: win, Blob: FakeBlob, TextEncoder, encodeURIComponent, console: { warn: (...a) => warnings.push(a.join(" ")), log() {} },
    setTimeout, clearTimeout, Date, Math, JSON, Number, Promise, Array, Object, String,
  };
  vm.runInNewContext(SOURCE, ctx);
  const api = win.saathiGenUI;
  const container = new Node(doc, "div");
  const actions = [];
  const onAction = (a) => { actions.push(JSON.parse(JSON.stringify(a))); };
  return { doc, api, container, actions, onAction, warnings, blobs, anchors, opened };
}

const idOf = (c) => c.attributes["data-genui-id"];
const ids = (container) => container.children.map(idOf);
const action = (label, id, style) => ({ label, action_id: id, style });
function cart(over = {}) {
  return {
    id: "cart1", type: "cart", title: "Basket", demo: true, editable: true, total_inr: "156.00", fees_inr: null,
    lines: [{ id: "l1", name: "Rice", quantity: 2, unit_price_inr: "78.00", detail: "1 kg" }], ...over,
  };
}
function confirmation(over = {}) {
  return {
    id: "conf", type: "confirmation", title: "Place order?", demo: true, details: [{ label: "Total", value: "Rs 156" }],
    consequence: "The demo order is placed.", confirm: action("Yes", "order.confirm", "primary"), reject: action("No", "order.reject"), ...over,
  };
}

/* ---- tests ------------------------------------------------------------------- */
test("render keeps one element per id and replaces in place", () => {
  const e = makeEnv();
  const a = { id: "a", type: "text", text: "one" };
  const b = { id: "b", type: "text", text: "two" };
  const c = { id: "c", type: "text", text: "three" };
  e.api.render(e.container, [a, b], { onAction: e.onAction });
  e.api.render(e.container, [{ ...a, text: "uno" }, c], { onAction: e.onAction });
  assert.deepEqual(ids(e.container), ["a", "b", "c"]);
  assert.equal(e.container.children[0].textContent, "uno");
  assert.equal(e.container.children[1].textContent, "two");
  e.api.clear(e.container);
  assert.equal(e.container.children.length, 0);
});

test("validation rejects unknown, extra, over-limit and too-deep widgets without throwing", () => {
  const e = makeEnv();
  const deep = (n) => ({ id: `d${n}`, type: "card", title: "t", children: n ? [deep(n - 1)] : [] });
  const bad = [
    { id: "x1", type: "iframe", src: "x" },
    { id: "x2", type: "text", text: "hi", onclick: "alert(1)" },
    { id: "x3", type: "text", text: "a".repeat(2001) },
    { id: "X4", type: "text", text: "bad id" },
    { id: "x5", type: "card", title: "t", children: Array.from({ length: 41 }, (_, i) => ({ id: `k${i}`, type: "text", text: "x" })) },
    deep(3),
    { id: "x6", type: "heading", text: "h", level: 4 },
    { id: "x7", type: "cart", title: "t", lines: [{ id: "l", name: "n", quantity: 100, unit_price_inr: "1" }], total_inr: "1", demo: true },
    { id: "x8", type: "cart", title: "t", lines: [], total_inr: "1" },
    null, 5, "str", [],
  ];
  const good = [deep(2), { id: "ok", type: "text", text: "fine" }];
  assert.doesNotThrow(() => {
    e.api.render(e.container, bad, { onAction: e.onAction });
    e.api.render(e.container, good, { onAction: e.onAction });
  });
  assert.deepEqual(ids(e.container), ["d2", "ok"]);
  assert.ok(e.warnings.length >= bad.length);
  for (const w of bad) assert.equal(e.api.validate(w).ok, false);
  assert.equal(e.api.validate({ id: "z", type: "text", text: "a".repeat(2000) }).ok, true);
  assert.equal(e.api.validate(undefined).ok, false);
});

test("at most 12 widgets are rendered per reply", () => {
  const e = makeEnv();
  const many = Array.from({ length: 15 }, (_, i) => ({ id: `w${i}`, type: "text", text: "x" }));
  e.api.render(e.container, many, {});
  assert.equal(e.container.children.length, 12);
});

test("source uses none of the forbidden DOM or code-execution APIs", () => {
  const code = SOURCE.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  const forbidden = [/innerHTML/, /outerHTML/, /insertAdjacentHTML/, /document\.write/, /\beval\s*\(/, /new\s+Function/, /\.style\b/, /setAttribute\(\s*["']style/, /setAttribute\(\s*["']on/i, /\.on[a-z]+\s*=[^=]/, /srcdoc/, /javascript:/];
  for (const re of forbidden) assert.doesNotMatch(code, re, `forbidden pattern ${re}`);
  for (const re of [/innerHTML/, /outerHTML/, /insertAdjacentHTML/, /document\.write/, /\beval\s*\(/]) assert.doesNotMatch(SOURCE, re);
});

test("widget text stays text: markup is never parsed", () => {
  const e = makeEnv();
  e.api.render(e.container, [{ id: "t", type: "text", text: "<img src=x onerror=alert(1)>" }], {});
  const p = e.container.children[0].children[0];
  assert.equal(p.textContent, "<img src=x onerror=alert(1)>");
  assert.equal(e.container.querySelectorAll("img").length, 0);
});

test("cart edits call onAction with the contract values and never change displayed totals", () => {
  const e = makeEnv();
  e.api.render(e.container, [cart()], { onAction: e.onAction });
  const totalOf = () => e.container.querySelector("dd.genui-total").textContent;
  assert.equal(totalOf(), "₹156.00");
  e.container.querySelector('[data-key="plus:l1"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "cart1", actionId: "cart.set_quantity", values: { line_id: "l1", quantity: 3 } });
  const input = e.container.querySelector('[data-key="qty:l1"]');
  input.value = "7";
  input.dispatch("change");
  assert.deepEqual(e.actions.pop(), { widgetId: "cart1", actionId: "cart.set_quantity", values: { line_id: "l1", quantity: 7 } });
  e.container.querySelector('[data-key="minus:l1"]').click();
  assert.equal(e.actions.pop().values.quantity, 6);
  input.value = "150";
  input.dispatch("change");
  assert.equal(e.actions.length, 0, "out of range is not sent");
  assert.equal(e.container.querySelector("p.genui-error").hidden, false);
  assert.equal(totalOf(), "₹156.00");
  assert.match(e.container.textContent, /Demo/);
});

test("read-only cart has no controls; demo false shows no Demo badge", () => {
  const e = makeEnv();
  e.api.render(e.container, [cart({ editable: false, demo: false, fees_inr: "15.00" })], {});
  assert.equal(e.container.querySelectorAll("button").length, 0);
  assert.doesNotMatch(e.container.textContent, /Demo/);
  assert.match(e.container.textContent, /₹15\.00/);
});

test("confirmation fires once on double click and rests until replaced", () => {
  const e = makeEnv();
  e.api.render(e.container, [confirmation()], { onAction: e.onAction });
  const yes = e.container.querySelector('[data-key="act:order.confirm"]');
  yes.click();
  yes.dispatch("click");
  yes.click();
  assert.equal(e.actions.length, 1);
  const no = e.container.querySelector('[data-key="act:order.reject"]');
  assert.equal(no.disabled, true);
  no.dispatch("click");
  assert.equal(e.actions.length, 1);
  e.api.render(e.container, [confirmation({ title: "Updated" })], { onAction: e.onAction });
  assert.equal(e.container.querySelector('[data-key="act:order.confirm"]').disabled, false);
  assert.match(e.container.textContent, /Demo/);
});

test("confirmation re-enables when onAction rejects", async () => {
  const e = makeEnv();
  e.api.render(e.container, [confirmation()], { onAction: () => Promise.reject(new Error("net")) });
  e.container.querySelector('[data-key="act:order.confirm"]').click();
  await new Promise((r) => setImmediate(r));
  assert.equal(e.container.querySelector('[data-key="act:order.confirm"]').disabled, false);
});

function formWidget(fields) {
  return { id: "f", type: "form", title: "Form", fields, submit: action("Go", "form.go", "primary") };
}
const F = (name, kind, extra = {}) => ({ name, label: name, kind, ...extra });

test("form enforces required, min/max, max_length and submits typed values", () => {
  const e = makeEnv();
  e.api.render(e.container, [formWidget([
    F("who", "text", { required: true, max_length: 5 }), F("n", "number", { min: 1, max: 10, value: 2 }),
    F("area", "select", { options: [{ value: "a", label: "A" }], value: "a" }), F("ok", "checkbox", { required: true }),
    F("d", "date", { value: "2026-11-09" }), F("t", "time"), F("note", "textarea"),
  ])], { onAction: e.onAction });
  const q = (n) => e.container.querySelector(`[name="${n}"]`);
  const submit = e.container.querySelector('[data-key="submit"]');
  submit.click();
  assert.equal(e.actions.length, 0);
  assert.equal(q("who").getAttribute("aria-invalid"), "true");
  assert.equal(q("ok").getAttribute("aria-invalid"), "true");
  assert.equal(e.doc.activeElement, q("who"));
  q("who").type("toolong");
  q("n").type("11");
  submit.click();
  assert.equal(e.actions.length, 0);
  assert.equal(e.container.querySelectorAll("p.genui-error").filter((p) => !p.hidden).length, 3);
  q("who").type("Toit");
  q("n").type("4");
  q("ok").checked = true;
  q("ok").dispatch("change");
  submit.click();
  assert.deepEqual(e.actions, [{ widgetId: "f", actionId: "form.go", values: { who: "Toit", n: 4, area: "a", ok: true, d: "2026-11-09", t: "", note: "" } }]);
  assert.equal(submit.disabled, true, "disabled while submitting");
  submit.click();
  assert.equal(e.actions.length, 1);
});

test("form labels are tied to inputs and submit re-enables when the promise settles", async () => {
  const e = makeEnv();
  e.api.render(e.container, [formWidget([F("who", "text")])], { onAction: () => Promise.resolve() });
  const label = e.container.querySelector("label");
  assert.equal(label.getAttribute("for"), e.container.querySelector('[name="who"]').getAttribute("id"));
  const submit = e.container.querySelector('[data-key="submit"]');
  submit.click();
  assert.equal(submit.disabled, true);
  await new Promise((r) => setImmediate(r));
  assert.equal(submit.disabled, false);
});

test("typed form values and focus survive an in-place replace", () => {
  const e = makeEnv();
  const w = formWidget([F("who", "text"), F("note", "textarea")]);
  e.api.render(e.container, [w], { onAction: e.onAction });
  const who = e.container.querySelector('[name="who"]');
  who.type("Asha");
  who.focus();
  who.selectionStart = 2; who.selectionEnd = 2;
  e.api.render(e.container, [{ ...w, title: "Form v2", fields: [F("who", "text", { value: "server" }), F("other", "text")] }], { onAction: e.onAction });
  const again = e.container.querySelector('[name="who"]');
  assert.notEqual(again, who);
  assert.equal(again.value, "Asha");
  assert.equal(e.doc.activeElement, again);
  assert.equal(again.selectionStart, 2);
  assert.equal(e.container.querySelector('[name="other"]').value, "");
});

test("form rejects duplicate names, select without options, options on text", () => {
  const e = makeEnv();
  assert.equal(e.api.validate(formWidget([F("a", "text"), F("a", "text")])).ok, false);
  assert.equal(e.api.validate(formWidget([F("a", "select")])).ok, false);
  assert.equal(e.api.validate(formWidget([F("a", "text", { options: [{ value: "x", label: "x" }] })])).ok, false);
  assert.equal(e.api.validate(formWidget([F("A", "text")])).ok, false);
});

test("task_board shows retry and cancel only when ids exist and the status allows", () => {
  const e = makeEnv();
  const t = (id, status, extra = {}) => ({ id, title: id, status, ...extra });
  e.api.render(e.container, [{ id: "tb", type: "task_board", title: "T", tasks: [
    t("f1", "failed", { retry_action_id: "r.f1" }), t("f2", "failed"), t("c1", "completed", { retry_action_id: "r.c1", cancel_action_id: "x.c1" }),
    t("r1", "running", { cancel_action_id: "x.r1", retry_action_id: "r.r1" }), t("r2", "running"),
  ] }], { onAction: e.onAction });
  const keys = e.container.querySelectorAll("button").map((b) => b.getAttribute("data-key"));
  assert.deepEqual(keys, ["retry:f1", "cancel:r1"]);
  e.container.querySelector('[data-key="retry:f1"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "tb", actionId: "r.f1", values: { task_id: "f1" } });
  assert.equal(e.container.children[0].children[0].getAttribute("aria-live"), "polite");
  assert.match(e.container.textContent, /Failed/);
  assert.match(e.container.textContent, /Done/);
});

test("status renders loading, success, empty and error with retry", () => {
  const e = makeEnv();
  const kinds = ["loading", "success", "empty", "error"].map((kind) => ({ id: `s_${kind}`, type: "status", kind, message: `msg ${kind}`, retry: kind === "error" ? action("Retry", "s.retry") : null }));
  e.api.render(e.container, kinds, { onAction: e.onAction });
  assert.equal(e.container.children.length, 4);
  assert.equal(e.container.querySelectorAll("button").length, 1);
  assert.equal(e.container.querySelectorAll(".genui-spinner").length, 1);
  assert.match(e.container.children[0].textContent, /Working: msg loading/);
  assert.match(e.container.children[3].textContent, /Problem: msg error/);
  e.container.querySelector("button").click();
  assert.deepEqual(e.actions.pop(), { widgetId: "s_error", actionId: "s.retry", values: {} });
});

const unfold = (s) => s.replace(/\r\n /g, "");
function shareWith(e, calendar, summary = "Dinner at 7") {
  e.api.render(e.container, [{ id: "sh", type: "share_panel", title: "Share", summary, calendar }], { onAction: e.onAction });
}

test("share_panel builds a correct .ics: CRLF, escaping, folding, UTC, UID, DTSTAMP", () => {
  const e = makeEnv();
  const longDesc = `Line one, with; special \\ chars\nLine two ${"ಕನ್ನಡ ".repeat(40)}`;
  shareWith(e, { title: "Dinner, with Asha; Toit", start: "2026-11-09T19:30:00+05:30", end: "2026-11-09T21:00:00+05:30", location: "Toit, Indiranagar", description: longDesc });
  e.container.querySelector('[data-key="ics"]').click();
  assert.equal(e.blobs.length, 1);
  assert.match(e.blobs[0].type, /^text\/calendar/);
  const text = e.blobs[0].text;
  assert.ok(text.startsWith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"));
  assert.ok(text.endsWith("END:VEVENT\r\nEND:VCALENDAR\r\n"));
  assert.equal(text.replace(/\r\n/g, "").includes("\n"), false, "no bare LF");
  assert.equal(/[^\r]\n/.test(text), false);
  const lines = text.split("\r\n");
  for (const l of lines) assert.ok(Buffer.byteLength(l, "utf8") <= 75, `line too long: ${l}`);
  const un = unfold(text);
  assert.match(un, /\r\nUID:[^\r\n]+@namma-saathi\r\n/);
  assert.match(un, /\r\nDTSTAMP:\d{8}T\d{6}Z\r\n/);
  assert.ok(un.includes("\r\nDTSTART:20261109T140000Z\r\n"));
  assert.ok(un.includes("\r\nDTEND:20261109T153000Z\r\n"));
  assert.ok(un.includes("\r\nSUMMARY:Dinner\\, with Asha\\; Toit\r\n"));
  assert.ok(un.includes("\r\nLOCATION:Toit\\, Indiranagar\r\n"));
  assert.ok(un.includes("DESCRIPTION:Line one\\, with\\; special \\\\ chars\\nLine two "));
  assert.ok(lines.some((l) => l.startsWith(" ")), "long description was folded");
  // Folding never splits a multi-byte character.
  assert.ok(un.includes("ಕನ್ನಡ ".repeat(40)));
});

test("share_panel rejects an event that ends before it starts, and bad offsets", () => {
  const e = makeEnv();
  shareWith(e, { title: "x", start: "2026-11-09T21:00:00+05:30", end: "2026-11-09T19:00:00+05:30" });
  e.container.querySelector('[data-key="ics"]').click();
  assert.equal(e.blobs.length, 0);
  assert.match(e.container.textContent, /ends before it starts/);
  assert.equal(e.api.validate({ id: "s", type: "share_panel", title: "t", summary: "s", calendar: { title: "x", start: "2026-11-09T19:00:00", end: "2026-11-09T20:00:00" } }).ok, false);
});

test("share_panel links are built locally, capped at 1500 characters, and say nothing is sent", () => {
  const e = makeEnv();
  const summary = `Plan & go? 100% ಕನ್ನಡ ${"x".repeat(1800)}`;
  shareWith(e, null, summary);
  assert.equal(e.container.querySelector('[data-key="ics"]'), null);
  const capped = `${summary.slice(0, 1499)}…`;
  const wa = e.container.querySelector('[data-key="link:Share on WhatsApp"]');
  assert.equal(wa.getAttribute("href"), `https://wa.me/?text=${encodeURIComponent(capped)}`);
  assert.equal(wa.getAttribute("rel"), "noopener noreferrer");
  assert.equal(wa.getAttribute("target"), "_blank");
  const mail = e.container.querySelector('[data-key="link:Email it"]');
  assert.equal(mail.getAttribute("href"), `mailto:?subject=${encodeURIComponent("Share")}&body=${encodeURIComponent(capped)}`);
  assert.match(e.container.textContent, /Nothing is sent by Namma Saathi/);
  assert.equal(capped.length, 1500);
});

test("copy uses the clipboard and fails gracefully", async () => {
  const copied = [];
  const ok = makeEnv({ clipboard: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } });
  shareWith(ok, null);
  ok.container.querySelector('[data-key="copy"]').click();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(copied, ["Dinner at 7"]);
  assert.match(ok.container.textContent, /Copied/);
  const bad = makeEnv({ clipboard: { writeText: () => Promise.reject(new Error("denied")) } });
  shareWith(bad, null);
  bad.container.querySelector('[data-key="copy"]').click();
  await new Promise((r) => setImmediate(r));
  assert.match(bad.container.textContent, /Could not copy/);
  const none = makeEnv();
  shareWith(none, null);
  none.container.querySelector('[data-key="copy"]').click();
  assert.match(none.container.textContent, /Could not copy/);
});

test("composer sends edited values and copies when enabled", async () => {
  const copied = [];
  const e = makeEnv({ clipboard: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } });
  e.api.render(e.container, [{ id: "c", type: "composer", channel: "email", to: "a@b.c", subject: "Hi", body: "Body", send: action("Send", "compose.send", "primary"), copy_enabled: true, state: "draft", demo: false }], { onAction: e.onAction });
  e.container.querySelector('[name="body"]').type("Edited body");
  e.container.querySelector('[data-key="send"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "c", actionId: "compose.send", values: { to: "a@b.c", subject: "Hi", body: "Edited body" } });
  e.container.querySelector('[data-key="copy"]').click();
  await new Promise((r) => setImmediate(r));
  assert.equal(copied[0], "To: a@b.c\nSubject: Hi\n\nEdited body");
});

test("reminders: toggle ids, delete, overdue by browser clock, empty state; notes delete", () => {
  const e = makeEnv();
  e.api.render(e.container, [
    { id: "rem", type: "reminder_list", title: "R", items: [
      { id: "r1", text: "old", due: "2020-01-01T09:00:00+05:30", done: false },
      { id: "r2", text: "future", due: "2999-01-01T09:00:00+05:30", done: false },
      { id: "r3", text: "done", due: "2020-01-01T09:00:00+05:30", done: true },
    ] },
    { id: "rem2", type: "reminder_list", title: "R", items: [] },
    { id: "nt", type: "note_list", title: "N", items: [{ id: "n1", body: "b", updated: "2026-11-05T10:00:00+05:30" }] },
  ], { onAction: e.onAction });
  const lis = e.container.children[0].querySelectorAll("li");
  assert.ok(lis[0].className.includes("genui-overdue"));
  assert.ok(!lis[1].className.includes("genui-overdue"));
  assert.ok(!lis[2].className.includes("genui-overdue"));
  assert.match(lis[0].textContent, /Overdue/);
  const cb = e.container.querySelector('[data-key="done:r1"]');
  cb.checked = true; cb.dispatch("change");
  assert.equal(e.actions.pop().actionId, "reminder.done:r1");
  const cb3 = e.container.querySelector('[data-key="done:r3"]');
  cb3.checked = false; cb3.dispatch("change");
  assert.equal(e.actions.pop().actionId, "reminder.undo:r3");
  e.container.querySelector('[data-key="del:r2"]').click();
  assert.equal(e.actions.pop().actionId, "reminder.delete:r2");
  assert.match(e.container.children[1].textContent, /No reminders/);
  e.container.children[2].querySelector("button").click();
  assert.equal(e.actions.pop().actionId, "note.delete:n1");
});

test("table is a real table with column headers; calendar, progress, list, card and panel render", () => {
  const e = makeEnv();
  e.api.render(e.container, [
    { id: "tb", type: "table", columns: ["A", "B"], rows: [["1", "2"], ["3"]] },
    { id: "cal", type: "calendar_view", title: "W", days: [{ date: "2026-11-09", events: [{ id: "e", title: "Ev", start: "10:00", end: "11:00" }] }] },
    { id: "pr", type: "progress", label: "Go", value: 40 },
    { id: "ls", type: "list", items: [], empty_text: "Nothing at all" },
    { id: "cd", type: "card", title: "C", children: [{ id: "cd.t", type: "text", text: "kid" }], actions: [action("Do", "card.do")] },
    { id: "ap", type: "action_panel", title: "P", actions: [action("Go", "p.go", "danger")] },
  ], { onAction: e.onAction });
  const ths = e.container.querySelectorAll("th");
  assert.deepEqual(ths.map((t) => [t.textContent, t.getAttribute("scope")]), [["A", "col"], ["B", "col"]]);
  assert.equal(e.container.querySelectorAll("td").length, 4);
  assert.match(e.container.children[1].textContent, /10:00–11:00/);
  const bar = e.container.querySelector("progress");
  assert.equal(bar.getAttribute("role"), "progressbar");
  assert.equal(bar.getAttribute("aria-valuenow"), "40");
  assert.match(e.container.children[3].textContent, /Nothing at all/);
  assert.match(e.container.children[4].textContent, /kid/);
  e.container.children[4].querySelector("button").click();
  assert.deepEqual(e.actions.pop(), { widgetId: "cd", actionId: "card.do", values: {} });
  e.container.children[5].querySelector("button").click();
  assert.equal(e.actions.pop().actionId, "p.go");
  assert.equal(e.api.validate({ id: "tb", type: "table", columns: ["A"], rows: Array.from({ length: 51 }, () => ["x"]) }).ok, false);
});

test("a throwing onAction never breaks the page", () => {
  const e = makeEnv();
  e.api.render(e.container, [{ id: "ap", type: "action_panel", title: "P", actions: [action("Go", "p.go")] }], { onAction: () => { throw new Error("boom"); } });
  assert.doesNotThrow(() => e.container.querySelector("button").click());
});

/* ---- addendum: Gmail, Calendar, WhatsApp, planning and memory ------------------ */
const ISO_NOW = (offsetMs) => new Date(Date.now() - offsetMs).toISOString();
function emailList(over = {}) {
  return {
    id: "emails", type: "email_list", title: "Emails", empty_text: "No emails.", demo: true,
    items: [
      { id: "18c3F_a-1", sender: "Rahul", subject: "Meet tomorrow?", snippet: "Hi, can we meet", received: ISO_NOW(25 * 60000), unread: true, important: true, needs_reply: true },
      { id: "m2", sender: "Asha", subject: "Old news", snippet: "", received: ISO_NOW(3 * 86400000), unread: false, important: false, needs_reply: false },
    ],
    ...over,
  };
}
function eventPreview(over = {}) {
  return {
    id: "event.t1", type: "event_preview", title: "New event", state: "draft", demo: false, editable: true,
    fields: { title: "Dinner", date: "2026-11-09", start: "19:30", minutes: 90, attendees: ["a@x.io", "b@x.io"] },
    conflicts: [{ title: "Standup", start: "19:00", end: "20:00" }],
    approve: action("Approve", "task.approve:t1", "primary"), cancel: action("Cancel", "task.cancel:t1"), ...over,
  };
}
function planner(over = {}) {
  return {
    id: "planner", type: "day_planner", title: "Your day", date: "2026-11-09", demo: false, editable: true,
    items: [
      { id: "p1", kind: "event", title: "Standup", time: "10:00", done: false, priority: "normal" },
      { id: "p2", kind: "email", title: "Reply to Rahul", done: true, priority: "high" },
      { id: "p3", kind: "task", title: "Pay", done: false, priority: "low", detail: "Before 5" },
    ],
    ...over,
  };
}
function memory(over = {}) {
  return {
    id: "memory", type: "memory_list", title: "What I remember", empty_text: "Nothing yet.", consent_text: "Kept only to help you.",
    items: [{ key: "diet", label: "Diet", value: "vegetarian" }, { key: "home", label: "Home", value: "Indiranagar" }], ...over,
  };
}
function composer(over = {}) {
  return {
    id: "composer.t7", type: "composer", channel: "email", to: "a@b.c", subject: "Hi", body: "Body\nline two", state: "draft", demo: false, editable: true,
    send: action("Send", "task.approve:t7", "primary"), copy_enabled: true, ...over,
  };
}
const within = (e, id) => e.container.children.find((c) => idOf(c) === id);

test("new components validate: demo is required, limits and ids hold", () => {
  const e = makeEnv();
  const v = (w) => e.api.validate(w).ok;
  for (const make of [emailList, eventPreview, planner, composer]) {
    assert.equal(v(make()), true);
    const noDemo = make();
    delete noDemo.demo;
    assert.equal(v(noDemo), false, `${noDemo.type} needs demo`);
  }
  assert.equal(v(memory()), true);
  const mail = (n) => emailList({ items: Array.from({ length: n }, (_, i) => ({ ...emailList().items[0], id: `m${i}` })) });
  assert.equal(v(mail(10)), true);
  assert.equal(v(mail(11)), false);
  assert.equal(v(emailList({ items: [{ ...emailList().items[0], id: "a b" }] })), false);
  assert.equal(v(emailList({ items: [{ ...emailList().items[0], id: "x".repeat(101) }] })), false);
  assert.equal(v(emailList({ items: [emailList().items[0], emailList().items[0]] })), false, "duplicate ids");
  assert.equal(v(emailList({ items: [{ ...emailList().items[0], unread: "yes" }] })), false);
  const ev = (f) => eventPreview({ fields: { ...eventPreview().fields, ...f } });
  assert.equal(v(ev({ attendees: Array(10).fill("a") })), true);
  assert.equal(v(ev({ attendees: Array(11).fill("a") })), false);
  assert.equal(v(ev({ date: "2026-13-45" })), false);
  assert.equal(v(ev({ start: "25:00" })), false);
  assert.equal(v(ev({ minutes: 0 })), false);
  assert.equal(v(ev({ minutes: 1.5 })), false);
  assert.equal(v(eventPreview({ conflicts: Array(11).fill({ title: "x", start: "10:00", end: "11:00" }) })), false);
  assert.equal(v(eventPreview({ state: "sent" })), false);
  const items = (n) => planner({ items: Array.from({ length: n }, (_, i) => ({ id: `i${i}`, kind: "task", title: "t", done: false, priority: "normal" })) });
  assert.equal(v(items(40)), true);
  assert.equal(v(items(41)), false);
  assert.equal(v(planner({ items: [{ ...planner().items[0], id: "A:b" }] })), false);
  assert.equal(v(planner({ items: [{ ...planner().items[0], kind: "meeting" }] })), false);
  assert.equal(v(planner({ items: [{ ...planner().items[0], time: "7pm" }] })), false);
  assert.equal(v(planner({ date: "2026-02-30" })), false);
  const mem = (n) => memory({ items: Array.from({ length: n }, (_, i) => ({ key: `k${i}`, label: "l", value: "v" })) });
  assert.equal(v(mem(30)), true);
  assert.equal(v(mem(31)), false);
  assert.equal(v(memory({ items: [{ key: "Bad Key", label: "l", value: "v" }] })), false);
  assert.equal(v(memory({ demo: true })), false, "memory_list has no demo property");
  assert.equal(v(composer({ state: "queued" })), false);
  assert.equal(v({ id: "c", type: "calendar_view", title: "t", days: [], demo: true }), true);
  assert.equal(v({ id: "c", type: "calendar_view", title: "t", days: [] }), true);
  assert.equal(v({ id: "t", type: "task_board", title: "t", tasks: [], demo: true, summary: "s" }), true);
});

test("email_list renders cards with text chips and posts email.open and email.reply", () => {
  const e = makeEnv();
  e.api.render(e.container, [emailList()], { onAction: e.onAction });
  const text = e.container.textContent;
  assert.match(text, /Rahul/);
  assert.match(text, /Meet tomorrow\?/);
  assert.match(text, /25 min ago/);
  assert.match(text, /3 days ago/);
  assert.match(text, /Unread/);
  assert.match(text, /Important/);
  assert.match(text, /Needs reply/);
  assert.match(text, /Demo/);
  assert.equal(e.container.querySelectorAll("li").length, 2);
  e.container.querySelector('[data-key="open:18c3F_a-1"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "emails", actionId: "email.open:18c3F_a-1", values: {} });
  e.container.querySelector('[data-key="reply:m2"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "emails", actionId: "email.reply:m2", values: {} });
  e.api.render(e.container, [emailList({ items: [], demo: false })], { onAction: e.onAction });
  assert.equal(e.container.children.length, 1);
  assert.match(e.container.textContent, /No emails\./);
  assert.doesNotMatch(e.container.textContent, /Demo/);
});

test("email text from outside stays text", () => {
  const e = makeEnv();
  const hostile = { ...emailList().items[0], sender: "<img src=x onerror=alert(1)>", subject: "<script>alert(1)</script>" };
  e.api.render(e.container, [emailList({ items: [hostile] })], {});
  assert.match(e.container.textContent, /<script>alert\(1\)<\/script>/);
  assert.equal(e.container.querySelectorAll("script").length, 0);
  assert.equal(e.container.querySelectorAll("img").length, 0);
});

test("event_preview posts the edited values with the approve action, once", () => {
  const e = makeEnv();
  e.api.render(e.container, [eventPreview()], { onAction: e.onAction });
  const root = within(e, "event.t1");
  assert.match(root.textContent, /Draft, not created/);
  assert.match(root.textContent, /Standup, 19:00–20:00/);
  const q = (n) => root.querySelector(`[name="${n}"]`);
  assert.equal(q("title").value, "Dinner");
  assert.equal(q("attendees").value, "a@x.io\nb@x.io");
  q("title").type("Dinner at Toit");
  q("date").type("2026-11-10");
  q("start").type("20:00");
  q("minutes").type("120");
  q("attendees").type("a@x.io\n\n  c@x.io  \n");
  const approve = root.querySelector('[data-key="approve"]');
  approve.click();
  approve.click();
  assert.equal(e.actions.length, 1);
  assert.deepEqual(e.actions[0], {
    widgetId: "event.t1", actionId: "task.approve:t1",
    values: { title: "Dinner at Toit", date: "2026-11-10", start: "20:00", minutes: 120, attendees: ["a@x.io", "c@x.io"] },
  });
  assert.equal(root.querySelector('[data-key="cancel"]').disabled, true, "both rest until replaced");
});

test("event_preview refuses to post invalid values and cancel posts the cancel action", () => {
  const e = makeEnv();
  e.api.render(e.container, [eventPreview({ conflicts: [] })], { onAction: e.onAction });
  const root = within(e, "event.t1");
  assert.doesNotMatch(root.textContent, /overlaps/);
  const q = (n) => root.querySelector(`[name="${n}"]`);
  q("title").type("   ");
  q("minutes").type("2.5");
  q("attendees").type(Array.from({ length: 11 }, (_, i) => `p${i}`).join("\n"));
  root.querySelector('[data-key="approve"]').click();
  assert.equal(e.actions.length, 0);
  assert.equal(q("title").getAttribute("aria-invalid"), "true");
  assert.equal(q("minutes").getAttribute("aria-invalid"), "true");
  assert.equal(q("attendees").getAttribute("aria-invalid"), "true");
  assert.equal(root.querySelector('[data-key="approve"]').disabled, false);
  root.querySelector('[data-key="cancel"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "event.t1", actionId: "task.cancel:t1", values: {} });
});

test("event_preview after created, failed or cancelled is read-only and shows detail", () => {
  const e = makeEnv();
  for (const [state, text, detail] of [["created", /Created/, "Added to your calendar"], ["failed", /Failed/, "Calendar refused"], ["cancelled", /Cancelled/, "You cancelled"]]) {
    e.api.render(e.container, [eventPreview({ state, detail })], { onAction: e.onAction });
    const root = within(e, "event.t1");
    assert.equal(root.querySelectorAll("input").length, 0);
    assert.equal(root.querySelectorAll("textarea").length, 0);
    assert.equal(root.querySelectorAll("button").length, 0);
    assert.match(root.textContent, text);
    assert.match(root.textContent, new RegExp(detail));
    assert.match(root.textContent, /Dinner/);
  }
  assert.equal(e.container.children.length, 1, "replaced in place by id");
  e.api.render(e.container, [eventPreview({ state: "created", demo: true })], {});
  assert.match(e.container.textContent, /Demo/);
  assert.match(e.container.textContent, /not really created/);
  assert.doesNotMatch(e.container.textContent.replace(/not really created/, ""), /\bCreated\b/);
});

test("event_preview keeps typed text and focus when the server replaces it", () => {
  const e = makeEnv();
  e.api.render(e.container, [eventPreview()], { onAction: e.onAction });
  const title = within(e, "event.t1").querySelector('[name="title"]');
  title.type("My edit");
  title.focus();
  e.api.render(e.container, [eventPreview({ detail: "Checked your calendar", conflicts: [] })], { onAction: e.onAction });
  const again = within(e, "event.t1").querySelector('[name="title"]');
  assert.notEqual(again, title);
  assert.equal(again.value, "My edit");
  assert.equal(e.doc.activeElement, again);
});

test("day_planner posts toggle, priority, move and add with the contract values", () => {
  const e = makeEnv();
  e.api.render(e.container, [planner()], { onAction: e.onAction });
  const root = within(e, "planner");
  assert.match(root.textContent, /1 of 3 done/);
  assert.match(root.textContent, /Event/);
  assert.match(root.textContent, /Email/);
  assert.match(root.textContent, /Task/);
  assert.match(root.textContent, /10:00/);
  assert.match(root.textContent, /High priority/);
  const cb = root.querySelector('[data-key="toggle:p1"]');
  cb.checked = true;
  cb.dispatch("change");
  assert.deepEqual(e.actions.pop(), { widgetId: "planner", actionId: "plan.toggle:p1", values: {} });
  const sel = root.querySelector('[data-key="priority:p1"]');
  sel.value = "high";
  sel.dispatch("change");
  assert.deepEqual(e.actions.pop(), { widgetId: "planner", actionId: "plan.priority:p1", values: { priority: "high" } });
  root.querySelector('[data-key="down:p1"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "planner", actionId: "plan.move:p1", values: { direction: "down" } });
  root.querySelector('[data-key="up:p2"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "planner", actionId: "plan.move:p2", values: { direction: "up" } });
  assert.equal(root.querySelector('[data-key="up:p1"]').disabled, true, "first item cannot move up");
  assert.equal(root.querySelector('[data-key="down:p3"]').disabled, true, "last item cannot move down");
  root.querySelector('[data-key="up:p1"]').click();
  root.querySelector('[data-key="down:p3"]').click();
  assert.equal(e.actions.length, 0);
});

test("day_planner add form validates, omits an empty time and clears after posting", () => {
  const e = makeEnv();
  e.api.render(e.container, [planner()], { onAction: e.onAction });
  const root = within(e, "planner");
  const add = root.querySelector('[data-key="add"]');
  add.click();
  assert.equal(e.actions.length, 0, "empty title is refused");
  const title = root.querySelector('[name="title"]');
  assert.equal(title.getAttribute("aria-invalid"), "true");
  title.type("  Call Ajji ");
  add.click();
  assert.deepEqual(e.actions.pop(), { widgetId: "planner", actionId: "plan.add", values: { title: "Call Ajji" } });
  assert.equal(title.value, "");
  title.type("Lunch");
  root.querySelector('[name="time"]').type("13:00");
  add.click();
  assert.deepEqual(e.actions.pop(), { widgetId: "planner", actionId: "plan.add", values: { title: "Lunch", time: "13:00" } });
  title.type("half typed");
  e.api.render(e.container, [planner({ items: [] })], { onAction: e.onAction });
  assert.equal(within(e, "planner").querySelector('[name="title"]').value, "half typed", "typed text survives replace");
  assert.match(within(e, "planner").textContent, /Nothing planned yet/);
});

test("day_planner read-only has no controls and says done or to do in text", () => {
  const e = makeEnv();
  e.api.render(e.container, [planner({ editable: false, demo: true })], { onAction: e.onAction });
  const root = within(e, "planner");
  assert.equal(root.querySelectorAll("button").length, 0);
  assert.equal(root.querySelectorAll("input").length, 0);
  assert.match(root.textContent, /Done/);
  assert.match(root.textContent, /To do/);
  assert.match(root.textContent, /Demo/);
});

test("memory_list shows consent, forgets by key and saves new values", () => {
  const e = makeEnv();
  e.api.render(e.container, [memory()], { onAction: e.onAction });
  const root = within(e, "memory");
  assert.match(root.textContent, /Kept only to help you\./);
  assert.match(root.textContent, /vegetarian/);
  root.querySelector('[data-key="forget:diet"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "memory", actionId: "memory.forget:diet", values: {} });
  assert.equal(root.querySelector('[data-key="forget:diet"]').disabled, true, "forget rests until replaced");
  const input = root.querySelector('[name="value_home"]');
  root.querySelector('[data-key="save:home"]').click();
  assert.equal(e.actions.length, 0, "empty value is refused");
  assert.equal(input.getAttribute("aria-invalid"), "true");
  input.type(" Koramangala ");
  root.querySelector('[data-key="save:home"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "memory", actionId: "memory.save", values: { key: "home", value: "Koramangala" } });
  assert.equal(input.value, "");
  e.api.render(e.container, [memory({ items: [] })], {});
  assert.match(e.container.textContent, /Nothing yet/);
  assert.match(e.container.textContent, /Kept only to help you/);
});

test("task_board approve, view and edit post their ids; approve fires once", () => {
  const e = makeEnv();
  const board = (over = {}) => ({
    id: "tasks", type: "task_board", title: "Tasks", demo: true, summary: "1 needs you", tasks: [
      { id: "a1", title: "Email Asha", status: "waiting_for_approval", approve_action_id: "task.approve:a1", view_action_id: "task.view:a1", edit_action_id: "task.edit:a1", cancel_action_id: "task.cancel:a1" },
      { id: "a2", title: "Running", status: "running", approve_action_id: "task.approve:a2", view_action_id: "task.view:a2" },
      { id: "a3", title: "No ids", status: "waiting_for_approval" },
    ], ...over,
  });
  e.api.render(e.container, [board()], { onAction: e.onAction });
  const root = e.container;
  assert.deepEqual(root.querySelectorAll("button").map((b) => b.getAttribute("data-key")), ["approve:a1", "view:a1", "edit:a1", "cancel:a1", "view:a2"]);
  assert.match(root.textContent, /Demo/);
  assert.match(root.textContent, /1 needs you/);
  assert.match(root.textContent, /Needs your approval/);
  assert.match(root.textContent, /Running/);
  assert.equal(root.children[0].children[0].getAttribute("aria-live"), "polite");
  const approve = root.querySelector('[data-key="approve:a1"]');
  approve.click();
  approve.click();
  assert.deepEqual(e.actions.splice(0), [{ widgetId: "tasks", actionId: "task.approve:a1", values: { task_id: "a1" } }]);
  assert.equal(approve.disabled, true);
  root.querySelector('[data-key="view:a1"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "tasks", actionId: "task.view:a1", values: { task_id: "a1" } });
  root.querySelector('[data-key="edit:a1"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "tasks", actionId: "task.edit:a1", values: { task_id: "a1" } });
  e.api.render(e.container, [board({ summary: "done" })], { onAction: e.onAction });
  assert.equal(e.container.querySelector('[data-key="approve:a1"]').disabled, false, "replaced board is live again");
  assert.equal(e.container.children.length, 1);
});

test("composer states look different and carry text", () => {
  const e = makeEnv();
  const seen = {};
  for (const [state, expected] of [["draft", /Draft, not sent/], ["copied", /Copied, not sent/], ["sent", /Sent/], ["failed", /Failed/]]) {
    e.api.render(e.container, [composer({ state, detail: `detail for ${state}` })], { onAction: e.onAction });
    const root = within(e, "composer.t7");
    assert.match(root.textContent, expected);
    assert.match(root.textContent, new RegExp(`detail for ${state}`));
    seen[state] = root.querySelector("div").className;
    assert.equal(root.querySelectorAll("textarea").length, state === "draft" ? 1 : 0, `${state} editable only as draft`);
    assert.equal(!!root.querySelector('[data-key="send"]'), state === "draft");
    assert.ok(root.querySelector('[data-key="copy"]'), "copy stays available as a fallback");
  }
  assert.equal(new Set(Object.values(seen)).size, 4, "four distinct looks");
  assert.equal(e.container.children.length, 1);
  e.api.render(e.container, [composer({ state: "sent", demo: true, detail: "Pretend delivery" })], {});
  assert.match(e.container.textContent, /Demo: not really sent/);
  assert.doesNotMatch(e.container.textContent.replace(/not really sent/g, ""), /\bSent\b/);
  assert.match(e.container.textContent, /Body\nline two/);
});

test("composer Send posts edited values once, saves edits, and rests until replaced", () => {
  const e = makeEnv();
  e.api.render(e.container, [composer()], { onAction: e.onAction });
  const root = within(e, "composer.t7");
  root.querySelector('[name="body"]').type("Edited");
  root.querySelector('[name="subject"]').type("New subject");
  root.querySelector('[data-key="save"]').click();
  assert.deepEqual(e.actions.pop(), { widgetId: "composer.t7", actionId: "task.edit:t7", values: { to: "a@b.c", subject: "New subject", body: "Edited" } });
  const send = root.querySelector('[data-key="send"]');
  send.click();
  send.click();
  send.dispatch("click");
  assert.deepEqual(e.actions.splice(0), [{ widgetId: "composer.t7", actionId: "task.approve:t7", values: { to: "a@b.c", subject: "New subject", body: "Edited" } }]);
  assert.equal(send.disabled, true);
  e.api.render(e.container, [composer({ state: "draft", detail: "again" })], { onAction: e.onAction });
  const again = within(e, "composer.t7");
  assert.equal(again.querySelector('[data-key="send"]').disabled, false);
  assert.equal(again.querySelector('[name="body"]').value, "Edited", "typed text survives replace");
});

test("composer Send re-enables if onAction rejects; copy fails gracefully", async () => {
  const e = makeEnv();
  e.api.render(e.container, [composer()], { onAction: () => Promise.reject(new Error("net")) });
  const send = e.container.querySelector('[data-key="send"]');
  send.click();
  assert.equal(send.disabled, true);
  await new Promise((r) => setImmediate(r));
  assert.equal(send.disabled, false);
  e.container.querySelector('[data-key="copy"]').click();
  assert.match(e.container.textContent, /Could not copy/);
  const ok = [];
  const c = makeEnv({ clipboard: { writeText: (t) => { ok.push(t); return Promise.resolve(); } } });
  c.api.render(c.container, [composer({ state: "failed", detail: "x", editable: false })], {});
  c.container.querySelector('[data-key="copy"]').click();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(ok, ["To: a@b.c\nSubject: Hi\n\nBody\nline two"]);
});

test("calendar_view shows a Demo badge only when demo is true", () => {
  const e = makeEnv();
  const cal = (demo) => ({ id: "calendar", type: "calendar_view", title: "Week", demo, days: [{ date: "2026-11-09", events: [{ id: "e", title: "Ev", start: "10:00", end: "11:00" }] }] });
  e.api.render(e.container, [cal(true)], {});
  assert.match(e.container.textContent, /Demo/);
  e.api.render(e.container, [cal(false)], {});
  assert.doesNotMatch(e.container.textContent, /Demo/);
  assert.equal(e.container.children.length, 1);
});

test("the extended source still avoids every forbidden API", () => {
  assert.doesNotMatch(SOURCE, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function|\.style\b|srcdoc/);
  assert.doesNotMatch(SOURCE, /setAttribute\(\s*["'](style|on)/i);
});
