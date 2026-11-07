"use strict";

const MAX_RECORDING_MS = 30_000;
const SESSION_STORAGE_KEY = "namma-saathi-session";

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
const cartHeading = document.querySelector("#cart-heading");
const tasksPanel = document.querySelector("#tasks-panel");
const tasksList = document.querySelector("#tasks-list");
const tasksMode = document.querySelector("#tasks-mode");
const languageChoices = document.querySelector("#language-choices");
const suggestionsNode = document.querySelector("#suggestions");
const orderHeading = document.querySelector("#order-heading");
const confirmEyebrow = document.querySelector("#confirm-eyebrow");
const confirmationAddressLabel = document.querySelector("#confirmation-address-label");
const textForm = document.querySelector("#text-form");
const textInput = document.querySelector("#text-input");
const textSend = document.querySelector("#text-send");
const scriptedNote = document.querySelector("#scripted-note");
const publicNote = document.querySelector("#public-note");
const timingsNode = document.querySelector("#timings");
const SHOW_TIMINGS = (window.location?.search || "").includes("timings");
const TYPE_ONLY = (window.location?.search || "").includes("typed");

let sessionId = sessionStorage.getItem(SESSION_STORAGE_KEY);
let recorder = null;
let busy = false;
let activeAudioUrl = null;
let microphonePending = false;
let recordingInstruction = "";
// The readback this page actually received. A spoken yes confirms this cart, not the latest one.
let heardReadback = null;
// The person's own Swiggy account, chosen on the page. Off means the demo shops.
let liveMode = false;

function setBusy(value) {
  busy = value;
  conversation.classList[value ? "add" : "remove"]("is-busy");
  startButton.disabled = value || !addressSelect.value;
  recordButton.disabled = value || !sessionId;
  textSend.disabled = value || !sessionId;
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
  window.scrollTo?.(0, 0);
  recordButton.disabled = !sessionId;
}

function displayLanguage(code) {
  return ({ "kn-IN": "ಕನ್ನಡ · Kannada", "hi-IN": "हिन्दी · Hindi", "en-IN": "English" })[code] || code;
}

// Phrases the demo is known to handle, one per capability, in each language.
const TRY_SAYING = {
  grocery: {
    "kn-IN": "ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ ಮತ್ತು ಒಂದು ಲೀಟರ್ ಹಾಲು",
    "hi-IN": "दो किलो चावल और एक लीटर दूध",
    "en-IN": "Two kilos of rice and one litre of milk",
  },
  meal: {
    "kn-IN": "ರಾತ್ರಿ ಊಟಕ್ಕೆ ಏನಾದರೂ ಬೇಕು",
    "hi-IN": "मुझे रात के खाने के लिए कुछ चाहिए",
    "en-IN": "I want something for dinner",
  },
  table: {
    "kn-IN": "ನಾಳೆ ಸಂಜೆ ಏಳಕ್ಕೆ ಇಂದಿರಾನಗರದಲ್ಲಿ ಇಬ್ಬರಿಗೆ ಇಟಾಲಿಯನ್ ಟೇಬಲ್ ಬುಕ್ ಮಾಡಿ",
    "hi-IN": "कल शाम सात बजे इंदिरानगर में दो लोगों के लिए इटालियन टेबल बुक करो",
    "en-IN": "Book a table for two in Indiranagar tomorrow at 7 PM, Italian",
  },
};
TRY_SAYING.day = {
  "kn-IN": "ನನ್ನ ದಿನ ಯೋಜಿಸಿ",
  "hi-IN": "मेरा दिन प्लान करो",
  "en-IN": "Plan my day",
};
TRY_SAYING.email = {
  "kn-IN": "ಮುಖ್ಯ ಇಮೇಲ್ ನೋಡಿ",
  "hi-IN": "जरूरी ईमेल देखो",
  "en-IN": "Check my important emails",
};
TRY_SAYING.meeting = {
  "kn-IN": "ನಾಳೆ ಮಧ್ಯಾಹ್ನ 3 ಗಂಟೆಗೆ ರಾಹುಲ್ ಜೊತೆ ಮೀಟಿಂಗ್ ಹಾಕಿ",
  "hi-IN": "कल दोपहर 3 बजे राहुल के साथ मीटिंग रखो",
  "en-IN": "Schedule a meeting with Rahul tomorrow at 3 PM",
};
TRY_SAYING.whatsapp = {
  "kn-IN": "ಅಮ್ಮನಿಗೆ ನಾನು 15 ನಿಮಿಷದಲ್ಲಿ ತಲುಪುತ್ತೇನೆ ಎಂದು ವಾಟ್ಸಾಪ್ ಮಾಡಿ",
  "hi-IN": "अम्मा को व्हाट्सऐप भेजो कि मैं 15 मिनट में पहुँचूँगा",
  "en-IN": "Send Amma a WhatsApp message saying I'll reach in 15 minutes",
};
TRY_SAYING.multi = {
  "kn-IN": "ಮುಖ್ಯ ಇಮೇಲ್ ನೋಡಿ, ನಾಳೆ ಮಧ್ಯಾಹ್ನ 3 ಗಂಟೆಗೆ ರಾಹುಲ್ ಜೊತೆ ಮೀಟಿಂಗ್ ಹಾಕಿ ಮತ್ತು ಅಮ್ಮನಿಗೆ ನಾನು ತಡವಾಗಿ ಬರುತ್ತೇನೆ ಎಂದು ವಾಟ್ಸಾಪ್ ಮಾಡಿ",
  "hi-IN": "जरूरी ईमेल देखो, कल दोपहर 3 बजे राहुल के साथ मीटिंग रखो और अम्मा को व्हाट्सऐप भेजो कि मैं देर से आऊँगा",
  "en-IN": "Check my important emails, schedule a meeting with Rahul tomorrow afternoon, remind me to buy groceries, and send my mother a WhatsApp message saying I'll be late",
};
let tryCapability = "grocery";

