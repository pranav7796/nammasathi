# Swiggy integration notes

Verified behaviour of the Instamart MCP server. The public docs are the contract;
this file records what they omit, as observed with read-only calls on 4 November
2026. Values were not recorded, only shapes.

Docs: <https://mcp.swiggy.com/builders/docs/>. Key pages:
[checkout](https://mcp.swiggy.com/builders/docs/reference/instamart/checkout.md),
[update_cart](https://mcp.swiggy.com/builders/docs/reference/instamart/update_cart.md),
[get_cart](https://mcp.swiggy.com/builders/docs/reference/instamart/get_cart.md),
[get_payment_options](https://mcp.swiggy.com/builders/docs/reference/instamart/get_payment_options.md),
[get_orders](https://mcp.swiggy.com/builders/docs/reference/instamart/get_orders.md),
[errors](https://mcp.swiggy.com/builders/docs/reference/errors.md),
[ship to production](https://mcp.swiggy.com/builders/docs/build/ship-to-production.md),
[rate limits](https://mcp.swiggy.com/builders/docs/operate/rate-limits.md),
[access](https://mcp.swiggy.com/builders/docs/operate/access.md).

## Observed live

| Topic | Observation | Consequence in code |
|---|---|---|
| Address IDs | `get_addresses` returns `<id>__<token>`; the cart's `selectedAddressDetails.id` is the bare `<id>`. The full ID was stable across calls and sessions. | Compare with `same_address_id`; always send the full ID. Exact comparison would block every order. |
| Pack size | Variation `displayName` repeats the product name. `quantityDescription` holds the pack: `500 ml`, `1 ltr`, `5 kg`, `500 ml x 4`. | `app/commerce/units.py` parses it; the `x 4` multiplies. |
| Payment | `get_cart` omits `availablePaymentMethods`. `get_payment_options` returns `cod: {available, id: "COD", displayName: "Pay on delivery"}`. | COD comes from `get_payment_options`. |
| `agenticPaymentEligible` | `false` on this account while `cod.available` is `true`. Meaning for COD is undocumented. | Not used. Watch this at the first real attempt. |
| Bill | `lineItems` hold `Item Total` and fees (handling, delivery partner, rain), some with paise. `toPay.value` looks like `₹999`. | Money is `Decimal`; everything above the item total is read out as fees. |
| Cart coordinates | `selectedAddressDetails` includes `lat` and `lng`, undocumented, and `track_order` needs them. | Used as the second verification source. |
| Search | Native script works (`ಹಾಲು`, `दूध`), `haalu` works, `akki` returned noise. 20 products per page. | LLM `search_term`, a vocabulary table, and a name filter. |

## Not yet observed

These need a live write or a populated history, which has not been authorized.
The code fails closed on each.

- A `get_orders` response containing an order (history was empty).
- A real `update_cart` or `checkout` response, including `selectedAddressDetails`
  after a write and whether `removedOutOfStockItems` or `reducedQuantityItems` appear.
- An empty cart's shape. Handled for both `cartAbsent` and an empty `items`.
- Whether `discountedFinalPrice` is per unit or per line at quantity above one.
  All observed lines had quantity 1. The adapter checks it against `Item Total`.
- The exact staging path. The docs say `mcp-staging.swiggy.com/{server}`, and
  `/im` is assumed from the production path. Confirm it with Swiggy.

## Access

The docs describe v1 as invite-based, with production following at least 48 hours of
green staging and confirmation from builders@swiggy.in. The go-live checklist applies
before real-user traffic. The app keeps real checkout off until Pranav decides.
