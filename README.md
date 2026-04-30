# wolt-mcp

A clean MCP server for Wolt — venue search, menu lookup, delivery addresses, and order placement. Pairs with Claude Desktop, Claude Code, or any MCP-aware client.

Built on top of the (unofficial) Wolt JSON API. **Personal use only** — Wolt does not publish a public API; this MCP wraps the same endpoints the Wolt website uses, with your own session token.

## What it does

- **Find venues** by name or cuisine near you
- **Browse menus** by venue oid
- **Item search** across nearby venues with optional max price
- **Read your saved delivery addresses** (from your Wolt account)
- **Place an order** — irreversible, requires explicit confirmation in chat

## Architecture

```
src/
  index.ts           ← MCP server entry (stdio transport)
  wolt/
    client.ts        ← WoltClient — port of ostja-bot/wolt.py with typed responses
    types.ts         ← Venue, MenuItem, SearchItem, DeliveryAddress, OrderResult
  storage/
    session-token.ts ← persists JWT at ~/.wolt-mcp/session.json (chmod 600)
  tools/
    search.ts        ← search_venues, get_venue_menu, search_items
    session.ts       ← set_session, get_session_status, clear_session
    address.ts       ← list_delivery_addresses, get_default_delivery_address, set_search_location
    order.ts         ← place_order (irreversible, gated by skill prompt)
skills/
  wolt-order/SKILL.md ← workflow + confirmation discipline
```

## Install

### 1. Prerequisites

- Node.js 18+ (`node --version`)
- A Wolt account with a recent web/app session (to extract a JWT)

### 2. Build

```bash
cd ~/Desktop/Personal-Brain/wolt-mcp
npm install
npm run build
```

This produces `dist/index.js`.

### 3. Wire into Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` and add a `wolt-mcp` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "wolt-mcp": {
      "command": "node",
      "args": ["/Users/YOUR_NAME/Desktop/Personal-Brain/wolt-mcp/dist/index.js"]
    }
  }
}
```

Quit and reopen Claude Desktop.

### 4. Wire into Claude Code (alternative)

```bash
claude mcp add wolt-mcp node ~/Desktop/Personal-Brain/wolt-mcp/dist/index.js
```

### 5. Install the order skill (recommended)

```bash
mkdir -p ~/.claude/skills/wolt-order
cp ~/Desktop/Personal-Brain/wolt-mcp/skills/wolt-order/SKILL.md ~/.claude/skills/wolt-order/SKILL.md
```

The skill teaches Claude how to do the search-propose-confirm-place dance correctly. Without it, Claude can still call individual tools, but you'd need to manage confirmation in your prompt.

## First-time setup — get a session token

Wolt's tokens are JWTs found in browser localStorage / app storage after you sign in.

1. Open https://wolt.com/ in Chrome and sign in.
2. Open DevTools → Application → Local Storage → `https://wolt.com`.
3. Find the entry with a long JWT (starts with `eyJ...`).
4. Copy the token.
5. In Claude, ask: "Save my Wolt session token: eyJ..."
   The skill calls `set_session` and stores it at `~/.wolt-mcp/session.json` (chmod 600).

Tokens typically expire in ~30 minutes for the access token, but Wolt's web flow auto-refreshes — for now, this MCP only stores the access token, so you'll need to repeat this when it expires. Refresh-token support is a future enhancement.

## Usage

> "Find me a burger under €12 on Wolt"

Claude will call `search_items` and show 3 options.

> "Order option 1"

Claude will fetch your delivery address, summarize the order with total price, and ask you to confirm. After "yes", it calls `place_order`.

> "What's my Wolt session status?"

Claude calls `get_session_status` — shows token validity and expiry.

## Tools reference

| Tool | Auth | Description |
|---|---|---|
| `search_venues` | optional | Search venues by query, returns up to 5 |
| `get_venue_menu` | optional | Fetch menu by venue oid |
| `search_items` | optional | Combined: search venues then filter their menus |
| `set_session` | — | Save JWT to `~/.wolt-mcp/session.json` |
| `get_session_status` | — | Decode JWT locally, show expiry |
| `clear_session` | — | Delete saved JWT |
| `list_delivery_addresses` | required | All saved Wolt addresses |
| `get_default_delivery_address` | required | First saved address |
| `set_search_location` | — | Override search lat/lon for this session |
| `place_order` | required | **Irreversible.** Place an order at one venue |

## Why a separate MCP from `ostja-bot`?

The original `ostja-bot/wolt.py` was part of a Telegram-fronted multi-store agent. Pulling it out as a standalone MCP gives:

- **Reuse** — the same MCP works with Claude Desktop, Claude Code, Cowork, or any MCP host without dragging the Telegram bot along.
- **Clean tool boundaries** — each operation is a separate MCP tool with a typed schema, instead of methods on a Python class.
- **Token persistence on disk** rather than as a process-bound `.env`.
- **TypeScript** — same toolchain as `selver-mcp`, single mental model for both groceries and food delivery.

The original Python code is being retired — see migration note below.

## Migration from ostja-bot/wolt.py

If you have an old `ostja-bot/wolt.py`:

```bash
rm ~/Desktop/Personal-Brain/personal-developments/intel-fusion/ostja-bot/wolt.py
```

The new MCP is a superset (search, menu, items, addresses, order) plus session management and JWT introspection.

## Security notes

- The JWT contains your Wolt account access. Treat `~/.wolt-mcp/session.json` like a password file — it's chmod 600 by default.
- **Order placement moves money.** The MCP does not enforce confirmation by itself — the wrapping skill (`wolt-order/SKILL.md`) does, by instructing Claude to summarize + ask first. If you bypass the skill, you're responsible for confirmation.
- This is an unofficial wrapper around Wolt's internal API. Wolt may rate-limit or block accounts that abuse it. Use at human-pace volumes.

## Updating

```bash
cd ~/Desktop/Personal-Brain/wolt-mcp
git pull          # if you've put it under version control
npm install
npm run build
```

Then quit and reopen Claude Desktop.

## Uninstall

Remove the `wolt-mcp` entry from `claude_desktop_config.json`, delete `~/Desktop/Personal-Brain/wolt-mcp`, and `rm ~/.wolt-mcp/session.json` to clear your saved token.

## License

Personal use. No warranty. Wolt's TOS apply.
