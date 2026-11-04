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

## Observed in the live rehearsal (4 November 2026)

| Topic | Observation | Consequence in code |
|---|---|---|
| Writes | `update_cart` worked from localhost with the full address ID, and the cart's address matched. Readback total matched the Swiggy app. | Cart writes are proven; ordering is not. |
| Emptying | Cancelling back to an empty cart failed when it used `update_cart` with no items. The exact response was not captured. | Emptying uses `clear_cart`, then `get_cart` to prove it. Not yet re-run live. |
| Bill | `Extra Offer Savings` is negative, `Delivery Partner Fee` can be `FREE`, and `Rain Fee` appeared later, moving the total from ₹508 to ₹544 within minutes. | Fees and savings read from the bill lines; preflight before checkout catches changes. |
| Price | At quantity 2, `discountedFinalPrice` is per unit. | Confirms the default. |
| Pack label | `itemVariant` can say `1 kg` where the app shows `500 g x 2`. | Same amount; read back as Swiggy's API reports it. |
| Refusal | `get_cart` was refused as not serviceable twice around 00:37 IST on 5 November, then succeeded on 10 of 10 reads from 00:38 IST. Swiggy's wording was not captured. Cause unknown. | Session start retries once; the reason is now logged with digits masked. |
| Address | `selectedAddressDetails.flatNo` holds the flat number, separate from `address`. | Included in the readback. |

## Not yet observed

These need a live checkout or a populated history, which has not been authorized.
The code fails closed on each.

- A `get_orders` response containing an order (history was empty).
- A real `checkout` response, and whether `removedOutOfStockItems` or
  `reducedQuantityItems` appear after `update_cart`.
- An empty cart's shape. Handled for both `cartAbsent` and an empty `items`.
- The exact staging path. The docs say `mcp-staging.swiggy.com/{server}`, and
  `/im` is assumed from the production path. Confirm it with Swiggy.

## Access

The docs describe v1 as invite-based, with production following at least 48 hours of
green staging and confirmation from builders@swiggy.in. The go-live checklist applies
before real-user traffic. The app keeps real checkout off until Pranav decides.