function currentPhrase() {
  return TRY_SAYING[tryCapability]?.[languageSelect.value] || "";
}

function showTrySaying() {
  const text = document.querySelector("#try-text");
  if (text) text.textContent = `\u201C${currentPhrase()}\u201D`;
  for (const cap of document.querySelectorAll(".cap")) {
    cap.setAttribute("aria-pressed", String(cap.dataset.cap === tryCapability));
  }
}

function showLanguage() {
  showTrySaying();
  for (const pill of document.querySelectorAll(".lang-pill")) {
    pill.setAttribute("aria-pressed", String(pill.dataset.lang === languageSelect.value));
  }
  selectedLanguage.textContent = displayLanguage(languageSelect.value);
  for (const choice of languageChoices.children) {
    choice.setAttribute("aria-pressed", String(choice.dataset.lang === languageSelect.value));
  }
}

languageSelect.addEventListener("change", showLanguage);
languageChoices.addEventListener("click", (event) => {
  const choice = event.target.closest("[data-lang]");
  if (!choice) return;
  languageSelect.value = choice.dataset.lang;
  showLanguage();
});

const SUGGESTIONS = {
  "en-IN": ["I need rice and milk", "I want something for dinner", "Book a table for two tomorrow at 7 PM in Indiranagar, Italian"],
  "hi-IN": ["दो किलो चावल और एक लीटर दूध", "रात के खाने के लिए कुछ चाहिए", "कल शाम सात बजे इंदिरानगर में दो लोगों के लिए इटालियन टेबल बुक करो"],
  "kn-IN": ["ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ ಮತ್ತು ಒಂದು ಲೀಟರ್ ಹಾಲು", "ರಾತ್ರಿ ಊಟಕ್ಕೆ ಏನಾದರೂ ಬೇಕು", "ನಾಳೆ ಸಂಜೆ ಏಳು ಗಂಟೆಗೆ ಇಂದಿರಾನಗರದಲ್ಲಿ ಇಬ್ಬರಿಗೆ ಇಟಾಲಿಯನ್ ಟೇಬಲ್ ಬುಕ್ ಮಾಡಿ"],
};

function renderSuggestions(data) {
  const phrases = (data.tasks || []).length === 0 ? SUGGESTIONS[data.language] || [] : [];
  suggestionsNode.hidden = phrases.length === 0;
  suggestionsNode.replaceChildren();
  for (const phrase of phrases) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion";
    button.textContent = phrase;
    button.addEventListener("click", () => {
      if (!busy && sessionId) void sendText(phrase);
    });
    suggestionsNode.append(button);
  }
}

