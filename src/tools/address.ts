// src/tools/address.ts
//
// Address tools — read-only: list saved addresses on the user's Wolt account,
// or fetch the default one. Address mutation (add/edit) is intentionally not
// exposed: the user should manage that in the Wolt app.

import { z } from 'zod';
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { WoltClient } from '../wolt/client.js';

export function registerAddressTools(
  server: McpServer,
  client: WoltClient,
): void {
  server.tool(
    'list_delivery_addresses',
    'List all delivery addresses saved on the authenticated Wolt account. Requires a valid session — see set_session.',
    {},
    async () => {
      try {
        const list = await client.listDeliveryAddresses();
        return {
          content: [
            { type: 'text' as const, text: JSON.stringify(list, null, 2) },
          ],
        };
      } catch (e) {
        return {
          content: [
            {
              type: 'text' as const,
              text: JSON.stringify({ error: (e as Error).message }),
            },
          ],
        };
      }
    },
  );

  server.tool(
    'get_default_delivery_address',
    'Return the first delivery address on the Wolt account (used as the default for ordering).',
    {},
    async () => {
      try {
        const addr = await client.getDeliveryAddress();
        return {
          content: [
            { type: 'text' as const, text: JSON.stringify(addr, null, 2) },
          ],
        };
      } catch (e) {
        return {
          content: [
            {
              type: 'text' as const,
              text: JSON.stringify({ error: (e as Error).message }),
            },
          ],
        };
      }
    },
  );

  // Caller-provided lat/lon for an ad-hoc delivery point (e.g. office). Stored
  // in-memory on the client; not persisted across restarts. Use Wolt app for
  // permanent saved addresses.
  server.tool(
    'set_search_location',
    'Override default lat/lon used by venue searches (default: Tallinn centre). In-memory only; reset on MCP restart.',
    {
      lat: z.number().describe('Latitude'),
      lon: z.number().describe('Longitude'),
    },
    async (params) => {
      client.lat = params.lat;
      client.lon = params.lon;
      return {
        content: [
          {
            type: 'text' as const,
            text: JSON.stringify({ ok: true, lat: client.lat, lon: client.lon }),
          },
        ],
      };
    },
  );
}
