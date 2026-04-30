#!/usr/bin/env node
// src/index.ts
//
// wolt-mcp entry point. Wires the WoltClient + tool modules to an MCP server
// over stdio. Mirrors selver-mcp's structure for consistency.

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { WoltClient } from './wolt/client.js';
import { readSessionToken } from './storage/session-token.js';
import { registerSearchTools } from './tools/search.js';
import { registerSessionTools } from './tools/session.js';
import { registerAddressTools } from './tools/address.js';
import { registerOrderTools } from './tools/order.js';

const server = new McpServer({
  name: 'wolt-mcp',
  version: '0.1.0',
});

const client = new WoltClient();

// Hydrate the saved token (if any) at startup so search/address/order tools
// don't require a set_session call every restart.
const savedToken = await readSessionToken();
if (savedToken) client.setToken(savedToken);

registerSearchTools(server, client);
registerSessionTools(server, client);
registerAddressTools(server, client);
registerOrderTools(server, client);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error('wolt-mcp fatal:', err);
  process.exit(1);
});
