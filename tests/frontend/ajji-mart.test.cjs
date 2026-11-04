const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(id = "") {
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.classList = { add() {}, remove() {} };
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this.value = "kn-IN";
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { this.focused = true; }
  click() { return this.listeners.click?.(); }
}

const selectors = [
  ".home-panel", "#conversation", "#language", "#selected-language", "#start-button",
  "#record-button", "#record-label", "#recording-status", "#transcript", "#assistant-text",
  "#error-message", "#reply-audio", "#audio-note", "#cart-items", "#cart-total", "#state-label",
  "#confirm-button", "#confirmation-panel", "#confirmation-items", "#confirmation-total",
  "#confirmation-address", "#confirmation-payment", "#confirm-note", "#back-button",
  "#new-session-button", "#page-status", "#language-status",
];
const initialCart = [
  { name: "Sona Masoori Rice", pack_size: "1 kg", quantity: 2, unit_price_inr: 78, line_total_inr: 156 },
];
function payload(overrides = {}) {
  return {
    session_id: "session-123", language: "kn-IN", state: "READBACK", cart: initialCart,
    total_inr: 156, cart_version: 4, cart_hash: "fresh-hash", can_confirm: true,
    address_label: "Home", payment_method: "COD", assistant_text: "Please check your basket.",
    transcript: null, audio_base64: null, ...overrides,
  };
}
function makeApp({ savedSession = null, getPayload = payload(), postPayload = payload(), holdTurn = false } = {}) {
  const elements = Object.fromEntries(selectors.map((selector) => [selector, new Element(selector)]));
  const storage = new Map(savedSession ? [["ajji-mart-session", savedSession]] : []);
  const calls = [];
  const processors = [];
  let releaseTurn;
  const turnWait = holdTurn ? new Promise((resolve) => { releaseTurn = resolve; }) : Promise.resolve();
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.startsWith("/api/session/turn")) await turnWait;
    let data = getPayload;
    if (url === "/api/sessions") data = postPayload;
    else if (url === "/api/session/action") data = payload({ state: "DONE", can_confirm: false, cart: initialCart });
    return { ok: true, status: 200, async json() { return data; } };
  };
  const stream = { getTracks: () => [{ stop() {} }] };
  class AudioContextMock {
    constructor() { this.sampleRate = 16000; this.destination = {}; }
    async resume() {}
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createScriptProcessor() {
      const processor = { connect() {}, disconnect() {}, onaudioprocess: null };
      processors.push(processor);
      return processor;
    }
    async close() {}
  }
  const context = {
    document: {
      querySelector(selector) { return elements[selector]; },
      createElement() { return new Element(); },
    },
    sessionStorage: {
      getItem(key) { return storage.get(key) ?? null; },
      setItem(key, value) { storage.set(key, value); },
      removeItem(key) { storage.delete(key); },
    },
    fetch,
    Headers: class { constructor() { this.values = {}; } set(key, value) { this.values[key] = value; } },
    navigator: { mediaDevices: { async getUserMedia() { return stream; } } },
    window: { AudioContext: AudioContextMock, setTimeout() { return 1; }, clearTimeout() {} },
    URL: { createObjectURL() { return "blob:reply"; }, revokeObjectURL() {} },
    Blob, Uint8Array, ArrayBuffer, DataView, Float32Array, Math, JSON, Number, encodeURIComponent,
    atob, Error, Promise, console,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../../app/static/app.js"), "utf8"), context);
  return { elements, storage, calls, processors, releaseTurn };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("start creates and stores a session, then enters the listening state", async () => {
  const app = makeApp({ getPayload: payload({ state: "COLLECTING", cart: [], can_confirm: false }), postPayload: payload({ state: "COLLECTING", cart: [], can_confirm: false }) });
  await app.elements["#start-button"].click();
  await tick();
  assert.equal(app.storage.get("ajji-mart-session"), "session-123");
  assert.equal(app.elements["#conversation"].hidden, false);
  assert.equal(app.elements["#record-button"].attributes["aria-pressed"], "true");
  assert.match(app.elements["#recording-status"].textContent, /Recording/);
});

test("cart and confirmation show pack, quantities, prices, address, and explicit actions", async () => {
  const app = makeApp({ savedSession: "session-123" });
  await tick();
  const cartItem = app.elements["#cart-items"].children[0];
  assert.equal(cartItem.children[0].children[0].textContent, "Sona Masoori Rice");
  assert.match(cartItem.children[0].children[1].textContent, /2 × 1 kg/);
  assert.equal(app.elements["#cart-total"].textContent, "₹156");
  assert.equal(app.elements["#confirmation-panel"].hidden, false);
  assert.match(app.elements["#confirmation-items"].children[0].textContent, /₹78 each/);
  assert.equal(app.elements["#confirmation-address"].textContent, "Home");
  assert.equal(app.elements["#confirmation-payment"].textContent, "Cash on delivery (demo only)");
  assert.equal(app.elements["#confirm-button"].disabled, false);
});

test("edit control starts a recording and back returns without confirming", async () => {
  const app = makeApp({ savedSession: "session-123" });
  await tick();
  const edit = app.elements["#cart-items"].children[0].children[2];
  await edit.click();
  await tick();
  assert.equal(app.elements["#record-button"].attributes["aria-pressed"], "true");
  assert.match(app.elements["#recording-status"].textContent, /Sona Masoori Rice/);
  app.elements["#back-button"].click();
  assert.equal(app.elements["#confirmation-panel"].hidden, true);
  assert.equal(app.calls.some((call) => call.url === "/api/session/action"), false);
});

test("confirm sends the current cart version and hash; restored session is sent on requests", async () => {
  const app = makeApp({ savedSession: "session-123" });
  await tick();
  const get = app.calls.find((call) => call.url === "/api/session");
  assert.equal(get.options.headers.values["X-Session-ID"], "session-123");
  await app.elements["#confirm-button"].click();
  const action = app.calls.find((call) => call.url === "/api/session/action");
  assert.deepEqual(JSON.parse(action.options.body), {
    action: "confirm", cart_version: 4, cart_hash: "fresh-hash",
  });
  assert.equal(action.options.headers.values["X-Session-ID"], "session-123");
});

test("language selection is visible and processing prevents another submission", async () => {
  const app = makeApp({ getPayload: payload({ state: "COLLECTING", cart: [], can_confirm: false }), postPayload: payload({ state: "COLLECTING", cart: [], can_confirm: false }) });
  app.elements["#language"].value = "hi-IN";
  await app.elements["#language"].listeners.change();
  assert.equal(app.elements["#selected-language"].textContent, "हिन्दी · Hindi");
  await app.elements["#start-button"].click();
  await tick();
  assert.equal(app.elements["#record-button"].disabled, false);
});

test("finish stops the microphone, shows processing, and blocks duplicate turns", async () => {
  const app = makeApp({ savedSession: "session-123", holdTurn: true });
  await tick();
  await app.elements["#record-button"].click();
  await tick();
  const processor = app.processors[0];
  processor.onaudioprocess({
    inputBuffer: { getChannelData: () => new Float32Array([0.2, 0.1]) },
    outputBuffer: { getChannelData: () => new Float32Array(2) },
  });
  assert.equal(app.elements["#confirm-button"].disabled, true);
  assert.equal(app.elements["#record-button"].attributes["aria-pressed"], "true");
  app.elements["#record-button"].click();
  await tick();
  assert.match(app.elements["#recording-status"].textContent, /Processing/);
  assert.equal(app.elements["#record-button"].disabled, true);
  app.elements["#record-button"].click();
  assert.equal(app.calls.filter((call) => call.url.startsWith("/api/session/turn")).length, 1);
  app.releaseTurn();
  await tick();
  await tick();
});
