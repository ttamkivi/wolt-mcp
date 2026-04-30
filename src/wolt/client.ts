// src/wolt/client.ts
//
// Port of ostja-bot/wolt.py (Python httpx → native fetch). Adds clearer error
// surfaces, typed responses, and uses session token from local storage rather
// than a constructor argument.

import type {
  Venue,
  MenuItem,
  SearchItem,
  DeliveryAddress,
  CartItem,
  OrderResult,
} from './types.js';

const WOLT_BASE = 'https://restaurant-api.wolt.com';
// Authentication endpoint is reserved for future refresh-token support.
// const WOLT_AUTH = 'https://authentication.wolt.com';

const DEFAULT_HEADERS: Record<string, string> = {
  'User-Agent':
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
  Accept: 'application/json, text/plain, */*',
  'Accept-Language': 'et-EE,et;q=0.9,en;q=0.8',
  Referer: 'https://wolt.com/',
  Origin: 'https://wolt.com',
};

export interface WoltClientOpts {
  /** JWT session token (Authorization: Bearer ...). Optional for read-only ops. */
  token?: string;
  /** Default search/delivery latitude (Tallinn city centre by default). */
  lat?: number;
  /** Default search/delivery longitude. */
  lon?: number;
}

export class WoltClient {
  private token?: string;
  public lat: number;
  public lon: number;

  constructor(opts: WoltClientOpts = {}) {
    this.token = opts.token;
    // Tallinn city centre as a sensible default; override per-call if needed.
    this.lat = opts.lat ?? 59.4370;
    this.lon = opts.lon ?? 24.7536;
  }

  setToken(token: string | undefined): void {
    this.token = token;
  }

  hasToken(): boolean {
    return !!this.token;
  }

  private headers(): Record<string, string> {
    const h = { ...DEFAULT_HEADERS };
    if (this.token) h.Authorization = `Bearer ${this.token}`;
    return h;
  }

  // ── 1. Venue search ──────────────────────────────────────────────────────

  async searchVenues(query: string, lat?: number, lon?: number): Promise<Venue[]> {
    const params = new URLSearchParams({
      q: query,
      lat: String(lat ?? this.lat),
      lon: String(lon ?? this.lon),
      limit: '10',
    });
    const res = await fetch(`${WOLT_BASE}/v1/search?${params}`, {
      headers: this.headers(),
    });
    if (!res.ok) throw new Error(`Wolt search HTTP ${res.status}`);
    const raw = (await res.json()) as { results?: unknown[] };
    return this.parseVenues(raw).slice(0, 5);
  }

  private parseVenues(raw: { results?: unknown[] }): Venue[] {
    const out: Venue[] = [];
    for (const item of raw.results ?? []) {
      const i = item as Record<string, any>;
      const venue =
        i.value?.venue ??
        i.venue ??
        null;
      if (!venue) continue;

      let name: string = '';
      const rawName = venue.name;
      if (typeof rawName === 'string') name = rawName;
      else if (rawName && typeof rawName === 'object') {
        name = rawName.et || rawName.en || Object.values(rawName)[0] || '';
      }

      const slug: string = venue.slug || '';
      const oid: string = String(venue.id || venue._id?.$oid || '');
      const rating =
        typeof venue.rating === 'object'
          ? venue.rating?.score ?? 0
          : venue.rating ?? 0;

      out.push({
        platform: 'wolt',
        name,
        slug,
        oid,
        rating,
        delivery_time: venue.estimate ?? 30,
        delivery_price: (venue.delivery_price_int ?? 0) / 100,
        url: `https://wolt.com/et/est/tallinn/restaurant/${slug}`,
      });
    }
    return out;
  }

  // ── 2. Menu by venue oid ─────────────────────────────────────────────────

  async getMenu(oid: string): Promise<MenuItem[]> {
    if (!oid) return [];
    const res = await fetch(`${WOLT_BASE}/v3/menus/${oid}`, {
      headers: this.headers(),
    });
    if (!res.ok) throw new Error(`Wolt menu HTTP ${res.status}`);
    const raw = (await res.json()) as { results?: unknown[] };
    return this.parseMenu(raw);
  }

  private parseMenu(raw: { results?: unknown[] }): MenuItem[] {
    const items: MenuItem[] = [];
    for (const result of raw.results ?? []) {
      const r = result as Record<string, any>;
      for (const category of r.categories ?? []) {
        const catName =
          typeof category.name === 'object'
            ? category.name?.et || category.name?.en || ''
            : category.name || '';
        for (const it of category.items ?? []) {
          let name: string = '';
          if (typeof it.name === 'string') name = it.name;
          else if (it.name && typeof it.name === 'object') {
            name = it.name.et || it.name.en || '';
          }
          let desc: string = '';
          if (typeof it.description === 'string') desc = it.description;
          else if (it.description && typeof it.description === 'object') {
            desc = it.description.et || it.description.en || '';
          }
          const price = (it.baseprice ?? it.base_price ?? 0) / 100;
          const imageArr = Array.isArray(it.image) ? it.image : [];
          const image: string = imageArr[0]?.url || '';
          items.push({
            item_id: String(it.id ?? ''),
            item_name: name,
            description: desc,
            price,
            image,
            category: catName,
          });
        }
      }
    }
    return items;
  }

  // ── 3. Combined item search: venue + menu filter ─────────────────────────

