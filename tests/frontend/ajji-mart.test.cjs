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
  "#new-session-button", "#page-status", "#language-status", "#delivery-address", "#address-help",
  "#commerce-badge", "#commerce-note", "#confirmation-intro", "#cancel-button", "#options-panel",
  "#options-list", "#order-panel", "#order-result", "#order-ids", "#catalogue-note",
];
const initialCart = [
  { name: "Sona Masoori Rice", pack_size: "1 kg", quantity: 2, unit_price_inr: 78, line_total_inr: 156 },
];
function payload(overrides = {}) {
  return {
    session_id: "session-123", language: "kn-IN", state: "READBACK", cart: initialCart,
    total_inr: 156, cart_version: 4, cart_hash: "fresh-hash", can_confirm: true, show_readback: true,
    commerce_mode: "mock", checkout_enabled: true, options: [], order_ids: [],
    address_label: "Home", payment_method: "COD", assistant_text: "Please check your basket.",
    transcript: null, audio_base64: null, ...overrides,
  };
}
function makeApp({ savedSession = null, getPayload = payload(), postPayload = payload(), holdTurn = false, actionPayload = payload({ state: "DONE", can_confirm: false, show_readback: false, cart: initialCart }) } = {}) {
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
    else if (url === "/api/addresses") data = { commerce_mode: getPayload.commerce_mode, checkout_enabled: true, addresses: [{ id: "home", label: "Home", full_address: "1 Road" }] };
    else if (url === "/api/session/action") data = actionPayload;
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
    Headers: class {
      constructor(initial = {}) { this.values = { ...initial }; }
      set(key, value) { this.values[key] = value; }
    },
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

async function recordTurn(app) {
  await app.elements["#record-button"].click();
  await tick();
  app.processors.at(-1).onaudioprocess({
    inputBuffer: { getChannelData: () => new Float32Array([0.2, 0.1]) },
    outputBuffer: { getChannelData: () => new Float32Array(2) },
  });
  app.elements["#record-button"].click();
  await tick();
  await tick();
  return app.calls.filter((call) => call.url.startsWith("/api/session/turn")).at(-1);
}

test("a spoken turn tells the server which readback this page received", async () => {
  const app = makeApp({ savedSession: "session-123" });
  await tick();
  const turn = await recordTurn(app);
  assert.equal(turn.options.headers.values["X-Heard-Cart-Version"], "4");
  assert.equal(turn.options.headers.values["X-Heard-Cart-Hash"], "fresh-hash");
});

test("no readback is claimed when the cart cannot be confirmed", async () => {
  const app = makeApp({ savedSession: "session-123", getPayload: payload({ can_confirm: false }) });
  await tick();
  const turn = await recordTurn(app);
  assert.equal(turn.options.headers.values["X-Heard-Cart-Hash"], undefined);
  assert.equal(app.elements["#confirm-button"].disabled, true);
});

test("options are shown as buttons and tapping one sends only its index", async () => {
  const options = [
    { name: "Amul Milk", pack_size: "500 ml", quantity: 1, unit_price_inr: 30 },
    { name: "Nandini Milk", pack_size: "1 ltr", quantity: 2, unit_price_inr: 58 },
  ];
  const app = makeApp({
    savedSession: "session-123",
    getPayload: payload({ state: "CLARIFYING", options, can_confirm: false, show_readback: false }),
  });
  await tick();
  assert.equal(app.elements["#options-panel"].hidden, false);
  const buttons = app.elements["#options-list"].children;
  assert.equal(buttons.length, 2);
  assert.match(buttons[1].textContent, /2 × Nandini Milk, 1 ltr — ₹58/);
  await buttons[1].click();
  const action = app.calls.find((call) => call.url === "/api/session/action");
  assert.deepEqual(JSON.parse(action.options.body), { action: "select_option", option_index: 1 });
});

test("a real order never shows demo wording", async () => {
  const app = makeApp({
    savedSession: "session-123",
    getPayload: payload({
      commerce_mode: "swiggy", state: "DONE", can_confirm: false, show_readback: false,
      order_ids: ["O123"], order_verified: true, order_status: "PLACED",
    }),
  });
  await tick();
  assert.equal(app.elements["#order-panel"].hidden, false);
  assert.match(app.elements["#order-result"].textContent, /PLACED/);
  assert.match(app.elements["#order-ids"].textContent, /O123/);
  assert.doesNotMatch(app.elements["#order-result"].textContent, /demo/i);
  assert.equal(app.elements["#state-label"].textContent, "Order placed");
  assert.doesNotMatch(app.elements["#catalogue-note"].textContent, /demo/i);
});

test("a demo order says it was not real", async () => {
  const app = makeApp({
    savedSession: "session-123",
    getPayload: payload({ state: "DONE", can_confirm: false, show_readback: false, order_ids: ["IM1"], order_verified: true }),
  });
  await tick();
  assert.match(app.elements["#order-result"].textContent, /No real order/);
  assert.equal(app.elements["#state-label"].textContent, "Demo order placed");
});

test("an unknown order warns against ordering again and offers no confirm", async () => {
  const app = makeApp({
    savedSession: "session-123",
    getPayload: payload({ commerce_mode: "swiggy", state: "ORDER_UNKNOWN", can_confirm: false, show_readback: false }),
  });
  await tick();
  assert.equal(app.elements["#order-panel"].hidden, false);
  assert.match(app.elements["#order-result"].textContent, /do not order again/);
  assert.equal(app.elements["#confirm-button"].disabled, true);
  assert.equal(app.elements["#confirmation-panel"].hidden, true);
});

test("cancel is available at readback and sends a cancel action", async () => {
  const app = makeApp({ savedSession: "session-123" });
  await tick();
  assert.equal(app.elements["#cancel-button"].disabled, false);
  await app.elements["#cancel-button"].click();
  const action = app.calls.find((call) => call.url === "/api/session/action");
  assert.deepEqual(JSON.parse(action.options.body), { action: "cancel" });
});

test("a restored page shows the last reply it may have missed", async () => {
  const app = makeApp({
    savedSession: "session-123",
    getPayload: payload({ state: "DONE", can_confirm: false, show_readback: false, last_reply: "Your order is placed." }),
  });
  await tick();
  assert.equal(app.elements["#assistant-text"].textContent, "Your order is placed.");
});
