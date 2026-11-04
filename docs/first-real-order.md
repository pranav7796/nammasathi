# First real order

These steps use Instamart. Food follows the same steps, and also uses
`ALLOW_SWIGGY_CART_WRITES` and `ALLOW_REAL_CHECKOUT`, but neither its cart writes nor its
checkout have run live yet. A real table booking has its own switch, `ALLOW_REAL_BOOKING`,
and has not run live either. Rehearse each one separately.

This procedure places real money on a real order. Nothing in it runs by itself.
Only Pranav turns on the switches, and each stage stops if anything looks wrong.

## Before you start

- [ ] Swiggy's access position is settled. Their docs describe production as
      invite-based, following staging, with confirmation from builders@swiggy.in.
- [ ] You have checked the basket in the Swiggy app. **Saathi keeps what is already
      there, so those items become part of the order unless you remove them.**
      Emptying it first in the app is simplest.
- [ ] The basket is empty or set to the address you intend to use.
- [ ] You have read the known limitations in the README.

## 1. Verify the code and login

```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
node --test tests/frontend/ajji-mart.test.cjs
uv run python -m app.swiggy_preflight
```

The preflight must report `0 blocker(s)`. It is read-only. If login has expired, run
`uv run python -m app.swiggy_auth`.

## 2. Rehearse with the real cart, without checkout

This is the first time Saathi writes to the real cart.

```dotenv
COMMERCE_BACKEND=swiggy
SWIGGY_LIVE_ACCESS=true
ALLOW_SWIGGY_CART_WRITES=true
ALLOW_REAL_CHECKOUT=false
```

```bash
uv run uvicorn app.web:app      # no --reload, ever, with a real account
```

Open <http://localhost:8000>, then:

1. Choose the address and ask for one small item.
2. Answer any question Saathi asks. Check the readback against the Swiggy app: items,
   packs, prices, fees, total, address.
3. Tap **Cancel this basket** and confirm in the app that the cart returned to how it was.

Stop here if anything differs. Check the terminal log for `address_mismatch` or
`cart_differs_from_request`.

## 3. Place the one order

Choose something inexpensive and above the ₹99 minimum, from one store.

```dotenv
ALLOW_REAL_CHECKOUT=true
MAX_REAL_ORDER_INR=300          # refuse anything above this total
REAL_ORDER_LIMIT=1              # refuse a second order
```

Restart the server, then speak the request. Listen to the readback in full and say
yes (`ಹೌದು`, `हाँ`) or tap **Confirm real Swiggy order**. Stay at the screen:
checking an unclear result can take about 30 seconds.

## 4. Afterwards

Whatever happened, turn the switches off and restart:

```dotenv
ALLOW_REAL_CHECKOUT=false
ALLOW_SWIGGY_CART_WRITES=false
```

Then run `uv run python -m app.order_admin status`, and check the Swiggy app.

## What Saathi may tell you

| Saathi says | Meaning | What to do |
|---|---|---|
| Order placed and checked | Checkout reported it and order history shows it | Confirm in the Swiggy app |
| Placed but could not check it | Checkout reported an order that could not be found afterwards | **Do not order again.** Check the app |
| Only part was placed | A multi-store cart partly failed | Check the app. Saathi will not retry |
| Could not confirm whether it was placed | The outcome is unknown, and checkout is now blocked | **Do not order again.** Follow the steps below |
| Swiggy did not accept it | Refused, and order history shows no new order | A fresh readback follows, and a new yes may retry |
| Did not place the order | A check failed before anything was sent | Nothing to reconcile |

### Settling an unknown outcome

Check the Swiggy app, then record what you found:

```bash
uv run python -m app.order_admin status
uv run python -m app.order_admin resolve <attempt_id> placed --order-id <id>
# or
uv run python -m app.order_admin resolve <attempt_id> not-placed
```

To cancel an order, call Swiggy customer care on 080-67466729. There is no cancel API.