const CAPABILITY_TITLES = { grocery: "Your groceries", food: "Your meal", dining: "Your table" };

const TASK_NAMES = { grocery: "Groceries", food: "Meal", dining: "Table" };

function taskDetail(task, isMock) {
  if (task.done) {
    const noun = task.capability === "dining" ? "booked" : "ordered";
    return isMock ? `Demo ${noun}` : `${noun[0].toUpperCase()}${noun.slice(1)}`;
  }
  if (task.state === "ORDER_UNKNOWN" || task.state === "UNKNOWN") return "Status unknown";
  const parts = [];
  if (task.capability !== "dining" && task.item_count) {
    parts.push(`${task.item_count} ${task.item_count === 1 ? "item" : "items"}`, `₹${task.total_inr}`);
  }
  parts.push(task.active ? "Working on it" : task.open ? "Waiting" : "Nothing yet");
  return parts.join(" · ");
}

function renderTasks(data) {
  const tasks = data.tasks || [];
  const isMock = data.commerce_mode !== "swiggy";
  tasksPanel.hidden = tasks.length === 0;
  tasksMode.textContent = isMock
    ? "· Demo: mock shops, nothing is real"
    : data.swiggy_environment === "staging" ? "· Swiggy test data" : "· Live Swiggy account";
  tasksList.replaceChildren();
  for (const task of tasks) {
    const row = document.createElement("li");
    row.className = task.active ? "task-row is-active" : "task-row";
    const name = document.createElement("strong");
    name.textContent = TASK_NAMES[task.capability] || task.capability;
    const detail = document.createElement("span");
    detail.textContent = taskDetail(task, isMock);
    row.append(name, detail);
    if (!task.active && task.open && !task.done) {
      const back = document.createElement("button");
      back.type = "button";
      back.className = "line-edit-button";
      back.textContent = "Go back to this";
      back.addEventListener("click", () => sendAction({ action: "resume", capability: task.capability }));
      row.append(back);
    }
    tasksList.append(row);
  }
}

// Swiggy's staging server has seeded data and places no real orders, so never call it real.
function swiggyBadge(data) {
  return data.swiggy_environment === "staging" ? "Swiggy test data" : "Swiggy live cart";
}

