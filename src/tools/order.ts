// src/tools/order.ts
//
// Order placement is irreversible and moves money. The MCP exposes it as a
// dedicated tool that requires every coordinate and SKU explicitly — no
// "smart" defaults. Spec philosophy from ostja-bot/spec-ostja-bot.md:
//
//   "Bot küsib alati kinnitust enne makse sooritamist — automaatset
//    makset ilma 'jah'-ta ei toimu kunagi"
//
// LLM must surface the full order to the user and get explicit confirmation
// in chat before calling place_order. This tool itself does not enforce that
// (no UI affordance available) — it relies on the wrapping skill / system
// prompt to gate the call.

import { z } from 'zod';
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { WoltClient } from '../wolt/client.js';

export function registerOrderTools(
  server: McpServer,
  client: WoltClient,
): void {
  server.tool(
    'place_order',
    [
      'Place a Wolt order. IRREVERSIBLE — moves money. Requires a saved session token.',
      'IMPORTANT: only call after explicit user confirmation in chat showing total price, venue, items, and delivery address.',
      'Returns { success, order_id, message }.',
    ].join(' '),
    {
      venue_slug: z
        .string()
        .describe('Venue slug, e.g. "lido-rotermann" — from search_venues'),
      items: z
        .array(
          z.object({
            item_id: z.string().describe('Menu item id from get_venue_menu'),
            count: z.number().int().min(1).default(1),
            modifiers: z.array(z.unknown()).optional(),
          }),
        )
        .min(1)
        .describe('Items to order — at least one'),
      delivery_lat: z
        .number()
        .describe('Delivery latitude (use get_default_delivery_address)'),
      delivery_lon: z.number().describe('Delivery longitude'),
    },
    async (params) => {
      const result = await client.placeOrder(
        params.venue_slug,
        params.items,
        { lat: params.delivery_lat, lon: params.delivery_lon },
      );
      return {
        content: [
          { type: 'text' as const, text: JSON.stringify(result, null, 2) },
        ],
      };
    },
  );
}
