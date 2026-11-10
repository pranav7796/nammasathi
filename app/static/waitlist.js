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
  // The same shape the server checks, so a typo is caught before the request.
  var EMAIL = /^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$/;

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      node.setAttribute(k, attrs[k]);
    });
    if (text) node.textContent = text;
    return node;
  }

  var root = el("div", { class: "waitlist" });
  var form = el("form", { novalidate: "", "aria-labelledby": "waitlist-heading" });

  var emailWrap = el("div");
  emailWrap.appendChild(el("label", { for: "wl-email" }, "Your email"));
  var email = el("input", {
    id: "wl-email",
    type: "email",
    name: "email",
    autocomplete: "email",
    inputmode: "email",
    autocapitalize: "none",
    spellcheck: "false",
    placeholder: "you@example.com",
    maxlength: "254",
    required: "",
    "aria-describedby": "wl-status",
  });
  emailWrap.appendChild(email);
  form.appendChild(emailWrap);

  // Left empty by people. Anything that fills it is not saved.
  var trapWrap = el("div", { class: "wl-hp", "aria-hidden": "true" });
  trapWrap.appendChild(el("label", { for: "wl-website" }, "Leave this empty"));
  var trap = el("input", {
    id: "wl-website",
    type: "text",
    name: "website",
    tabindex: "-1",
    autocomplete: "off",
  });
  trapWrap.appendChild(trap);
  form.appendChild(trapWrap);

  var more = el("details");
  more.appendChild(el("summary", {}, "Tell me more (optional)"));
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
  more.appendChild(fieldset);
  var langWrap = el("div");
  langWrap.appendChild(el("label", { for: "wl-language" }, "Language you would speak"));
  var language = el("select", { id: "wl-language", name: "language" });
  LANGUAGES.forEach(function (pair) {
    language.appendChild(el("option", { value: pair[0] }, pair[1]));
  });
  langWrap.appendChild(language);
  more.appendChild(langWrap);
  form.appendChild(more);

  var consentLabel = el("label", { class: "wl-check", for: "wl-consent" });
  var consent = el("input", { id: "wl-consent", type: "checkbox", required: "" });
  consentLabel.appendChild(consent);
  var consentText = el("span", {}, CONSENT + " ");
  consentText.appendChild(el("a", { href: "/privacy" }, "Privacy"));
  consentLabel.appendChild(consentText);
  form.appendChild(consentLabel);

  var button = el("button", { type: "submit" }, "Join the waitlist");
  form.appendChild(button);
  var status = el("p", { id: "wl-status", class: "wl-status", role: "status", "aria-live": "polite" });
  form.appendChild(status);
  root.appendChild(form);
  mount.appendChild(root);

  function say(message, isError) {
    status.textContent = message;
    status.className = "wl-status" + (isError ? " wl-error" : "");
    if (isError) email.setAttribute("aria-invalid", "true");
    else email.removeAttribute("aria-invalid");
  }

  // The form is replaced, so the result cannot be missed or sent twice.
  function done(title, detail) {
    var box = el("div", { class: "wl-done", role: "status", tabindex: "-1" });
    box.appendChild(el("strong", {}, title));
    box.appendChild(el("p", {}, detail));
    root.replaceChildren(box);
    box.focus();
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (button.disabled) return;
    var address = email.value.trim();
    if (!EMAIL.test(address)) {
      say("Please enter a valid email address, like you@example.com.", true);
      email.focus();
      return;
    }
    if (!consent.checked) {
      status.textContent = "Please tick the box to join the list.";
      status.className = "wl-status wl-error";
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
    button.textContent = "Joining...";
    say("", false);
    fetch("/api/waitlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: address,
        interests: interests,
        language: language.value,
        consent: true,
        website: trap.value,
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
              done(
                "You're on the list.",
                "I'll email " + address + " when live ordering opens. " +
                  "There is no confirmation email, so nothing to look for in your inbox today.",
              );
            } else if (response.ok && data.status === "already_listed") {
              done(
                "You're already on the list.",
                address + " was added earlier. I'll email you when live ordering opens.",
              );
            } else if (response.status === 429) {
              say("Too many tries just now. Please try again in a while.", true);
            } else if (response.status === 404) {
              say("The waitlist is not open on this server.", true);
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
        say("Could not reach the server. Please check your connection and try again.", true);
      })
      .then(function () {
        button.disabled = false;
        button.textContent = "Join the waitlist";
      });
  });
})();