function renderCart(data) {
  renderTasks(data);
  renderSuggestions(data);
  const isDining = data.capability === "dining";
  cartHeading.textContent = CAPABILITY_TITLES[data.capability] || "Your task";
  catalogueNote.hidden = isDining;
  cartNode.replaceChildren();
  if (!data.cart.length) {
    const empty = document.createElement("li");
    empty.className = "empty-cart";
    empty.textContent = "Nothing here yet.";
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
      const pack = line.pack_size ? `${line.pack_size} · ` : "";
      details.textContent = isDining ? line.pack_size : `${line.quantity} × ${pack}₹${line.unit_price_inr} each`;
      description.append(name, details);
      const lineTotal = document.createElement("span");
      lineTotal.className = "cart-line-total";
      lineTotal.textContent = isDining ? "Free" : `₹${line.line_total_inr}`;
      const editButton = document.createElement("button");
      editButton.className = "line-edit-button";
      editButton.type = "button";
      editButton.disabled = ["CONFIRMED", "PLACING", "VERIFYING", "FAILED", "DONE", "ORDER_UNKNOWN"].includes(data.state);
      editButton.textContent = "Change / remove";
      editButton.setAttribute("aria-label", `Change or remove ${line.name}`);
      editButton.addEventListener("click", () => {
        recordingInstruction = `Say what you would like to change about ${line.name}, or ask Saathi to remove it.`;
        beginRecording();
      });
      item.append(description, lineTotal, editButton);
      cartNode.append(item);
    }
  }
  cartTotalNode.textContent = isDining ? "Nothing to pay" : `₹${data.total_inr}`;
  const isMock = data.commerce_mode !== "swiggy";
  const stateLabels = {
    COLLECTING: "Ready when you are",
    CLARIFYING: "Saathi has a question",
    READBACK: "Please review",
    CONFIRMED: "Confirmed",
    PLACING: isMock ? "Placing demo order" : "Placing your order",
    CHOOSING_RESTAURANT: "Saathi has a question",
    CHOOSING_SLOT: "Saathi has a question",
    BOOKING: "Booking your table",
    VERIFYING: isDining ? "Checking your booking" : "Checking your order",
    DONE: isDining
      ? (isMock ? "Demo table booked" : "Table booked")
      : (isMock ? "Demo order placed" : "Order placed"),
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
      const size = line.pack_size ? `, ${line.pack_size}` : "";
      item.textContent = isDining
        ? `${line.name}${size}`
        : `${line.quantity} × ${line.name}${size} — ₹${line.unit_price_inr} each, ₹${line.line_total_inr} total`;
      confirmationItems.append(item);
    }
    const bill = [
      data.savings_inr ? `saves ₹${data.savings_inr}` : "",
      data.fees_inr ? `fees ₹${data.fees_inr}` : "",
    ].filter(Boolean).join(", ");
    confirmationTotal.textContent = isDining ? "Free reservation" : bill ? `₹${data.total_inr} (${bill})` : `₹${data.total_inr}`;
    confirmEyebrow.textContent = isDining ? "Please check before booking" : "Please check before ordering";
    confirmationAddressLabel.textContent = isDining ? "Area" : "Delivery address";
    confirmationAddress.textContent = data.delivery_address || data.address_label || "No delivery address is set";
    const payment = data.payment_method === "COD"
      ? "Cash on delivery"
      : data.payment_method || "Payment method not set";
    confirmationPayment.textContent = isDining
      ? "Nothing to pay now"
      : isMock ? `${payment} (demo only)` : payment;
    if (data.commerce_mode === "swiggy") {
      catalogueNote.textContent = "Prices and pack sizes come from Swiggy Instamart.";
      commerceBadge.textContent = swiggyBadge(data);
      commerceNote.textContent = data.checkout_enabled
        ? "Live Swiggy access is enabled. Review every detail before confirming."
        : "Live product and cart access only. Checkout is disabled.";
      confirmationIntro.textContent = data.checkout_enabled
        ? "Saathi will submit this order to Swiggy only after you explicitly confirm."
        : "Live checkout is disabled. This readback will not place an order.";
      confirmButton.textContent = !data.checkout_enabled
        ? "Swiggy checkout disabled"
        : data.swiggy_environment === "staging"
          ? (isDining ? "Confirm test booking" : "Confirm test order")
          : isDining ? "Confirm real table booking" : "Confirm real Swiggy order";
    } else {
      commerceBadge.textContent = "Demo order";
      commerceNote.textContent = "This is a demo. Orders are not sent to a shop.";
      confirmationIntro.textContent = "Nothing happens until you confirm. This is a demo.";
      confirmButton.textContent = isDining ? "Confirm demo booking" : "Confirm demo order";
    }
  }
  confirmButton.disabled = busy || !canConfirm;
  confirmNote.textContent = isDining
    ? "Please review the restaurant, date, time, and number of people."
    : "Please review every item, the total, address, and payment method.";
}

function renderOptions(data) {
  const options = data.options || [];
  optionsPanel.hidden = options.length === 0;
  optionsList.replaceChildren();
  options.forEach((option, index) => {
    const button = document.createElement("button");
    button.type = "button";
    const quantity = option.quantity > 1 ? `${option.quantity} × ` : "";
    const size = option.pack_size ? `, ${option.pack_size}` : "";
    const price = data.capability === "dining" ? "" : ` — ₹${option.unit_price_inr}`;
    button.textContent = `${index + 1}. ${quantity}${option.name}${size}${price}`;
    button.addEventListener("click", () => sendChoice(index));
    optionsList.append(button);
  });
}

function renderOrder(data, isMock) {
  const ids = data.order_ids || [];
  const unknown = data.state === "ORDER_UNKNOWN";
  orderPanel.hidden = !(unknown || (data.state === "DONE" && ids.length > 0));
  orderHeading.textContent = data.capability === "dining" ? "Your table" : "Your order";
  orderPanel.dataset.outcome = unknown || data.order_partial || (!isMock && !data.order_verified)
    ? "attention"
    : "settled";
  if (unknown) {
    orderResult.textContent = "Saathi could not confirm this order. Please do not order again. Check the Swiggy app.";
  } else if (isMock) {
    orderResult.textContent = data.capability === "dining"
      ? "This was a demo. No real table was booked."
      : "This was a demo. No real order was placed.";
  } else if (data.order_partial) {
    orderResult.textContent = "Only part of this order was placed. Please check the Swiggy app.";
  } else if (data.order_verified) {
    orderResult.textContent = `Swiggy shows this as: ${data.order_status || "placed"}.`;
  } else {
    orderResult.textContent = "Swiggy reported this order, but Saathi could not check it. Please look in the Swiggy app.";
  }
  const noun = data.capability === "dining" ? "Booking" : "Order";
  orderIds.textContent = ids.length ? `${noun}: ${ids.join(", ")}` : "";
}

