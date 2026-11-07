// A short guided walk-through for someone seeing Saathi for the first time. It only suggests what to
// say and says it for them. Everything it triggers is the normal product on demo data, labelled Demo.
(() => {
  const card = document.querySelector("#guide");
  const start = document.querySelector("#guide-start");
  if (!card || !start) return;
  const stepNode = document.querySelector("#guide-step");
  const caption = document.querySelector("#guide-caption");
  const phraseNode = document.querySelector("#guide-phrase");
  const sayButton = document.querySelector("#guide-say");
  const nextButton = document.querySelector("#guide-next");
  const stopButton = document.querySelector("#guide-stop");

  const STEPS = [
    {
      say: "Plan my day",
      note: "Saathi gathers your calendar, reminders and emails into one plan. Tick things off, move them, add your own.",
    },
    {
      say: "Check my important emails",
      note: "Practice emails now, marked Demo. Connect Gmail under My day to use your real inbox. Open one, then press Reply.",
    },
    {
      say: "Reply to Rahul saying I'll attend the meeting",
      note: "It writes a draft. Edit it, then press Send. Nothing leaves without your approval, and in demo nothing is sent at all.",
    },
    {
      say: "Schedule a meeting with Rahul tomorrow at 3 PM",
      note: "Saathi checks for clashes first, then waits for you to approve.",
    },
    {
      say: "Make it 4 PM instead",
      note: "Changed your mind? The same card updates. It doesn't make a second one.",
    },
    {
      say: "Check my important emails, schedule a meeting with Rahul tomorrow afternoon, remind me to buy groceries, and send my mother a WhatsApp message saying I'll be late",
      note: "Several things in one sentence. Independent ones run together, and each change waits for your approval.",
    },
  ];
  let index = 0;

  const signedIn = () => !document.querySelector("#personal-button")?.hidden;

  function render() {
    const step = STEPS[index];
    stepNode.textContent = `Guided demo · step ${index + 1} of ${STEPS.length}`;
    caption.textContent = step.note;
    phraseNode.textContent = `“${step.say}”`;
    nextButton.textContent = index === STEPS.length - 1 ? "Finish" : "Next";
    sayButton.disabled = false;
  }

  function close() {
    card.hidden = true;
  }

  start.addEventListener("click", () => {
    if (window.saathiIsLive?.()) {
      window.saathiToast?.("Switch to Demo shops first. The guided demo uses practice data only.", "info");
      return;
    }
    if (!signedIn()) {
      window.saathiToast?.("Please sign in or create an account first. Your tasks are saved to your account.", "info");
      document.querySelector("#account-button")?.click();
      return;
    }
    index = 0;
    card.hidden = false;
    render();
  });

  sayButton.addEventListener("click", async () => {
    sayButton.disabled = true;
    const done = await window.saathiSay?.(STEPS[index].say);
    sayButton.disabled = done === true;
  });

  nextButton.addEventListener("click", () => {
    if (index >= STEPS.length - 1) {
      close();
      return;
    }
    index += 1;
    render();
  });

  stopButton.addEventListener("click", close);
})();
