# Namma Saathi: solution pitch

**Your personal AI for everyday life. Tell Namma Saathi what you need. It gets it done.**

Live demo: <https://namma-saathi-7b62.onrender.com> · Code: <https://github.com/pranav7796/nammasathi>

## The problem

Ordering groceries, booking a table or replying to an email means opening an app, reading small
English text and tapping through many screens. For millions of people in India, including older
family members and anyone more at home in Kannada or Hindi than in English, that is the barrier.
Speaking is not. Namma Saathi began as Ajji Mart: a way for a grandmother to order groceries by
simply asking.

## The solution

A voice-first assistant. You say what you need in Kannada, Hindi or English, and Saathi does the
errand in one conversation:

- **Groceries, a meal, a table**, built on Swiggy Instamart, Food and Dineout.
- **Gmail and Google Calendar**: find important mail, draft a reply, check your day, set up a meeting.
- **Reminders and notes**, kept under "My day".

Every task follows the same three steps: **say it, hear it back, say yes.** Saathi reads back the
exact items, prices, total and address, and nothing happens until you confirm that exact readback.

## How it works

**Gnani Prisma (speech to text) → LLM understanding → deterministic state machine → service
adapter → Gnani Timbre (text to speech)**

The LLM only turns speech into a structured request. It has no tools and cannot place an order,
choose a price or decide that you agreed. Deterministic code owns every step, binds your yes to
the exact cart you heard, takes prices only from the shop's data, and never blindly retries a
checkout. This makes a voice assistant safe to trust with money.

## What works today

- All three ordering journeys run end to end in three languages on built-in demo shops, with real
  Gnani speech and a real LLM. Anyone can try them on the live site.
- Accounts, per-person history and a public waitlist are live.
- Gmail and Calendar work for early-access testers.
- Against real Swiggy: sign-in, search and cart readback are verified. **No real order has been
  placed.** Real ordering opens when Swiggy grants production access.

## Why it matters

Voice in a person's own language turns "ask someone to do it for me" into "I did it myself".
The same safe pattern, say it, hear it back, say yes, extends to any everyday service.

**Built with:** Gnani Prisma and Timbre, Python, FastAPI, Postgres, Swiggy MCP, Google APIs.
Built for the Gnani AI Great Indian AI Internship Challenge.