const workflowTitle = document.querySelector("#workflow-title");
const chatLog = document.querySelector("#chat-log");
const contextHint = document.querySelector("#ctx-hint");
const LANG_CODE = { "kn-IN": "kn", "hi-IN": "hi", "en-IN": "en" };
const ORDER_TITLES = { grocery: "Groceries", food: "Food", dining: "Table booking" };
const WORKFLOW_TITLES = {
  whatsapp: "WhatsApp message",
  email: "Email",
  calendar: "Calendar",
  reminders: "Reminders",
  day: "My day",
  tasks: "Tasks",
};
const WAITING_FOR = {
  to: "who it's for",
  body: "what to say",
  intent: "what to say",
  when: "the day and time",
  find: "which event",
  text: "what to remember",
};
let previousTurn = null;
let openOrderTasks = [];

// Which panel shows beside the conversation: the basket only for an order, the task widgets for
// everything else, and a welcome with suggestions when nothing is under way.
function contextFor(data) {
  const kind = (data.workflow && data.workflow.kind) || "none";
  if (kind === "order") {
    return (data.cart || []).length || data.state !== "COLLECTING" ? "order" : "welcome";
  }
  return kind === "none" ? "welcome" : "tasks";
}

function titleFor(data) {
  const kind = (data.workflow && data.workflow.kind) || "none";
  if (kind === "order") return ORDER_TITLES[data.capability] || "";
  return WORKFLOW_TITLES[kind] || "";
}

function addToLog(said, reply, language) {
  if (!chatLog || (!said && !reply)) return;
  const make = (who, text) => {
    const row = document.createElement("div");
    row.className = `msg ${who}`;
    const bubble = document.createElement("p");
    bubble.className = "bubble";
    bubble.textContent = text;
    if (language && LANG_CODE[language]) bubble.setAttribute("lang", LANG_CODE[language]);
    row.append(bubble);
    return row;
  };
  if (said) chatLog.append(make("user", said));
  if (reply) chatLog.append(make("assistant", reply));
  while (chatLog.children.length > 40) chatLog.firstElementChild?.remove();
}

function showWorkflow(data) {
  const ctx = contextFor(data);
  if (conversation.dataset) conversation.dataset.ctx = ctx;
  const title = titleFor(data);
  if (workflowTitle) {
    workflowTitle.textContent = title;
    workflowTitle.hidden = !title;
  }
  if (contextHint) {
    const waiting = data.workflow && data.workflow.waiting_for;
    contextHint.textContent = waiting ? `Saathi is waiting for ${WAITING_FOR[waiting] || "your answer"}. Say it, or say cancel.` : "";
    contextHint.hidden = !waiting;
  }
  openOrderTasks = (data.tasks || []).filter((task) => task.open && task.item_count > 0);
  // The generic "try saying" chips belong to starting out, not to a WhatsApp or email task.
  const chips = document.querySelector("#suggestions");
  if (chips && ctx !== "welcome") chips.hidden = true;
}