  async searchItems(query: string, maxPrice?: number): Promise<SearchItem[]> {
    const venues = await this.searchVenues(query);
    if (venues.length === 0) return [];

    const out: SearchItem[] = [];
    const q = query.toLowerCase();
    const words = q.split(/\s+/).filter(Boolean);

    // Check up to 3 venues' menus
    for (const v of venues.slice(0, 3)) {
      if (!v.oid) {
        out.push({
          platform: 'wolt',
          item_name: v.name,
          venue_name: v.name,
          venue_slug: v.slug,
          item_id: '',
          price: 0,
          delivery_time: v.delivery_time,
          url: v.url,
        });
        continue;
      }

      let menu: MenuItem[] = [];
      try {
        menu = await this.getMenu(v.oid);
      } catch {
        // Skip silently — menu fetch can fail for closed venues
      }

      for (const item of menu) {
        const haystack = `${item.item_name} ${item.description}`.toLowerCase();
        const matches = words.some((w) => haystack.includes(w));
        if (!matches) continue;
        if (maxPrice !== undefined && item.price > maxPrice) continue;
        out.push({
          platform: 'wolt',
          item_name: item.item_name,
          venue_name: v.name,
          venue_slug: v.slug,
          item_id: item.item_id,
          price: item.price,
          delivery_time: v.delivery_time,
          delivery_price: v.delivery_price,
          url: v.url,
        });
        if (out.length >= 5) break;
      }
      if (out.length >= 5) break;
    }

    // Fallback to venue-only if no items matched
    if (out.length === 0) {
      for (const v of venues.slice(0, 3)) {
        out.push({
          platform: 'wolt',
          item_name: v.name,
          venue_name: v.name,
          venue_slug: v.slug,
          item_id: '',
          price: 0,
          delivery_time: v.delivery_time,
          url: v.url,
        });
      }
    }

    return out.slice(0, 3);
  }

  // ── 4. Delivery addresses (auth required) ────────────────────────────────

  async listDeliveryAddresses(): Promise<DeliveryAddress[]> {
    if (!this.token) throw new Error('No Wolt session token. Use set_session.');
    const res = await fetch(`${WOLT_BASE}/v1/users/me/addresses`, {
      headers: this.headers(),
    });
    if (!res.ok) throw new Error(`Addresses HTTP ${res.status}`);
    const raw = (await res.json()) as Array<Record<string, any>>;
    return raw.map((a) => ({
      id: a.id ? String(a.id) : undefined,
      formatted_address: a.formatted_address ?? '',
      lat: a.location?.coordinates?.lat ?? a.lat,
      lon: a.location?.coordinates?.lon ?? a.lon,
    }));
  }

  async getDeliveryAddress(): Promise<DeliveryAddress | null> {
    const list = await this.listDeliveryAddresses();
    return list[0] ?? null;
  }

  // ── 5. Order placement (auth required, irreversible — guard tightly) ─────

  async placeOrder(
    venueSlug: string,
    items: CartItem[],
    address: { lat: number; lon: number },
  ): Promise<OrderResult> {
    if (!this.token) {
      return {
        success: false,
        message:
          'No Wolt session token. Save it via set_session, ' +
          `or order manually: https://wolt.com/et/est/tallinn/restaurant/${venueSlug}`,
      };
    }

    // Step 1: create cart
    const cartRes = await fetch(`${WOLT_BASE}/v1/order_xp/cart`, {
      method: 'POST',
      headers: { ...this.headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({
        venue_slug: venueSlug,
        items: items.map((i) => ({
          item_id: i.item_id,
          count: i.count,
          modifiers: i.modifiers ?? [],
        })),
      }),
    });
    if (!cartRes.ok) {
      return {
        success: false,
        message: `Cart create failed (HTTP ${cartRes.status})`,
      };
    }
    const cart = (await cartRes.json()) as Record<string, any>;
    const cartId: string | undefined = cart.cart_id || cart.id;
    if (!cartId) {
      return { success: false, message: 'Cart ID missing in response' };
    }

    // Step 2: place order
    const orderRes = await fetch(`${WOLT_BASE}/v1/order_xp/order`, {
      method: 'POST',
      headers: { ...this.headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cart_id: cartId,
        delivery: {
          location: {
            coordinates: { lat: address.lat, lon: address.lon },
          },
        },
      }),
    });
    if (!orderRes.ok) {
      const text = await orderRes.text().catch(() => '');
      return {
        success: false,
        message: `Order failed (HTTP ${orderRes.status}). ${text.slice(0, 200)}`,
      };
    }
    const order = (await orderRes.json()) as Record<string, any>;
    const oid: string = order.order_id || order.id || 'unknown';
    return {
      success: true,
      order_id: oid,
      message: `Wolt order placed. ID: ${oid}`,
    };
  }

  // ── 6. Session check ─────────────────────────────────────────────────────

  async getSessionStatus(): Promise<{
    has_token: boolean;
    valid?: boolean;
    user_email?: string;
    user_id?: string;
    expires_at?: string;
  }> {
    if (!this.token) return { has_token: false };

    // Decode JWT payload (no verification — just for surfacing claims)
    try {
      const parts = this.token.split('.');
      if (parts.length !== 3) return { has_token: true, valid: false };
      const payload = JSON.parse(
        Buffer.from(parts[1], 'base64url').toString('utf8'),
      );
      const exp: number | undefined = payload.exp;
      const now = Math.floor(Date.now() / 1000);
      const valid = exp ? exp > now : undefined;
      return {
        has_token: true,
        valid,
        user_email: payload.user?.email,
        user_id: payload.user?.id,
        expires_at: exp ? new Date(exp * 1000).toISOString() : undefined,
      };
    } catch {
      return { has_token: true, valid: false };
    }
  }
}
