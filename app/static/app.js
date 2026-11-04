"use strict";

const MAX_RECORDING_MS = 30_000;
const SESSION_STORAGE_KEY = "ajji-mart-session";

const startPanel = document.querySelector(".home-panel");
const conversation = document.querySelector("#conversation");
const languageSelect = document.querySelector("#language");
const addressSelect = document.querySelector("#delivery-address");
const selectedLanguage = document.querySelector("#selected-language");
const startButton = document.querySelector("#start-button");
const recordButton = document.querySelector("#record-button");
const recordLabel = document.querySelector("#record-label");
const recordingStatus = document.querySelector("#recording-status");
const transcriptNode = document.querySelector("#transcript");
const assistantNode = document.querySelector("#assistant-text");
const errorNode = document.querySelector("#error-message");
const audioNode = document.querySelector("#reply-audio");
const audioNote = document.querySelector("#audio-note");
const cartNode = document.querySelector("#cart-items");
const cartTotalNode = document.querySelector("#cart-total");
const stateNode = document.querySelector("#state-label");
const confirmButton = document.querySelector("#confirm-button");
const confirmationPanel = document.querySelector("#confirmation-panel");
const confirmationItems = document.querySelector("#confirmation-items");
const confirmationTotal = document.querySelector("#confirmation-total");
const confirmationAddress = document.querySelector("#confirmation-address");
const confirmationPayment = document.querySelector("#confirmation-payment");
const confirmNote = document.querySelector("#confirm-note");
const backButton = document.querySelector("#back-button");
const newSessionButton = document.querySelector("#new-session-button");
const pageStatus = document.querySelector("#page-status");
const commerceBadge = document.querySelector("#commerce-badge");
const commerceNote = document.querySelector("#commerce-note");
const confirmationIntro = document.querySelector("#confirmation-intro");
const cancelButton = document.querySelector("#cancel-button");
const optionsPanel = document.querySelector("#options-panel");
const optionsList = document.querySelector("#options-list");
const orderPanel = document.querySelector("#order-panel");
const orderResult = document.querySelector("#order-result");
const orderIds = document.querySelector("#order-ids");
const catalogueNote = document.querySelector("#catalogue-note");

let sessionId = sessionStorage.getItem(SESSION_STORAGE_KEY);
let recorder = null;
let busy = false;
let activeAudioUrl = null;
let microphonePending = false;
let recordingInstruction = "";
// The readback this page actually received. A spoken yes confirms this cart, not the latest one.
let heardReadback = null;

function setBusy(value) {
  busy = value;
  startButton.disabled = value || !addressSelect.value;
  recordButton.disabled = value || !sessionId;
  confirmButton.disabled = value || confirmButton.dataset.allowed !== "true";
  newSessionButton.disabled = value;
}

function showError(message) {
  errorNode.textContent = message;
  errorNode.hidden = !message;
}

function showConversation() {
  startPanel.hidden = true;
  conversation.hidden = false;
  recordButton.disabled = !sessionId;
}

function displayLanguage(code) {
  return ({ "kn-IN": "ಕನ್ನಡ · Kannada", "hi-IN": "हिन्दी · Hindi", "en-IN": "English" })[code] || code;
}

languageSelect.addEventListener("change", () => {
  selectedLanguage.textContent = displayLanguage(languageSelect.value);
});