function renderReply(data) {
  const language = data.language || languageSelect.value;
  // The turn that was on screen becomes history, so the conversation reads as a conversation.
  if (previousTurn && data.transcript !== undefined) {
    addToLog(previousTurn.said, previousTurn.reply, previousTurn.language);
  }
  if (data.transcript !== null && data.transcript !== undefined) {
    transcriptNode.textContent = data.transcript || "No speech was recognized.";
  } else {
    transcriptNode.textContent = ""; // nothing was heard in this reply: never show a stale line
  }
  assistantNode.textContent = data.assistant_text || "I am ready for your next turn.";
  const code = LANG_CODE[language];
  if (code) {
    assistantNode.setAttribute("lang", code);
    transcriptNode.setAttribute("lang", code);
  }
  previousTurn = { said: transcriptNode.textContent || "", reply: assistantNode.textContent, language };
  renderCart(data);
  showWorkflow(data);
  window.saathiShowWidgets?.(data.ui, data.plan, data.workflow);
  if (data.commerce_mode === "swiggy") window.saathiAccountRefresh?.();
  scriptedNote.hidden = data.scripted_understanding !== true;
  if (SHOW_TIMINGS && data.timings_ms) {
    const t = data.timings_ms;
    timingsNode.textContent = `Speech recognition ${t.stt} ms · understanding ${t.understanding} ms (${data.understood_by || "none"}) · provider ${t.provider} ms · orchestration ${t.orchestration} ms · speech ${t.speech} ms · total ${t.total} ms`;
    timingsNode.hidden = false;
  }
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
      audioNote.textContent = "Tap Play to hear Saathi's response.";
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

async function startConversation({ autoRecord = true } = {}) {
  let shouldRecord = false;
  showError("");
  setBusy(true);
  pageStatus.textContent = "Starting your conversation…";
  try {
    const data = await requestJson("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        language: languageSelect.value,
        address_id: addressSelect.value || null,
        ...(liveMode ? { live: true } : {}),
      }),
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
    // A browser with no microphone can still type, so a failed automatic start stays quiet.
    if (shouldRecord && autoRecord && !TYPE_ONLY) void beginRecording({ quiet: true });
  }
}

async function loadAddresses() {
  const data = await requestJson(liveMode ? "/api/addresses?live=1" : "/api/addresses");
  publicNote.hidden = data.public_deployment !== true || data.commerce_mode === "swiggy";
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
    commerceBadge.textContent = swiggyBadge(data);
    commerceNote.textContent = data.checkout_enabled
      ? "Live Swiggy access is enabled. Review every detail before confirming."
      : "Live product and cart access only. Checkout is disabled.";
    confirmationIntro.textContent = data.checkout_enabled
      ? "Saathi will submit this order to Swiggy only after you explicitly confirm."
      : "Live checkout is disabled. This readback will not place an order.";
  } else {
    commerceBadge.textContent = "Demo order";
    commerceNote.textContent = "This is a demo. Orders are not sent to a shop.";
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

async function beginRecording({ quiet = false } = {}) {
  if (microphonePending || busy || !sessionId) return;
  showError("");
  if (!navigator.mediaDevices?.getUserMedia) {
    if (!quiet) showError("Microphone recording is unavailable in this browser. Open this page on localhost or HTTPS.");
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
    if (!quiet) showError(error.message || "Saathi could not use the microphone. Check microphone permission and try again.");
  } finally {
    microphonePending = false;
  }
}

async function discardRecording() {
  if (!recorder) return;
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
  recordLabel.textContent = "Speak to Saathi";
  recordingStatus.classList.remove("is-recording");
}

async function sendText(text) {
  await discardRecording();
  setBusy(true);
  showError("");
  pageStatus.textContent = "Saathi is preparing a reply…";
  try {
    const data = await requestJson("/api/session/text", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(heardReadback
          ? { "X-Heard-Cart-Version": String(heardReadback.version), "X-Heard-Cart-Hash": heardReadback.hash }
          : {}),
      },
      body: JSON.stringify({ text }),
    });
    textInput.value = "";
    renderReply(data);
    recordingStatus.textContent = "Ready for your next turn.";
    pageStatus.textContent = "Your conversation is saved in this browser tab.";
  } catch (error) {
    if (!handleExpired(error)) showError(error.message);
  } finally {
    setBusy(false);
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
  recordLabel.textContent = "Speak to Saathi";
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
  pageStatus.textContent = "Saathi is listening to your words and preparing a reply…";
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
  pageStatus.textContent = "Saathi is working on it…";
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
  if (!sessionId) {
    // A signed-in person's conversation follows them to another tab or device.
    try {
      const current = await requestJson("/api/session/current");
      if (!current.session_id) return;
      sessionId = current.session_id;
      sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    } catch {
      return;
    }
  }
  try {
    const data = await requestJson("/api/session");
    showConversation();
    renderCart(data);
    if (data.last_reply) assistantNode.textContent = data.last_reply;
    languageSelect.value = data.language;
    showLanguage();
    pageStatus.textContent = `Conversation restored. ${displayLanguage(data.language)} selected.`;
  } catch (error) {
    handleExpired(error);
  }
}

