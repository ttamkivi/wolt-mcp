// src/tools/search.ts
import { z } from 'zod';
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { WoltClient } from '../wolt/client.js';

export function registerSearchTools(
  server: McpServer,
  client: WoltClient,
): void {
  server.tool(
    'search_venues',
    'Search Wolt venues (restaurants, shops) by query. Returns up to 5 venues with name, slug, oid, rating, delivery time and price.',
    {
      query: z.string().describe('Search term, e.g. "burger", "sushi", "lido"'),
      lat: z.number().optional().describe('Override latitude (defaults to Tallinn centre)'),
      lon: z.number().optional().describe('Override longitude'),
    },
    async (params) => {
      try {
        const venues = await client.searchVenues(
          params.query,
          params.lat,
          params.lon,
        );
        return {
          content: [
            { type: 'text' as const, text: JSON.stringify(venues, null, 2) },
          ],
        };
      } catch (e) {
        return {
          content: [
            {
              type: 'text' as const,
              text: JSON.stringify({
                error: (e as Error).message,
                fallback_url: `https://wolt.com/et/est/tallinn?search=${encodeURIComponent(params.query)}`,
              }),
            },
          ],
        };
      }
    },
  );

  server.tool(
    'get_venue_menu',
    'Fetch the full menu for a venue by its oid. Returns items with item_id, name, description, price, image, and category.',
    {
      oid: z.string().describe('Venue oid (object id) from search_venues'),
    },
    async (params) => {
      try {
        const items = await client.getMenu(params.oid);
        return {
          content: [
            { type: 'text' as const, text: JSON.stringify(items, null, 2) },
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
    'search_items',
    'High-level item search across nearby venues. Searches venues by query, fetches up to 3 menus, filters items matching the query (and optional max price). Returns up to 3 best matches.',
    {
      query: z.string().describe('Item or food name, e.g. "burger", "pizza margherita"'),
      max_price: z
        .number()
        .optional()
        .describe('Max price in EUR (excludes higher-priced matches)'),
    },
    async (params) => {
      try {
        const items = await client.searchItems(params.query, params.max_price);
        return {
          content: [
            { type: 'text' as const, text: JSON.stringify(items, null, 2) },
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
}