function renderCart(data) {
  cartNode.replaceChildren();
  if (!data.cart.length) {
    const empty = document.createElement("li");
    empty.className = "empty-cart";
    empty.textContent = "No items yet.";
    cartNode.append(empty);
  } else {
    for (const line of data.cart) {
      const item = document.createElement("li");
      item.className = "cart-line";
      const description = document.createElement("span");
      const name = document.createElement("span");
      name.className = "cart-line-name";
      name.textContent = line.name;
      const details = document.createElement("span");
      details.className = "cart-line-details";
      details.textContent = `${line.quantity} × ${line.pack_size} · ₹${line.unit_price_inr} each`;
      description.append(name, details);
      const lineTotal = document.createElement("span");
      lineTotal.className = "cart-line-total";
      lineTotal.textContent = `₹${line.line_total_inr}`;
      const editButton = document.createElement("button");
      editButton.className = "line-edit-button";
      editButton.type = "button";
      editButton.disabled = ["CONFIRMED", "PLACING", "VERIFYING", "FAILED", "DONE", "ORDER_UNKNOWN"].includes(data.state);
      editButton.textContent = "Change / remove";
      editButton.setAttribute("aria-label", `Change or remove ${line.name}`);
      editButton.addEventListener("click", () => {
        recordingInstruction = `Say what you would like to change about ${line.name}, or ask Ajji to remove it.`;
        beginRecording();
      });
      item.append(description, lineTotal, editButton);
      cartNode.append(item);
    }
  }
  cartTotalNode.textContent = `₹${data.total_inr}`;
  const isMock = data.commerce_mode !== "swiggy";
  const stateLabels = {
    COLLECTING: "Ready for your request",
    CLARIFYING: "Ajji has a question",
    READBACK: "Please review",
    CONFIRMED: "Confirmed",
    PLACING: isMock ? "Placing demo order" : "Placing your order",
    VERIFYING: "Checking your order",
    DONE: isMock ? "Demo order placed" : "Order placed",
    FAILED: "Checking your order",
    ORDER_UNKNOWN: "Order status unknown",
  };
  stateNode.textContent = stateLabels[data.state] || "Conversation in progress";
  confirmButton.dataset.version = data.cart_version;
  confirmButton.dataset.hash = data.cart_hash;
  const showReadback = data.show_readback === true && data.state === "READBACK";
  const canConfirm = data.can_confirm === true && showReadback;
  heardReadback = canConfirm ? { version: data.cart_version, hash: data.cart_hash } : null;
  renderOptions(data);
  renderOrder(data, isMock);
  cancelButton.disabled = !showReadback;
  confirmButton.dataset.allowed = canConfirm ? "true" : "false";
  confirmationPanel.hidden = !showReadback;
  confirmationItems.replaceChildren();
  if (showReadback) {
    for (const line of data.cart) {
      const item = document.createElement("li");
      item.textContent = `${line.quantity} × ${line.name}, ${line.pack_size} — ₹${line.unit_price_inr} each, ₹${line.line_total_inr} total`;
      confirmationItems.append(item);
    }
    const bill = [
      data.savings_inr ? `saves ₹${data.savings_inr}` : "",
      data.fees_inr ? `fees ₹${data.fees_inr}` : "",
    ].filter(Boolean).join(", ");
    confirmationTotal.textContent = bill ? `₹${data.total_inr} (${bill})` : `₹${data.total_inr}`;
    confirmationAddress.textContent = data.delivery_address || data.address_label || "No delivery address is set";
    const payment = data.payment_method === "COD"
      ? "Cash on delivery"
      : data.payment_method || "Payment method not set";
    confirmationPayment.textContent = isMock ? `${payment} (demo only)` : payment;
    if (data.commerce_mode === "swiggy") {
      catalogueNote.textContent = "Prices and pack sizes come from Swiggy Instamart.";
      commerceBadge.textContent = "Swiggy live cart";
      commerceNote.textContent = data.checkout_enabled
        ? "Live Swiggy access is enabled. Review every detail before confirming."
        : "Live product and cart access only. Checkout is disabled.";
      confirmationIntro.textContent = data.checkout_enabled
        ? "Ajji will submit this order to Swiggy only after you explicitly confirm."
        : "Live checkout is disabled. This readback will not place an order.";
      confirmButton.textContent = data.checkout_enabled ? "Confirm real Swiggy order" : "Swiggy checkout disabled";
    } else {
      commerceBadge.textContent = "Demo order";
      commerceNote.textContent = "This is a demo. Orders are not sent to a shop.";
      confirmationIntro.textContent = "Ajji will place this demo order only after you choose Confirm order.";
      confirmButton.textContent = "Confirm demo order";
    }
  }
  confirmButton.disabled = busy || !canConfirm;
  confirmNote.textContent = "Please review every item, the total, address, and payment method.";
}

