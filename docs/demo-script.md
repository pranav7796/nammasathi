# Demo script

One assistant, three everyday tasks. Everything here runs on the built-in mock services, and
the page labels it as a demo. Nothing reaches Swiggy.

## Setup

```bash
# In .env: COMMERCE_BACKEND=mock, plus GNANI_API_KEY and GEMINI_API_KEY
uv sync
uv run uvicorn app.web:app
```

Open <http://localhost:8000>, allow the microphone, and choose a language. Check your LLM quota
first: Gemini and Groq both ran out during testing. In the mock, ask only for what it stocks.

## 1. Groceries

| You say | Saathi does |
|---|---|
| "Two kilos of rice and one litre of milk" | Works out the packs and reads back items, fees, total, address, and payment |
| "Yes" (`ಹೌದು`, `हाँ`) | Places the demo order, checks it, and says it was a demo |

## 2. A meal

| You say | Saathi does |
|---|---|
| "I want something for dinner" | Asks what you feel like eating |
| "Chicken biryani" | Offers it from two restaurants and asks which |
| "The second one" | Reads back the order |
| "Yes" | Places the demo order |

## 3. A table

| You say | Saathi does |
|---|---|
| "Book a table for two in Indiranagar tomorrow at seven, Italian" | Offers three restaurants |
| "First" | Finds that time and reads back the booking, free with nothing to pay |
| "Yes" | Books the demo table |

## Moments worth showing

- **Changing your mind:** add groceries, then ask for a table. Saathi keeps the groceries, and
  a yes only applies to the table.
- **Saying no:** say `ಬೇಡ` at a readback. Nothing is ordered.
- **Honesty:** after any order, point at the label saying it was a demo.

## What to say about it

Say plainly that this run uses mock services. Live Swiggy data has been read through the same
code. The real cart has been changed once, in an Instamart rehearsal, and no real order or
booking has been placed.
