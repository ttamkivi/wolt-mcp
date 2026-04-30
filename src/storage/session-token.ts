// src/storage/session-token.ts
//
// Session token storage at ~/.wolt-mcp/session.json. Mirrors the selver-mcp
// cart-token pattern so the MCP can survive restarts without re-auth.

import fs from 'fs/promises';
import path from 'path';
import os from 'os';

const DEFAULT_DATA_DIR = path.join(os.homedir(), '.wolt-mcp');

interface StoredSession {
  token: string;
  saved_at: string;
}

function sessionPath(dataDir: string): string {
  return path.join(dataDir, 'session.json');
}

export async function readSessionToken(
  dataDir = DEFAULT_DATA_DIR,
): Promise<string | null> {
  try {
    const raw = await fs.readFile(sessionPath(dataDir), 'utf-8');
    const data: StoredSession = JSON.parse(raw);
    return data.token;
  } catch {
    return null;
  }
}

export async function writeSessionToken(
  token: string,
  dataDir = DEFAULT_DATA_DIR,
): Promise<void> {
  await fs.mkdir(dataDir, { recursive: true });
  const data: StoredSession = {
    token,
    saved_at: new Date().toISOString(),
  };
  await fs.writeFile(sessionPath(dataDir), JSON.stringify(data, null, 2));
  // Best-effort permissions: keep token readable only by current user.
  try {
    await fs.chmod(sessionPath(dataDir), 0o600);
  } catch {
    // Ignore on filesystems that don't support chmod
  }
}

export async function clearSessionToken(
  dataDir = DEFAULT_DATA_DIR,
): Promise<void> {
  try {
    await fs.unlink(sessionPath(dataDir));
  } catch {
    // No-op if file doesn't exist
  }
}