function renderOptions(data) {
  const options = data.options || [];
  optionsPanel.hidden = !(data.state === "CLARIFYING" && options.length > 0);
  optionsList.replaceChildren();
  options.forEach((option, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button";
    const quantity = option.quantity > 1 ? `${option.quantity} × ` : "";
    button.textContent = `${index + 1}. ${quantity}${option.name}, ${option.pack_size} — ₹${option.unit_price_inr}`;
    button.addEventListener("click", () => sendChoice(index));
    optionsList.append(button);
  });
}

function renderOrder(data, isMock) {
  const ids = data.order_ids || [];
  const unknown = data.state === "ORDER_UNKNOWN";
  orderPanel.hidden = !(unknown || (data.state === "DONE" && ids.length > 0));
  if (unknown) {
    orderResult.textContent = "Ajji could not confirm this order. Please do not order again. Check the Swiggy app.";
  } else if (isMock) {
    orderResult.textContent = "This was a demo. No real order was placed.";
  } else if (data.order_partial) {
    orderResult.textContent = "Only part of this order was placed. Please check the Swiggy app.";
  } else if (data.order_verified) {
    orderResult.textContent = `Swiggy shows this order as: ${data.order_status || "placed"}.`;
  } else {
    orderResult.textContent = "Swiggy reported this order, but Ajji could not check it. Please look in the Swiggy app.";
  }
  orderIds.textContent = ids.length ? `Order: ${ids.join(", ")}` : "";
}

function renderReply(data) {
  if (data.transcript !== null && data.transcript !== undefined) {
    transcriptNode.textContent = data.transcript || "No speech was recognized.";
  }
  assistantNode.textContent = data.assistant_text || "I am ready for your next turn.";
  renderCart(data);
  document.querySelector("#language-status").textContent = `${displayLanguage(data.language || languageSelect.value)} selected`;
  audioNode.hidden = true;
  audioNote.hidden = true;
  audioNode.removeAttribute("src");
  if (activeAudioUrl) URL.revokeObjectURL(activeAudioUrl);
  activeAudioUrl = null;
  if (data.audio_base64) {
    const raw = atob(data.audio_base64);
    const bytes = new Uint8Array(raw.length);
    for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
    activeAudioUrl = URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
    audioNode.src = activeAudioUrl;
    audioNode.hidden = false;
    audioNode.play().catch(() => {
      audioNote.textContent = "Tap Play to hear Ajji's response.";
      audioNote.hidden = false;
    });
  } else if (data.audio_error) {
    audioNote.textContent = data.audio_error;
    audioNote.hidden = false;
  }
}

function handleExpired(error) {
  if (error.status !== 410) return false;
  sessionStorage.removeItem(SESSION_STORAGE_KEY);
  sessionId = null;
  conversation.hidden = true;
  startPanel.hidden = false;
  pageStatus.textContent = "That conversation expired. Start a new one to continue.";
  return true;
}

async function requestJson(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (sessionId) headers.set("X-Session-ID", sessionId);
  const response = await fetch(url, { ...options, headers });
  let data;
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    const error = new Error(data.detail || data.assistant_text || "The request could not be completed.");
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

async function startConversation() {
  let shouldRecord = false;
  showError("");
  setBusy(true);
  pageStatus.textContent = "Starting your conversation…";
  try {
    const data = await requestJson("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: languageSelect.value, address_id: addressSelect.value || null }),
    });
    sessionId = data.session_id;
    sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    showConversation();
    renderReply(data);
    pageStatus.textContent = `${displayLanguage(data.language)} selected.`;
    shouldRecord = data.state === "COLLECTING";
  } catch (error) {
    showError(error.message);
    pageStatus.textContent = "Could not start the conversation.";
  } finally {
    setBusy(false);
    if (shouldRecord) void beginRecording();
  }
}

