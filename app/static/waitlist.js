// Waitlist form. Mounts into #waitlist-mount. Built with DOM APIs and textContent only.
(function () {
  "use strict";
  var mount = document.getElementById("waitlist-mount");
  if (!mount) return;

  var CONSENT =
    "Tell me by email when live ordering opens. I can ask you to remove my email at any time.";
  var INTERESTS = [
    ["groceries", "Groceries"],
    ["food", "Food"],
    ["dining", "Table booking"],
    ["reminders", "Reminders"],
  ];
  var LANGUAGES = [
    ["en-IN", "English"],
    ["hi-IN", "Hindi"],
    ["kn-IN", "Kannada"],
  ];

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      node.setAttribute(k, attrs[k]);
    });
    if (text) node.textContent = text;
    return node;
  }

  var root = el("section", { class: "waitlist", "aria-labelledby": "wl-title" });
  root.appendChild(el("h2", { id: "wl-title" }, "Join the waitlist"));
  root.appendChild(el("p", {}, "Get an email when live ordering opens."));
  var form = el("form", { novalidate: "" });

  var emailWrap = el("div");
  emailWrap.appendChild(el("label", { for: "wl-email" }, "Email"));
  var email = el("input", {
    id: "wl-email",
    type: "email",
    name: "email",
    autocomplete: "email",
    maxlength: "254",
    required: "",
  });
  emailWrap.appendChild(email);
  form.appendChild(emailWrap);

  var fieldset = el("fieldset");
  fieldset.appendChild(el("legend", {}, "What are you interested in?"));
  var boxes = INTERESTS.map(function (pair) {
    var label = el("label", { class: "wl-check" });
    var box = el("input", { type: "checkbox", value: pair[0] });
    label.appendChild(box);
    label.appendChild(el("span", {}, pair[1]));
    fieldset.appendChild(label);
    return box;
  });
  form.appendChild(fieldset);

  var langWrap = el("div");
  langWrap.appendChild(el("label", { for: "wl-language" }, "Language"));
  var language = el("select", { id: "wl-language", name: "language" });
  LANGUAGES.forEach(function (pair) {
    language.appendChild(el("option", { value: pair[0] }, pair[1]));
  });
  langWrap.appendChild(language);
  form.appendChild(langWrap);

  var consentLabel = el("label", { class: "wl-check", for: "wl-consent" });
  var consent = el("input", { id: "wl-consent", type: "checkbox", required: "" });
  consentLabel.appendChild(consent);
  var consentText = el("span", {}, CONSENT + " ");
  consentText.appendChild(el("a", { href: "/privacy" }, "Privacy"));
  consentLabel.appendChild(consentText);
  form.appendChild(consentLabel);

  var button = el("button", { type: "submit" }, "Join the list");
  form.appendChild(button);
  var status = el("p", { class: "wl-status", role: "status", "aria-live": "polite" });
  form.appendChild(status);
  root.appendChild(form);
  mount.appendChild(root);

  function say(message, isError) {
    status.textContent = message;
    status.className = "wl-status" + (isError ? " wl-error" : "");
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var address = email.value.trim();
    if (!address || address.indexOf("@") < 1 || /\s/.test(address)) {
      say("Please enter a valid email address.", true);
      email.focus();
      return;
    }
    if (!consent.checked) {
      say("Please tick the box to join the list.", true);
      consent.focus();
      return;
    }
    var interests = boxes
      .filter(function (b) {
        return b.checked;
      })
      .map(function (b) {
        return b.value;
      });
    button.disabled = true;
    say("Sending...", false);
    fetch("/api/waitlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: address,
        interests: interests,
        language: language.value,
        consent: true,
      }),
    })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            if (response.ok && data.status === "joined") {
              say("You're on the list.", false);
              form.reset();
            } else if (response.ok && data.status === "already_listed") {
              say("This email is already on the list.", false);
            } else if (response.status === 429) {
              say("Too many tries. Please try again later.", true);
            } else {
              say(
                typeof data.detail === "string"
                  ? data.detail
                  : "Something went wrong. Please check your email address and try again.",
                true,
              );
            }
          });
      })
      .catch(function () {
        say("Could not reach the server. Please try again.", true);
      })
      .then(function () {
        button.disabled = false;
      });
  });
})();
