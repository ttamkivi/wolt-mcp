# wolt-mcp v0.7

MCP server for Wolt food delivery. Inspired by [martparve/selver-mcp](https://github.com/martparve/selver-mcp).

> **History note (23.08.2026):** this repo briefly forked into two directions — a
> TypeScript rewrite mirroring selver-mcp's structure (`git checkout
> ts-rewrite-attempt`), and this Python implementation, developed in parallel in
> `~/Downloads/wolt-mcp` and actually wired into Claude Desktop. The Python side
> won on capability (venues, menu, orders, baskets, payment methods, checkout
> prep, audit log) and is now the canonical copy, moved here from Downloads. The
> TS attempt is preserved on its branch, not deleted, in case it's worth
> resuming later.

**v0.1**: anonymous tools — search venues, browse menus, virtual cart, deeplink.
**v0.2**: authenticated tools — order history, payment methods, pickup slots, place pickup orders (two-step dry-run/confirm).
**v0.3 adds**: home delivery — saved addresses (pre-seeded with Salv office at Veerenni 38), delivery slots, place_delivery_order, default payment method.

> ⚠️ **Order placement violates Wolt's Terms of Service** in the strict sense (automated/programmatic access). Use sparingly: pickup-only, low volume (a few orders a week), and always confirm the dry-run output before submitting. Mitigation: every `place_pickup_order` call defaults to `dry_run=True` and refuses to submit without a matching `confirm_token`.

## Tools

### Anonymous (no session needed)
- **search_venues(query, lat, lon, max_estimate_min)** — find open restaurants
- **get_venue(slug)** — venue details
- **get_venue_menu(slug)** — full menu with options
- **find_items(slug, query)** — search a venue's menu
- **add_to_cart / view_cart / remove_from_cart / clear_cart** — local virtual cart
- **get_deeplink(slug?)** — Wolt web/app deeplink

### Authenticated (requires `set_session` first)
- **set_session(token, kind="bearer")** — save your Wolt JWT or cookie
- **get_session_status()** — check if a token is saved
- **get_my_orders(limit, days_back)** — order history. Use this to compute favorites in the agent.
- **get_payment_methods()** — your saved cards
- **set_default_payment_method(payment_method_id)** — save a default card
- **get_pickup_slots(slug, date_iso)** — when can you pick up
- **place_pickup_order(...)** — submit pickup order (two-step: dry_run → confirm_token)

### Delivery (v0.3)
- **set_delivery_address(label, address, lat, lon, floor?, instructions?, set_default?)** — save a named address
- **get_delivery_address(label?)** — fetch a saved address (default if no label)
- **list_delivery_addresses()** — see all saved addresses
- **get_delivery_slots(slug, date_iso?, address_label?)** — delivery times for a venue → address
- **place_delivery_order(slug, items, delivery_time_iso, payment_method_id?, address_label?, dry_run, confirm_token)** — submit delivery order (two-step)

Pre-seeded address: `salv-office` → Veerenni 38, Tallinn. To use a different default, call `set_delivery_address(... set_default=True)`.

## Install

```bash
unzip wolt-mcp.zip && cd wolt-mcp
pip install -e .       # or: uv pip install -e .
python wolt_mcp.py     # sanity check — Ctrl-C to exit
```

Wire into Claude Desktop. Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "wolt": {
      "command": "python",
      "args": ["/absolute/path/to/wolt-mcp/wolt_mcp.py"]
    }
  }
}
```

Restart Claude Desktop.

## Get your Wolt session token

**Recommended: cookie mode — set it up once, it renews itself.**

1. Open `https://wolt.com` in Chrome, log in.
2. Open DevTools (Cmd+Opt+I) → **Network** tab.
3. Click any restaurant or refresh the page so requests appear.
4. Click any request to `consumer-api.wolt.com`.
5. **Headers** → **Request Headers** → find the `cookie:` line.
6. Copy the **entire value** (it's long — includes `__wtoken`, `__wrtoken`, and others).

In Claude Desktop:

> Save this Wolt cookie session: `<paste the full cookie: value>`

Claude calls `set_session(token="<cookie string>", kind="cookie")`.

From then on, every authenticated call captures whatever `Set-Cookie` rotation
Wolt sends back (the same mechanism that keeps a real browser tab logged in)
and persists it to `~/.wolt-mcp/session.json` (mode 600, owner-only). Check
`get_session_status()` — a cookie-mode session reports `"mode":
"self-renewing"` and a `last_renewed_at` timestamp. You should only need to
repeat this whole procedure if a call starts returning 401, which means
`__wrtoken` itself (not just the short-lived access cookie) finally expired.

**Legacy: bearer mode.** Grab just the `authorization: Bearer ey...` header
instead and call `set_session(token="ey...")` (kind defaults to `"bearer"`).
This JWT is short-lived and does **not** self-renew — you'll be back here
re-extracting it every time it expires, which is the whole reason cookie
mode exists. Prefer cookie mode unless you have a specific reason not to.

## Use it — favorites + pickup order flow

```
You: What are my Wolt favorites — top venues and most-ordered items?

Claude: [calls get_my_orders(limit=200)]
        [groups by venue and item, counts, computes patterns]

        Top venues (last 12 months):
          Vapiano — 14 orders, avg 18.50€
          Tokyo — 9 orders, avg 22.10€
          Lendav Taldrik — 7 orders, avg 16.80€

        Most-ordered items:
          Pollo e Funghi (Vapiano) — 8x
          Spicy Tuna Roll 8pc (Tokyo) — 6x
          ...

You: Order me lunch from Vapiano tomorrow, pickup at 12:30. Usual.

Claude: [checks calendar via google-calendar MCP]
        Tomorrow 11:00–14:00 you're at Salv office. ✓

        [calls get_payment_methods → picks default Visa ****1234]
        [calls get_pickup_slots(slug='vapiano-tallinn', date='2026-04-28')]
          → 12:15, 12:30, 12:45 available
        [calls add_to_cart with Pollo e Funghi]
        [calls place_pickup_order(... dry_run=True ...)]

        Planned order at Vapiano:
          1× Pollo e Funghi — 12.50€
          1× San Pellegrino 0.33L — 3.00€
        Subtotal: 15.50€  • Pickup 12:30  • Visa ****1234

        Confirm? Say "submit" to place it.

You: Submit.

Claude: [calls place_pickup_order(... dry_run=False, confirm_token='8996...')]
        ✓ Order placed. Pickup at 12:30, Vapiano (Vabaduse väljak 8).
```

## Calendar awareness

If you have Google Calendar MCP connected, ask Claude to check before ordering:

> "Check my calendar for tomorrow afternoon — am I in Tallinn at the office? If yes, order pickup lunch from my favorite Vapiano dish at 12:30."

Claude will call `list_events` first, confirm location, then proceed with the order flow.

## Cart state

- `~/.wolt-mcp/cart.json` — virtual cart (anonymous flow)
- `~/.wolt-mcp/session.json` — your token (mode 600)

One venue at a time in the cart. Adding from a different venue resets it.

## Limitations

- **No nutrition macros.** Wolt menus don't expose kcal/protein. Claude estimates from item names.
- **Endpoint stability.** Authenticated endpoints are reverse-engineered from the Wolt web app. If `get_my_orders` or `place_pickup_order` returns a weird error, the URL or payload shape may have changed. Open DevTools, capture a real request, and adjust `WoltClient.my_orders` / `WoltClient.place_order` accordingly.
- **TOS risk.** As above. Pickup-only and low volume keeps risk down.
- **Pickup support varies.** Not all venues offer takeaway. `get_pickup_slots` will return `[]` if pickup isn't enabled.
- **Geo defaults to Tallinn.** Pass `lat`/`lon` for Helsinki (60.1699, 24.9384), Stockholm (59.3293, 18.0686), etc.

## Troubleshooting

**"No Wolt session token saved"** → run `set_session(token=...)` first.

**401 Unauthorized** → token expired. Re-extract from DevTools and `set_session` again.

**403 Forbidden on anonymous endpoints** → Cloudflare bot detection. Reduce request rate, ensure User-Agent header is set (it is by default).

**`place_pickup_order` returns 4xx with strange error** → Wolt may have changed the order endpoint. Capture a real takeaway order via DevTools, compare URL + payload to `WoltClient.place_order`, and adjust.

## Repo layout

```
wolt-mcp/
├── wolt_mcp.py                       # the whole server, single file (~700 lines)
├── pyproject.toml
├── README.md
└── claude_desktop_config.example.json
```

## License

MIT.