async function loadAddresses() {
  const data = await requestJson("/api/addresses");
  addressSelect.replaceChildren();
  if (data.addresses.length > 1) {
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Choose a delivery address";
    placeholder.selected = true;
    addressSelect.append(placeholder);
  }
  for (const address of data.addresses) {
    const option = document.createElement("option");
    option.value = address.id;
    option.textContent = `${address.label}: ${address.full_address}`;
    addressSelect.append(option);
  }
  addressSelect.disabled = data.addresses.length === 0;
  startButton.disabled = data.addresses.length === 0 || data.addresses.length > 1;
  if (data.commerce_mode === "swiggy") {
    catalogueNote.textContent = "Prices and pack sizes come from Swiggy Instamart.";
    commerceBadge.textContent = "Swiggy live cart";
    commerceNote.textContent = data.checkout_enabled
      ? "Live Swiggy access is enabled. Review every detail before confirming."
      : "Live product and cart access only. Checkout is disabled.";
    confirmationIntro.textContent = data.checkout_enabled
      ? "Ajji will submit this order to Swiggy only after you explicitly confirm."
      : "Live checkout is disabled. This readback will not place an order.";
  }
  if (data.addresses.length > 1) {
    document.querySelector("#address-help").textContent = "Choose the exact address for delivery.";
  }
}

addressSelect.addEventListener("change", () => {
  startButton.disabled = busy || !addressSelect.value;
});

function encodeWav(chunks, sampleRate) {
  const sampleCount = chunks.reduce((total, chunk) => total + chunk.length, 0);
  if (sampleCount === 0) throw new Error("No audio was recorded. Please try again.");
  const buffer = new ArrayBuffer(44 + sampleCount * 2);
  const view = new DataView(buffer);
  const writeText = (offset, text) => {
    for (let index = 0; index < text.length; index += 1) view.setUint8(offset + index, text.charCodeAt(index));
  };
  writeText(0, "RIFF");
  view.setUint32(4, 36 + sampleCount * 2, true);
  writeText(8, "WAVE");
  writeText(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, sampleCount * 2, true);
  let offset = 44;
  for (const chunk of chunks) {
    for (const sample of chunk) {
      const clipped = Math.max(-1, Math.min(1, sample));
      view.setInt16(offset, clipped < 0 ? clipped * 32768 : clipped * 32767, true);
      offset += 2;
    }
  }
  return new Blob([buffer], { type: "audio/wav" });
}

async function beginRecording() {
  if (microphonePending || busy || !sessionId) return;
  showError("");
  if (!navigator.mediaDevices?.getUserMedia) {
    showError("Microphone recording is unavailable in this browser. Open this page on localhost or HTTPS.");
    return;
  }
  microphonePending = true;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
    const AudioContextType = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextType) throw new Error("This browser does not support audio recording.");
    const context = new AudioContextType();
    await context.resume();
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    processor.onaudioprocess = (event) => {
      chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
      event.outputBuffer.getChannelData(0).fill(0);
    };
    source.connect(processor);
    processor.connect(context.destination);
    recorder = { stream, context, source, processor, chunks, sampleRate: context.sampleRate };
    confirmButton.disabled = true;
    recordButton.setAttribute("aria-pressed", "true");
    recordButton.setAttribute("aria-label", "Stop recording");
    recordLabel.textContent = "Finish speaking";
    recordingStatus.textContent = recordingInstruction
      ? `Recording… ${recordingInstruction}`
      : "Recording… tap Finish when you are done.";
    recordingInstruction = "";
    recordingStatus.classList.add("is-recording");
    recorder.timer = window.setTimeout(() => finishRecording(), MAX_RECORDING_MS);
  } catch (error) {
    showError(error.message || "Ajji could not use the microphone. Check microphone permission and try again.");
  } finally {
    microphonePending = false;
  }
}

