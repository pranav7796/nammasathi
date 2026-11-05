# Demo and recording script

One assistant, three everyday tasks, on built-in mock shops. The page labels this as a demo, and
nothing reaches Swiggy. Swiggy's access page accepts a localhost recording, so a video made here
qualifies.

## Set up

```bash
# In .env: COMMERCE_BACKEND=mock, GROQ_API_KEY, GEMINI_API_KEY, GNANI_API_KEY
# Add DEMO_UNDERSTANDING=fallback, so a quota failure cannot end the take.
uv sync
uv run python -m app.demo_check --language en-IN   # every journey must say FINISHED
uv run uvicorn app.web:app
```

Open <http://localhost:8000/?timings=1> to rehearse with each stage's timing on screen, and
<http://localhost:8000> to record. Allow the microphone.

## The script

Choose English and speak these in one conversation. It runs about 3 minutes.

| # | You say | Saathi does | Point at |
|---|---|---|---|
| 1 | "I need rice and milk" | Asks which pack of rice | The options, from the catalogue |
| 2 | "First" | Asks which milk | |
| 3 | "First" | Reads back items, fees, total, address, cash on delivery | The total and address |
| 4 | "Book a table for two in Indiranagar tomorrow at 7 PM, Italian" | Keeps the groceries and offers restaurants | **Your tasks:** Groceries says Waiting |
| 5 | "First" | Reads back the table, date, time, and that it is free | |
| 6 | "Yes" | Books the demo table and says it was not real | Groceries still says Waiting |
| 7 | "Back to my groceries" | Reads the basket back afresh | |
| 8 | "Yes" | Places the demo order and says it was not real | The demo label |
| 9 | "I want something for dinner" | Asks what you feel like | |
| 10 | "Chicken biryani" | Offers it from two restaurants, with sizes | |
| 11 | "The second one" | Reads it back | |
| 12 | "Yes" | Places the demo order | |

Then start a new conversation in Kannada and say `ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ ಮತ್ತು ಒಂದು ಲೀಟರ್ ಹಾಲು`, then
`ಹೌದು`. For Hindi: `दो किलो चावल और एक लीटर दूध`, then `हाँ`.

Say this at the end: "This recording uses mock shops. The same code has read live Swiggy data, and
one rehearsal changed my real Instamart cart. No real order or booking has been placed, because I
am asking for that access now."

## A draft without a live take

```bash
uv run --with playwright python scripts/record_demo.py namma-saathi-demo.mp4
```

It drives a real browser, types the same script with captions, and records Saathi's real voice. It
takes several minutes. It is a fair backup, but your own voice at the microphone shows the product
better, since this is a voice assistant.

## If something fails during a take

| You see | Do this |
|---|---|
| Yellow "scripted understanding" note | Every LLM failed and the script took over. Stick to the script, or wait a minute and retake |
| "I could not understand that" | Say it again. Prefer the exact words above |
| "Please try again" or a 502 | Say it again. Recognition already retried once |
| No voice, but text appears | Gnani's voice is down. Check `GNANI_API_KEY`, or wait and retake |
| Microphone error | Use **Or type it**, and say so on camera |
| Groq rate limit in the log | Pause a minute. Gemini takes over, slower, at 5 to 13 seconds |
| Anything else | **Start a new conversation**. Mock shops reset, so nothing is lost |

## Before recording

- [ ] `.env` says `COMMERCE_BACKEND=mock`, and the corner badge says **Demo order**
- [ ] `app.demo_check` finished within the last few minutes
- [ ] Only this browser window is shared; no `.env`, terminal, tokens, or notifications
- [ ] No real address or phone number on screen. The demo address is made up
- [ ] Your voice and Saathi's are both audible: record system audio and the microphone
- [ ] Test ten seconds and play it back

## Before submitting the link

- [ ] Watch the whole video, with sound, after uploading
- [ ] YouTube: **Unlisted**. Drive: **Anyone with the link, Viewer**. Loom: public link
- [ ] Open the link in a private window while signed out. It must play without asking for access
- [ ] The video says that the shops are mock
