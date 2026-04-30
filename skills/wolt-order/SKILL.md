---
name: wolt-order
description: Use this skill when the user wants to order food from Wolt — search a restaurant, view its menu, place an order. Coordinates the wolt-mcp tool calls and enforces an explicit confirmation step before any irreversible action (order placement, money movement).
---

# Wolt Order Workflow

This skill drives ordering through the **`wolt-mcp`** server. Wolt order placement moves money, so the workflow is split into a clearly reversible "search & propose" phase and a single irreversible "place" step.

## Trigger

- "Order me X from Wolt"
- "Telli mulle Woltist..."
- "Find me a burger under €12"
- "What's the cheapest sushi nearby on Wolt?"

## Phase 1 — Search & propose (always reversible)

1. **Establish location.** If the user has a saved Wolt session, call `get_default_delivery_address` to get lat/lon. Otherwise, ask for an address or use the default Tallinn centre coordinates.
2. **Search.** For specific items, prefer `search_items` (it filters menus). For browsing, use `search_venues` then `get_venue_menu` for one of them.
3. **Propose 1–3 options** with: item name, venue, price, delivery time and price, total. Include the Wolt URL so the user can sanity-check.

## Phase 2 — Confirm and place (irreversible)

**Never** call `place_order` without an explicit "yes" / "telli" / "1" in chat AFTER showing:

- Venue name and slug
- Items (name, qty, price each)
- Total price including delivery
- Delivery address

If the user has no saved session token (`get_session_status` returns `has_token: false` or `valid: false`), do NOT attempt to place. Instead, give them the venue URL and instructions to either:
1. Paste their JWT into `set_session`, or
2. Order manually via the URL.

After `place_order` returns success, surface the `order_id` and the venue URL so they can track in the Wolt app.

## Auth troubleshooting

- `get_session_status` shows `valid: false` → JWT expired. Tell user to paste a fresh one via `set_session`. JWT pattern: long `eyJ...` string from Wolt web/app session.
- Address fetch errors with HTTP 401 → same problem.

## Common pitfalls

- **Ordering across venues.** Wolt's cart is per-venue. Don't try to combine items from two venues into one `place_order` call.
- **Price units.** Wolt API returns prices in cents internally; `wolt-mcp` converts to EUR. Always show EUR with 2 decimals to the user.
- **Multilingual venue/item names.** API may return localized objects (`{et: "...", en: "..."}`); `wolt-mcp` already flattens these — just trust the `name` field.
- **Tallinn-only default.** Default search lat/lon is Tallinn centre. Override via `lat` / `lon` parameters or `set_search_location` for other cities.

## Example session

> User: "Order me a burger under €12 from Wolt"

1. `search_items({ query: "burger", max_price: 12 })` → 3 options
2. Show user: "Found 3:
   - Big Mac, McDonald's Viru — €8.50 (delivery 15 min, €1.99)
   - Classic burger, Hesburger — €7.20 (delivery 20 min, €1.49)
   - Wagyu burger, Burger Box — €11.50 (delivery 30 min, €2.99)
   Which one?"
3. User: "1" → `get_default_delivery_address` → confirm "Big Mac at McDonald's Viru, total €10.49 to Hobujaama 5. Place order?"
4. User: "yes" → `place_order({ venue_slug: "mcdonalds-viru", items: [{item_id:"...", count:1}], delivery_lat: 59.43..., delivery_lon: 24.75... })`
5. Surface `order_id` and `https://wolt.com/...` for tracking.