// account.js calls this with a signed-in person's saved language, before they start.
window.saathiSetLanguage = (code) => {
  if (sessionId || !["kn-IN", "hi-IN", "en-IN"].includes(code)) return;
  languageSelect.value = code;
  showLanguage();
};

// account.js calls this. A failed address read leaves the start button off, so nothing is assumed.
window.saathiSetLive = (useLive) => {
  if (useLive === liveMode) return;
  liveMode = useLive;
  showError("");
  addressSelect.replaceChildren();
  startButton.disabled = true;
  void loadAddresses().catch((error) => {
    showError(error.message);
    pageStatus.textContent = "Could not load delivery addresses.";
  });
};

pageStatus.textContent = "Ready to start.";
startButton.addEventListener("click", () => startConversation());

for (const cap of document.querySelectorAll(".cap")) {
  cap.addEventListener("click", () => {
    tryCapability = cap.dataset.cap;
    showTrySaying();
  });
}

// Tapping the example starts a conversation and says it. With a real Swiggy account it only fills
// the text box, so nothing reaches a real cart until the person presses Send themselves.
document.querySelector("#try-phrase")?.addEventListener("click", async () => {
  if (sessionId || busy) return;
  if (startButton.disabled) {
    showError("Choose a delivery address first, then try the phrase.");
    return;
  }
  await window.saathiSay(currentPhrase());
});

// Starts a conversation if there is none, then says this. With a real Swiggy account it only fills
// the text box, so nothing reaches anything real until the person presses Send themselves.
window.saathiSay = async (text) => {
  if (busy) return false;
  if (!sessionId) {
    if (startButton.disabled) {
      showError("Choose a delivery address first, then try the phrase.");
      return false;
    }
    await startConversation({ autoRecord: false });
    if (!sessionId) return false;
  }
  if (liveMode) {
    textInput.value = text;
    textInput.focus();
    return true;
  }
  await sendText(text);
  return true;
};
window.saathiIsLive = () => liveMode;

// Switching language mid-conversation. An order's spoken readback is bound to one language, so with
// an order open it asks to finish that first. Otherwise it starts a fresh conversation in the new
// language and keeps the history on screen.
async function switchLanguage(code) {
  if (!sessionId) {
    languageSelect.value = code;
    showLanguage();
    return;
  }
  if (code === languageSelect.value || busy) return;
  if (openOrderTasks.length) {
    window.saathiToast?.("You have an order open. Finish or cancel it, then switch language.", "info");
    return;
  }
  languageSelect.value = code;
  showLanguage();
  const divider = document.createElement("div");
  divider.className = "chat-divider";
  divider.textContent = `Now in ${displayLanguage(code)}`;
  chatLog?.append(divider);
  previousTurn = null;
  await startConversation({ autoRecord: false });
}

for (const pill of document.querySelectorAll(".lang-pill")) {
  pill.addEventListener("click", () => void switchLanguage(pill.dataset.lang));
}

for (const chip of document.querySelectorAll(".prompt-chip")) {
  chip.addEventListener("click", () => {
    tryCapability = chip.dataset.cap;
    void window.saathiSay(currentPhrase());
  });
}

showTrySaying();
void loadAddresses().catch((error) => {
  showError(error.message);
  pageStatus.textContent = "Could not load delivery addresses.";
});
recordButton.addEventListener("click", () => {
  if (busy) return;
  if (recorder) finishRecording();
  else beginRecording();
});
textForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = textInput.value.trim();
  if (text && !busy && sessionId) void sendText(text);
});
confirmButton.addEventListener("click", () => sendAction());
cancelButton.addEventListener("click", () => sendAction({ action: "cancel" }));
backButton.addEventListener("click", () => {
  confirmationPanel.hidden = true;
  pageStatus.textContent = "Tell Saathi what you would like to change.";
  recordButton.focus();
});
newSessionButton.addEventListener("click", () => {
  sessionStorage.removeItem(SESSION_STORAGE_KEY);
  sessionId = null;
  previousTurn = null;
  chatLog?.replaceChildren();
  conversation.hidden = true;
  startPanel.hidden = false;
  heardReadback = null;
  pageStatus.textContent = "Start another conversation when ready.";
});

restoreConversation();
