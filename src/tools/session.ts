// src/tools/session.ts
//
// Session management tools: persist/clear the JWT, surface its status (claims
// + expiry). Order placement tools depend on a valid token being saved here.

import { z } from 'zod';
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { WoltClient } from '../wolt/client.js';
import {
  readSessionToken,
  writeSessionToken,
  clearSessionToken,
} from '../storage/session-token.js';

export function registerSessionTools(
  server: McpServer,
  client: WoltClient,
): void {
  server.tool(
    'set_session',
    'Save a Wolt JWT session token to disk (~/.wolt-mcp/session.json) so it survives MCP restarts. Required before any auth-only operation (addresses, ordering).',
    {
      token: z
        .string()
        .describe(
          'Wolt JWT token (the long string starting with "eyJ..."). Get from Wolt web/app session.',
        ),
    },
    async (params) => {
      await writeSessionToken(params.token);
      client.setToken(params.token);
      const status = await client.getSessionStatus();
      return {
        content: [
          {
            type: 'text' as const,
            text: JSON.stringify({ saved: true, status }, null, 2),
          },
        ],
      };
    },
  );

  server.tool(
    'get_session_status',
    'Inspect the currently saved session: presence, validity, user email, expiry. No external API call — decodes the JWT locally.',
    {},
    async () => {
      const stored = await readSessionToken();
      if (stored && !client.hasToken()) client.setToken(stored);
      const status = await client.getSessionStatus();
      return {
        content: [
          { type: 'text' as const, text: JSON.stringify(status, null, 2) },
        ],
      };
    },
  );

  server.tool(
    'clear_session',
    'Delete the saved Wolt session token. Use when the token is invalid or you want to switch accounts.',
    {},
    async () => {
      await clearSessionToken();
      client.setToken(undefined);
      return {
        content: [
          { type: 'text' as const, text: JSON.stringify({ cleared: true }) },
        ],
      };
    },
  );
}