async function finishRecording() {
  if (!recorder || busy) return;
  const current = recorder;
  recorder = null;
  window.clearTimeout(current.timer);
  current.processor.onaudioprocess = null;
  current.source.disconnect();
  current.processor.disconnect();
  current.stream.getTracks().forEach((track) => track.stop());
  await current.context.close();
  recordButton.setAttribute("aria-pressed", "false");
  recordButton.setAttribute("aria-label", "Start recording");
  recordLabel.textContent = "Speak to Ajji";
  recordingStatus.classList.remove("is-recording");
  recordingStatus.textContent = "Processing your recording…";
  try {
    const wav = encodeWav(current.chunks, current.sampleRate);
    await uploadTurn(wav);
  } catch (error) {
    showError(error.message);
    recordingStatus.textContent = "Ready when you are.";
    confirmButton.disabled = confirmButton.dataset.allowed !== "true";
  }
}

async function uploadTurn(wav) {
  setBusy(true);
  showError("");
  pageStatus.textContent = "Ajji is listening to your words and preparing a reply…";
  try {
    const data = await requestJson("/api/session/turn", {
      method: "POST",
      headers: {
        "Content-Type": "audio/wav",
        ...(heardReadback
          ? { "X-Heard-Cart-Version": String(heardReadback.version), "X-Heard-Cart-Hash": heardReadback.hash }
          : {}),
      },
      body: wav,
    });
    renderReply(data);
    recordingStatus.textContent = "Ready for your next turn.";
    pageStatus.textContent = "Your conversation is saved in this browser tab.";
  } catch (error) {
    if (!handleExpired(error)) showError(error.message);
    recordingStatus.textContent = "Ready when you are.";
  } finally {
    setBusy(false);
  }
}

async function sendAction(body = {
  action: "confirm",
  cart_version: Number(confirmButton.dataset.version),
  cart_hash: confirmButton.dataset.hash,
}) {
  setBusy(true);
  showError("");
  pageStatus.textContent = "Ajji is working on it…";
  try {
    const data = await requestJson("/api/session/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderReply(data);
    pageStatus.textContent = data.state === "DONE" ? "Finished." : "Basket updated.";
  } catch (error) {
    if (error.status === 409 && error.data?.cart) {
      renderReply(error.data);
      pageStatus.textContent = "Please review the refreshed cart before confirming.";
      return;
    }
    if (!handleExpired(error)) showError(error.message);
  } finally {
    setBusy(false);
  }
}

function sendChoice(index) {
  return sendAction({ action: "select_option", option_index: index });
}

async function restoreConversation() {
  if (!sessionId) return;
  try {
    const data = await requestJson("/api/session");
    showConversation();
    renderCart(data);
    if (data.last_reply) assistantNode.textContent = data.last_reply;
    languageSelect.value = data.language;
    selectedLanguage.textContent = displayLanguage(data.language);
    pageStatus.textContent = `Conversation restored. ${displayLanguage(data.language)} selected.`;
  } catch (error) {
    handleExpired(error);
  }
}

pageStatus.textContent = "Ready to start.";
startButton.addEventListener("click", startConversation);
void loadAddresses().catch((error) => {
  showError(error.message);
  pageStatus.textContent = "Could not load delivery addresses.";
});
recordButton.addEventListener("click", () => {
  if (busy) return;
  if (recorder) finishRecording();
  else beginRecording();
});
confirmButton.addEventListener("click", () => sendAction());
cancelButton.addEventListener("click", () => sendAction({ action: "cancel" }));
backButton.addEventListener("click", () => {
  confirmationPanel.hidden = true;
  pageStatus.textContent = "Tell Ajji what you would like to change.";
  recordButton.focus();
});
newSessionButton.addEventListener("click", () => {
  sessionStorage.removeItem(SESSION_STORAGE_KEY);
  sessionId = null;
  conversation.hidden = true;
  startPanel.hidden = false;
  heardReadback = null;
  pageStatus.textContent = "Start another conversation when ready.";
});

restoreConversation();
