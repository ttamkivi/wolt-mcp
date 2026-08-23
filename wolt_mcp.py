"""
wolt-mcp — MCP server for Wolt food delivery (v0.5).

The "AI does 99%, human taps Apple Pay" model:
  - AI searches venues, picks items, prepares cart on Wolt's servers
  - User opens the Wolt checkout link → reviews → taps Apple Pay (Touch ID)
  - Order goes through. AI never autonomously authorizes payment.

Tools by purpose:
  - SEARCH:    search_venues, get_venue, get_venue_menu, find_items
  - LOCAL CART: add_to_cart, view_cart, remove_from_cart, clear_cart
  - DEEPLINK:  get_deeplink
  - AUTH:      set_session, get_session_status
  - HISTORY:   get_my_orders, get_wolt_baskets
  - PAYMENT:   get_payment_methods, set_default_payment_method
  - DELIVERY:  set_delivery_address, get_delivery_address, list_delivery_addresses
  - SYNC:      sync_basket_to_wolt          (NEW v0.5 — push local cart to Wolt)
               prepare_wolt_checkout         (NEW v0.5 — POST checkout, get checkout_id)
               get_checkout_link             (NEW v0.5 — Wolt checkout URL with prefilled cart)

REMOVED in v0.5: place_pickup_order, place_delivery_order, get_pickup_slots,
get_delivery_slots — replaced by the safer sync+deeplink flow above.

Defaults to Tallinn (lat=59.4370, lon=24.7536) when no coords given.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WOLT_RESTAURANT_API = "https://restaurant-api.wolt.com"
WOLT_CONSUMER_API = "https://consumer-api.wolt.com"
WOLT_WEB_BASE = "https://wolt.com"

DEFAULT_LAT = 59.4370   # Tallinn, Vabaduse väljak
DEFAULT_LON = 24.7536
DEFAULT_COUNTRY = "est"
DEFAULT_CITY = "tallinn"
DEFAULT_LANG = "en"

CART_DIR = Path.home() / ".wolt-mcp"
CART_FILE = CART_DIR / "cart.json"
SESSION_FILE = CART_DIR / "session.json"
CONFIG_FILE = CART_DIR / "config.json"
DEVICE_FILE = CART_DIR / "device.json"
AUDIT_FILE = CART_DIR / "audit.jsonl"

# Rate limiter: max N concurrent requests + min delay between them
RATE_LIMIT_MAX_CONCURRENT = 4
RATE_LIMIT_MIN_DELAY_MS = 100

# Default delivery addresses — pre-seeded for Taavi @ Salv
DEFAULT_ADDRESSES = {
    "salv-office": {
        "label": "salv-office",
        "name": "Salv kontor",
        "address": "Veerenni 38, Tallinn",
        "lat": 59.4279,
        "lon": 24.7488,
        "floor": "1",
        "instructions": "Esimene korrus, laud paremat kätt kuhu jätta",
    }
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/147.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8,et;q=0.7",
    "app-language": "en",
    "app-locale": "en",
    "platform": "Web",
    "client-version": "1.16.99",
    "clientversionnumber": "1.16.99",
    "Origin": "https://wolt.com",
    "Referer": "https://wolt.com/",
}

REQUEST_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# Audit log + rate limiter
# ---------------------------------------------------------------------------


def _audit(action: str, **fields) -> None:
    """Append an audit event to ~/.wolt-mcp/audit.jsonl. Best-effort, never raises."""
    try:
        CART_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": dt.datetime.utcnow().isoformat() + "Z",
            "action": action,
            **fields,
        }
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


_RATE_SEMAPHORE: Optional[Any] = None
_LAST_REQUEST_TS: float = 0.0


async def _rate_limit_acquire() -> None:
    """Throttle requests: at most RATE_LIMIT_MAX_CONCURRENT in flight,
    and at least RATE_LIMIT_MIN_DELAY_MS between consecutive starts."""
    global _RATE_SEMAPHORE, _LAST_REQUEST_TS
    if _RATE_SEMAPHORE is None:
        _RATE_SEMAPHORE = asyncio.Semaphore(RATE_LIMIT_MAX_CONCURRENT)
    await _RATE_SEMAPHORE.acquire()
    import time
    now = time.monotonic()
    delta = now - _LAST_REQUEST_TS
    min_delay = RATE_LIMIT_MIN_DELAY_MS / 1000.0
    if delta < min_delay:
        await asyncio.sleep(min_delay - delta)
    _LAST_REQUEST_TS = time.monotonic()


def _rate_limit_release() -> None:
    if _RATE_SEMAPHORE is not None:
        _RATE_SEMAPHORE.release()

# ---------------------------------------------------------------------------
# Session (Wolt JWT or cookie)
# ---------------------------------------------------------------------------


@dataclass
class Session:
    token: str = ""
    kind: str = "bearer"           # "bearer" or "cookie"
    saved_at: Optional[str] = None
    last_renewed_at: Optional[str] = None  # cookie mode: last Set-Cookie rotation captured

    def is_set(self) -> bool:
        return bool(self.token)

    def auth_headers(self) -> dict:
        if not self.token:
            return {}
        if self.kind == "cookie":
            return {"Cookie": self.token}
        # default: bearer
        tok = self.token
        if not tok.lower().startswith("bearer "):
            tok = f"Bearer {tok}"
        return {"Authorization": tok}


def _load_session() -> Session:
    if not SESSION_FILE.exists():
        return Session()
    try:
        raw = json.loads(SESSION_FILE.read_text())
        return Session(
            token=raw.get("token", ""),
            kind=raw.get("kind", "bearer"),
            saved_at=raw.get("saved_at"),
            last_renewed_at=raw.get("last_renewed_at"),
        )
    except Exception:
        return Session()


def _merge_cookie_updates(cookie_str: str, updates: dict[str, str]) -> str:
    """Merge freshly-rotated name=value pairs (from a Set-Cookie response)
    into an existing 'Cookie:' header string. Existing names are replaced
    in place; unrelated cookies are left untouched and keep their order."""
    if not updates:
        return cookie_str
    parts: dict[str, str] = {}
    order: list[str] = []
    for p in cookie_str.split(";"):
        p = p.strip()
        if not p:
            continue
        name, _, value = p.partition("=")
        name = name.strip()
        if name not in parts:
            order.append(name)
        parts[name] = value
    for name, value in updates.items():
        if name not in parts:
            order.append(name)
        parts[name] = value
    return "; ".join(f"{name}={parts[name]}" for name in order)


def _save_session(s: Session) -> None:
    CART_DIR.mkdir(parents=True, exist_ok=True)
    s.saved_at = dt.datetime.utcnow().isoformat() + "Z"
    SESSION_FILE.write_text(json.dumps(asdict(s), indent=2))
    try:
        os.chmod(SESSION_FILE, 0o600)  # restrict to owner
    except Exception:
        pass


def _decode_jwt_exp(token: str) -> Optional[int]:
    """Decode a JWT and return the exp claim (Unix seconds), or None.
    Does NOT verify signature — just reads the claim."""
    if not token or token.count(".") != 2:
        return None
    try:
        import base64
        payload_b64 = token.split(".")[1]
        # JWT base64url uses no padding — add it
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def _token_ttl_seconds(s: Session) -> Optional[int]:
    """Seconds until the bearer JWT expires. None if not a JWT."""
    if not s.token or s.kind != "bearer":
        return None
    exp = _decode_jwt_exp(s.token)
    if exp is None:
        return None
    # dt.datetime.utcnow().timestamp() is a classic trap: .timestamp() treats
    # a naive datetime as LOCAL time, so on any non-UTC machine (e.g. Estonia,
    # UTC+3) this silently shifted every expiry check by the local offset —
    # tokens looked hours healthier than they actually were.
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    return exp - now


def _check_token_health() -> Optional[str]:
    """Return a warning string if token is near expiry / expired, else None."""
    s = _load_session()
    if not s.is_set():
        return None
    ttl = _token_ttl_seconds(s)
    if ttl is None:
        return None
    if ttl <= 0:
        return (
            f"⚠️ Wolt access token EXPIRED {-ttl}s ago (legacy bearer mode, "
            "doesn't self-renew). Either refresh it: Chrome → wolt.com → "
            "DevTools → Network → consumer-api request → 'Copy as fetch' → "
            "extract Bearer JWT → set_session(token=...) — or switch to "
            "kind=\"cookie\" (paste the full cookie: header instead) so it "
            "renews itself from then on. See README."
        )
    if ttl < 120:
        return (
            f"⚠️ Wolt access token expires in {ttl}s (legacy bearer mode) — "
            "refresh via DevTools 'Copy as fetch', or switch to "
            "kind=\"cookie\" so this stops happening (see README)."
        )
    return None


# ---------------------------------------------------------------------------
# Device identity (Wolt web client UUIDs — sent on every request)
# ---------------------------------------------------------------------------


@dataclass
class Device:
    wolt_client_id: str = ""    # x-wolt-web-clientid (persistent UUID)
    wolt_session_id: str = ""   # w-wolt-session-id (per-session UUID)
    created_at: Optional[str] = None

    def headers(self) -> dict:
        h = {}
        if self.wolt_client_id:
            h["x-wolt-web-clientid"] = self.wolt_client_id
        if self.wolt_session_id:
            h["w-wolt-session-id"] = self.wolt_session_id
        return h


def _parse_cookie_value(cookie_str: str, name: str) -> Optional[str]:
    """Extract a single cookie value by name from a Cookie header string."""
    if not cookie_str:
        return None
    for p in cookie_str.split(";"):
        p = p.strip()
        if not p:
            continue
        n, _, v = p.partition("=")
        if n.strip() == name:
            return v
    return None


def _load_device() -> Device:
    """Load device UUIDs.

    Priority:
      1. If session.json holds a Cookie (kind="cookie"), pull
         telemetryDeviceId + telemetrySessionId from it — these MUST match
         what Wolt's server expects (it cross-checks header UUIDs against
         the same UUIDs in session cookies).
      2. Else if device.json exists, load saved UUIDs.
      3. Else generate fresh UUIDs and persist.
    """
    import uuid
    # 1. Try to extract from cookie session
    sess = _load_session()
    if sess.is_set() and sess.kind == "cookie":
        client_id = _parse_cookie_value(sess.token, "telemetryDeviceId")
        session_id = _parse_cookie_value(sess.token, "telemetrySessionId")
        if client_id and session_id:
            return Device(
                wolt_client_id=client_id,
                wolt_session_id=session_id,
                created_at="from-cookie",
            )

    # 2. device.json
    if DEVICE_FILE.exists():
        try:
            raw = json.loads(DEVICE_FILE.read_text())
            d = Device(
                wolt_client_id=raw.get("wolt_client_id", ""),
                wolt_session_id=raw.get("wolt_session_id", ""),
                created_at=raw.get("created_at"),
            )
            if d.wolt_client_id and d.wolt_session_id:
                return d
        except Exception:
            pass

    # 3. Generate fresh
    d = Device(
        wolt_client_id=str(uuid.uuid4()),
        wolt_session_id=str(uuid.uuid4()),
        created_at=dt.datetime.utcnow().isoformat() + "Z",
    )
    CART_DIR.mkdir(parents=True, exist_ok=True)
    DEVICE_FILE.write_text(json.dumps(asdict(d), indent=2))
    return d


# ---------------------------------------------------------------------------
# Config (delivery addresses + defaults)
# ---------------------------------------------------------------------------


@dataclass
class Config:
    addresses: dict = field(default_factory=lambda: dict(DEFAULT_ADDRESSES))
    default_address_label: str = "salv-office"
    default_payment_method_id: Optional[str] = None


def _load_config() -> Config:
    if not CONFIG_FILE.exists():
        return Config()
    try:
        raw = json.loads(CONFIG_FILE.read_text())
        return Config(
            addresses=raw.get("addresses") or dict(DEFAULT_ADDRESSES),
            default_address_label=raw.get("default_address_label", "salv-office"),
            default_payment_method_id=raw.get("default_payment_method_id"),
        )
    except Exception:
        return Config()


def _save_config(c: Config) -> None:
    CART_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(asdict(c), indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Cart state (local JSON file)
# ---------------------------------------------------------------------------


@dataclass
class CartLine:
    item_id: str
    name: str
    qty: int
    unit_price: float
    options: list[dict] = field(default_factory=list)
    notes: Optional[str] = None

    @property
    def line_total(self) -> float:
        opt_delta = sum(o.get("price_delta", 0.0) for o in self.options)
        return round((self.unit_price + opt_delta) * self.qty, 2)


@dataclass
class Cart:
    venue_slug: Optional[str] = None
    venue_name: Optional[str] = None
    venue_id: Optional[str] = None
    currency: Optional[str] = None
    country: str = DEFAULT_COUNTRY
    city: str = DEFAULT_CITY
    lines: list[CartLine] = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return round(sum(l.line_total for l in self.lines), 2)

    def to_dict(self) -> dict:
        return {
            "venue_slug": self.venue_slug,
            "venue_name": self.venue_name,
            "venue_id": self.venue_id,
            "currency": self.currency,
            "country": self.country,
            "city": self.city,
            "lines": [asdict(l) for l in self.lines],
            "subtotal": self.subtotal,
            "deeplink": _venue_deeplink(self.venue_slug, self.country, self.city)
                        if self.venue_slug else None,
        }


def _load_cart() -> Cart:
    if not CART_FILE.exists():
        return Cart()
    try:
        raw = json.loads(CART_FILE.read_text())
        cart = Cart(
            venue_slug=raw.get("venue_slug"),
            venue_name=raw.get("venue_name"),
            venue_id=raw.get("venue_id"),
            currency=raw.get("currency"),
            country=raw.get("country", DEFAULT_COUNTRY),
            city=raw.get("city", DEFAULT_CITY),
        )
        for l in raw.get("lines", []):
            cart.lines.append(CartLine(
                item_id=l["item_id"],
                name=l["name"],
                qty=l["qty"],
                unit_price=l["unit_price"],
                options=l.get("options", []),
                notes=l.get("notes"),
            ))
        return cart
    except Exception:
        return Cart()


def _save_cart(cart: Cart) -> None:
    CART_DIR.mkdir(parents=True, exist_ok=True)
    CART_FILE.write_text(json.dumps(cart.to_dict(), indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Wolt API client
# ---------------------------------------------------------------------------


class WoltClient:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _maybe_persist_rotated_cookies(session: "Session", resp: "httpx.Response") -> None:
        """Cookie-mode self-renewal: Wolt rotates __wtoken (and occasionally
        __wrtoken) via ordinary Set-Cookie response headers, the same way it
        keeps a browser tab logged in without any dedicated 'refresh' call.
        Capture that rotation here and persist it, so a cookie session pasted
        once keeps renewing itself for as long as calls keep succeeding.
        """
        if session.kind != "cookie" or not resp.cookies:
            return
        rotated = dict(resp.cookies.items())
        if not rotated:
            return
        merged = _merge_cookie_updates(session.token, rotated)
        if merged != session.token:
            session.token = merged
            session.last_renewed_at = dt.datetime.utcnow().isoformat() + "Z"
            _save_session(session)
            _audit("session_cookie_rotated", cookies=list(rotated.keys()))

    async def get(self, url: str, *, auth: bool = False, **kwargs) -> dict:
        client = await self._get_client()
        headers = kwargs.pop("headers", {})
        # Always send device identity (Wolt web client UUIDs)
        headers = {**_load_device().headers(), **headers}
        if auth:
            session = _load_session()
            if not session.is_set():
                raise RuntimeError(
                    "Wolt sessiooni tokenit pole salvestatud. Kasuta esmalt set_session(token). "
                    "Vaata README-d, kuidas DevTools'ist tokenit eraldada."
                )
            ttl = _token_ttl_seconds(session)
            if ttl is not None and ttl <= 0:
                raise RuntimeError(
                    f"Wolt token AEGUNUD {-ttl}s tagasi (legacy bearer-režiim, "
                    "ei värskenda ennast). Värskenda käsitsi: Chrome → wolt.com → "
                    "DevTools → Network → consumer-api → 'Copy as fetch' → "
                    "eralda Bearer JWT → set_session(token=...) — VÕI lülitu "
                    "kind=\"cookie\" peale (kleebi terve cookie: header), et "
                    "sessioon hakkaks ennast automaatselt värskendama."
                )
            headers = {**headers, **session.auth_headers()}
        await _rate_limit_acquire()
        try:
            resp = await client.get(url, headers=headers, **kwargs)
            resp.raise_for_status()
            _audit("api_get", url=url, status=resp.status_code, auth=auth)
            if auth:
                self._maybe_persist_rotated_cookies(session, resp)
            return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as e:
            _audit("api_get_error", url=url, status=e.response.status_code, auth=auth)
            raise RuntimeError(
                f"Wolt API GET {url} → {e.response.status_code}: "
                f"{e.response.text[:300]}"
            )
        except httpx.HTTPError as e:
            _audit("api_get_error", url=url, error=str(e), auth=auth)
            raise RuntimeError(f"Wolt API GET {url} failed: {e}")
        finally:
            _rate_limit_release()

    async def post(self, url: str, *, auth: bool = True, json_body: Optional[dict] = None,
                   **kwargs) -> dict:
        client = await self._get_client()
        headers = kwargs.pop("headers", {})
        headers = {**_load_device().headers(), **headers}
        if auth:
            session = _load_session()
            if not session.is_set():
                raise RuntimeError(
                    "Wolt sessiooni tokenit pole salvestatud. Kasuta esmalt set_session(token)."
                )
            ttl = _token_ttl_seconds(session)
            if ttl is not None and ttl <= 0:
                raise RuntimeError(
                    f"Wolt token AEGUNUD {-ttl}s tagasi (legacy bearer-režiim). "
                    "Värskenda: Chrome → DevTools → Copy as fetch → eralda "
                    "Bearer JWT → set_session(token=...) — VÕI lülitu "
                    "kind=\"cookie\" peale, et see enam ei korduks."
                )
            headers = {**headers, **session.auth_headers()}
        await _rate_limit_acquire()
        try:
            resp = await client.post(url, headers=headers, json=json_body, **kwargs)
            resp.raise_for_status()
            _audit("api_post", url=url, status=resp.status_code, auth=auth,
                   payload_size=len(json.dumps(json_body)) if json_body else 0)
            if auth:
                self._maybe_persist_rotated_cookies(session, resp)
            return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as e:
            _audit("api_post_error", url=url, status=e.response.status_code, auth=auth)
            raise RuntimeError(
                f"Wolt API POST {url} → {e.response.status_code}: "
                f"{e.response.text[:300]}"
            )
        except httpx.HTTPError as e:
            _audit("api_post_error", url=url, error=str(e), auth=auth)
            raise RuntimeError(f"Wolt API POST {url} failed: {e}")
        finally:
            _rate_limit_release()

    # ---- Anonymous endpoints --------------------------------------------

    async def list_venues(self, lat: float, lon: float) -> list[dict]:
        url = f"{WOLT_RESTAURANT_API}/v1/pages/restaurants"
        data = await self.get(url, params={"lat": lat, "lon": lon})
        venues: list[dict] = []
        for section in data.get("sections", []):
            for item in section.get("items", []):
                venue = item.get("venue") or item.get("link", {}).get("venue")
                if venue:
                    venues.append(_normalize_venue_summary(venue, item))
        seen: set[str] = set()
        unique: list[dict] = []
        for v in venues:
            if v["slug"] not in seen:
                unique.append(v)
                seen.add(v["slug"])
        return unique

    async def get_venue(self, slug: str) -> dict:
        """Modern endpoint: /order-xp/web/v1/pages/venue/slug/{slug}/static.
        Falls back to legacy v3 if needed (mostly returns 410 Gone now)."""
        url = f"{WOLT_CONSUMER_API}/order-xp/web/v1/pages/venue/slug/{slug}/static"
        try:
            data = await self.get(url)
            venue = data.get("venue") or data.get("venue_raw")
            if venue:
                return _normalize_venue_static(venue, slug)
        except RuntimeError:
            pass
        # Legacy fallback
        url = f"{WOLT_RESTAURANT_API}/v3/venues/slug/{slug}"
        data = await self.get(url)
        results = data.get("results") or []
        if not results:
            raise RuntimeError(f"Venue '{slug}' not found")
        return _normalize_venue_full(results[0])

    async def get_menu(self, slug: str) -> dict:
        """Try the modern assortment endpoint first, fall back to v4 menu."""
        # Modern (consumer-assortment) — what Wolt web app currently uses
        url = f"{WOLT_CONSUMER_API}/consumer-api/consumer-assortment/v1/venues/slug/{slug}/assortment"
        try:
            data = await self.get(url)
            if data.get("items") or data.get("categories"):
                return _normalize_assortment(data, slug)
        except RuntimeError:
            pass
        # Fallback: legacy v4 menu (still works for some venues)
        url = f"{WOLT_RESTAURANT_API}/v4/venues/slug/{slug}/menu"
        data = await self.get(url)
        return _normalize_menu(data)

    # ---- Authenticated endpoints (best-effort URL guesses) --------------
    # Note: these endpoints are reverse-engineered from Wolt's web app.
    # If they break, capture a real request via Chrome DevTools and adjust.

    async def my_orders(self, limit: int = 20) -> list[dict]:
        """Confirmed working endpoint (verified via Wolt web DevTools 2026-04)."""
        url = f"{WOLT_CONSUMER_API}/order-xp/web/v1/pages/orders"
        data = await self.get(url, auth=True, params={"limit": limit})
        # order-xp returns a 'sections' / 'items' structure similar to discovery
        if isinstance(data, list):
            return data[:limit]
        # Try common shapes
        if "sections" in data:
            orders: list[dict] = []
            for section in data.get("sections", []):
                for item in section.get("items", []):
                    orders.append(item)
            return orders[:limit]
        return (data.get("results") or data.get("orders") or data.get("items") or [])[:limit]

    async def list_baskets(self, lat: float, lon: float) -> dict:
        """List Wolt-side baskets (carts) at this location. Read-only — does
        not modify Wolt state. Confirmed via DevTools."""
        url = f"{WOLT_CONSUMER_API}/order-xp/web/v1/pages/baskets"
        return await self.get(url, auth=True, params={"lat": lat, "lon": lon})

    async def my_payment_methods(self) -> list[dict]:
        url = f"{WOLT_CONSUMER_API}/v1/payment_methods"
        try:
            data = await self.get(url, auth=True)
        except RuntimeError:
            url = f"{WOLT_CONSUMER_API}/v3/payment_methods"
            data = await self.get(url, auth=True)
        if isinstance(data, list):
            return data
        return data.get("results") or data.get("payment_methods") or []

    # ---- v0.5: cart sync + checkout prep --------------------------------
    # Endpoints reverse-engineered from Wolt web checkout (HAR capture).

    async def create_basket(self, venue_id: str, items: list[dict],
                            currency: str = "EUR") -> dict:
        """POST a basket (cart) to Wolt's server side.
        items shape: [{id, count, name, price (cents), options: [...], substitution_settings}]
        Returns: {id: <basket_id>, venue_id}
        """
        url = f"{WOLT_CONSUMER_API}/order-xp/v1/baskets"
        payload = {"items": items, "venue_id": venue_id, "currency": currency}
        return await self.post(url, auth=True, json_body=payload)

    async def prepare_checkout(self, purchase_plan: dict) -> dict:
        """POST a purchase plan to /pages/checkout — returns checkout state
        with id (checkout_id), payable_amount, payment_breakdown, etc."""
        url = f"{WOLT_CONSUMER_API}/order-xp/web/v2/pages/checkout"
        return await self.post(url, auth=True, json_body={"purchase_plan": purchase_plan})

    async def search_global(self, query: str, lat: float, lon: float) -> dict:
        """POST /v1/pages/search — global search across venues + items."""
        url = f"{WOLT_RESTAURANT_API}/v1/pages/search"
        payload = {"q": query, "target": "items", "lat": lat, "lon": lon}
        return await self.post(url, auth=True, json_body=payload)

    async def wolt_plus_status(self) -> dict:
        """GET /subscriptions-api/v1/subscriptions — Wolt+ subscription info."""
        url = f"{WOLT_CONSUMER_API}/subscriptions-api/v1/subscriptions"
        return await self.get(url, auth=True)

    async def refresh_access_token(self) -> Optional[str]:
        """Best-effort refresh: tries known Wolt auth endpoints. Returns the
        new Bearer JWT on success, or None.

        ⚠️ Endpoint URL + payload are placeholders until a refresh request
        is captured in DevTools (currently unknown — needs a fresh HAR with
        a token rotation event).
        """
        # Candidate endpoints (best guess — needs HAR confirmation)
        for url in [
            f"{WOLT_CONSUMER_API}/woltauth/v1/refresh",
            f"{WOLT_CONSUMER_API}/woltauth/refresh",
            "https://authentication.wolt.com/v1/wauth/refresh",
        ]:
            try:
                # Wolt typically uses refresh token in cookie OR body
                data = await self.post(url, auth=True, json_body={})
                tok = data.get("access_token") or data.get("accessToken") or data.get("token")
                if tok and isinstance(tok, str) and tok.startswith("eyJ"):
                    return tok
            except RuntimeError:
                continue
        return None


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


def _normalize_venue_summary(venue: dict, container: Optional[dict] = None) -> dict:
    container = container or {}
    estimate = (container.get("venue") or venue).get("estimate")
    delivery_estimate = container.get("estimate") or estimate
    return {
        "id": venue.get("id"),
        "slug": venue.get("slug"),
        "name": venue.get("name"),
        "short_description": venue.get("short_description"),
        "address": venue.get("address"),
        "city": venue.get("city"),
        "country": venue.get("country"),
        "online": venue.get("online"),
        "delivers": venue.get("delivers"),
        "rating": (venue.get("rating") or {}).get("score"),
        "price_range": venue.get("price_range"),
        "estimate_min": delivery_estimate,
        "image": venue.get("image_url") or venue.get("image_blurhash"),
        "tags": venue.get("tags") or [],
        "currency": venue.get("currency"),
        "deeplink": _venue_deeplink(
            venue.get("slug"),
            (venue.get("country") or DEFAULT_COUNTRY).lower(),
            (venue.get("city") or DEFAULT_CITY).lower().replace(" ", "-"),
        ),
    }


def _normalize_venue_static(venue: dict, slug: str) -> dict:
    """Normalize the modern /pages/venue/slug/{slug}/static response shape."""
    rating = venue.get("rating")
    rating_score = rating.get("score") if isinstance(rating, dict) else rating
    return {
        "id": venue.get("id"),
        "slug": slug,
        "name": venue.get("name"),
        "brand_slug": venue.get("brand_slug"),
        "brand_name": venue.get("brand_name"),
        "short_description": venue.get("description"),
        "address": venue.get("address"),
        "city": venue.get("city"),
        "country": (venue.get("country") or "").lower(),
        "currency": venue.get("currency"),
        "online": True,  # static endpoint only returns extant venues
        "rating": rating_score,
        "delivery_methods": venue.get("delivery_methods") or [],
        "active_menu_id": venue.get("active_menu"),
        "share_url": venue.get("share_url"),
        "deeplink": venue.get("share_url") or _venue_deeplink(
            slug,
            (venue.get("country") or DEFAULT_COUNTRY).lower(),
            (venue.get("city") or DEFAULT_CITY).lower().replace(" ", "-"),
        ),
        "tags": venue.get("tags") or [],
    }


def _normalize_venue_full(venue: dict) -> dict:
    delivery = venue.get("delivery_specs", {}) or {}
    pricing = delivery.get("delivery_pricing", {}) or {}
    return {
        "id": venue.get("id"),
        "slug": venue.get("slug"),
        "name": venue.get("name"),
        "short_description": (venue.get("short_description") or [{}])[0].get("value")
            if venue.get("short_description") else None,
        "address": venue.get("address"),
        "city": venue.get("city"),
        "country": (venue.get("country") or "").lower(),
        "currency": venue.get("currency"),
        "online": venue.get("online"),
        "rating": (venue.get("rating") or {}).get("score"),
        "price_range": venue.get("price_range"),
        "min_basket": pricing.get("min_basket_price", 0) / 100 if pricing else None,
        "base_delivery_price": pricing.get("base_price", 0) / 100 if pricing else None,
        "tags": venue.get("tags") or [],
        "phone": venue.get("phone"),
        "deeplink": _venue_deeplink(
            venue.get("slug"),
            (venue.get("country") or DEFAULT_COUNTRY).lower(),
            (venue.get("city") or DEFAULT_CITY).lower().replace(" ", "-"),
        ),
    }


def _normalize_assortment(data: dict, slug: str) -> dict:
    """Convert modern consumer-assortment shape to our flat menu structure.

    Source shape: { categories: [{id,name,item_ids,...}], items: [...],
                    options: [{id,name,type,values}], variant_groups: [] }
    """
    currency = "EUR"  # assortment doesn't include currency at top — derive from item.price.currency if available
    cat_by_id = {c["id"]: c for c in data.get("categories", [])}
    options_by_id = {o["id"]: o for o in data.get("options", [])}

    items: list[dict] = []
    for it in data.get("items", []):
        # Skip disabled
        disabled = it.get("disabled_info")
        if disabled and disabled.get("disabled_reason"):
            continue
        # Price: assortment shows it as int cents in `price` (sometimes top-level number, sometimes dict with amount)
        price_field = it.get("price")
        price_cents = 0
        if isinstance(price_field, (int, float)):
            price_cents = int(price_field)
        elif isinstance(price_field, dict):
            price_cents = int(price_field.get("amount") or 0)
        item_currency = (
            (price_field.get("currency") if isinstance(price_field, dict) else None)
            or currency
        )
        # Find which category this item belongs to (reverse lookup via item_ids)
        cat_id = None
        cat_name = None
        for c in data.get("categories", []):
            if it["id"] in (c.get("item_ids") or []):
                cat_id = c["id"]
                cat_name = c.get("name")
                break

        # Item-specific option_ids → look up full option defs
        item_options = []
        for opt_ref in (it.get("options") or []):
            oid = opt_ref if isinstance(opt_ref, str) else opt_ref.get("id")
            full = options_by_id.get(oid)
            if not full:
                continue
            values = []
            for v in (full.get("values") or []):
                v_price = v.get("price")
                if isinstance(v_price, dict):
                    v_price = v_price.get("amount", 0)
                values.append({
                    "id": v.get("id"),
                    "name": v.get("name"),
                    "price_delta": (v_price or 0) / 100,
                    "default": v.get("id") == full.get("default_value"),
                })
            item_options.append({
                "id": full.get("id"),
                "name": full.get("name"),
                "type": "single" if full.get("type") in ("choice", "SingleSelection") else "multi",
                "min_count": (full.get("count") or {}).get("min", 0),
                "max_count": (full.get("count") or {}).get("max", 1),
                "values": values,
            })

        # Image url
        img = None
        imgs = it.get("images") or []
        if imgs and isinstance(imgs[0], dict):
            img = imgs[0].get("url")

        items.append({
            "id": it["id"],
            "name": it.get("name"),
            "description": it.get("description"),
            "price": price_cents / 100,
            "currency": item_currency,
            "image": img,
            "category_id": cat_id,
            "category_name": cat_name,
            "options": item_options,
        })

    categories = [
        {"id": c["id"], "name": c.get("name"), "description": c.get("description")}
        for c in data.get("categories", [])
    ]

    return {
        "currency": currency,
        "categories": categories,
        "items": items,
        "item_count": len(items),
        "_source": "assortment",
    }


def _normalize_menu(data: dict) -> dict:
    categories_raw = data.get("categories", [])
    items_raw = data.get("items", [])
    currency = data.get("currency") or "EUR"
    cat_by_id = {c["id"]: c for c in categories_raw}

    items: list[dict] = []
    for it in items_raw:
        if not it.get("enabled", True):
            continue
        category_id = it.get("category")
        items.append({
            "id": it.get("id"),
            "name": it.get("name"),
            "description": it.get("description"),
            "price": (it.get("baseprice") or it.get("price") or 0) / 100,
            "currency": currency,
            "image": it.get("image"),
            "category_id": category_id,
            "category_name": (cat_by_id.get(category_id) or {}).get("name"),
            "options": _normalize_options(it.get("options", [])),
        })

    categories = [
        {"id": c["id"], "name": c["name"], "description": c.get("description")}
        for c in categories_raw
    ]
    return {
        "currency": currency,
        "categories": categories,
        "items": items,
        "item_count": len(items),
    }


def _normalize_options(opts: list[dict]) -> list[dict]:
    out: list[dict] = []
    for o in opts:
        out.append({
            "id": o.get("id"),
            "name": o.get("name"),
            "type": "single" if (o.get("type") == "SingleSelection") else "multi",
            "min_count": o.get("count", {}).get("min", 0),
            "max_count": o.get("count", {}).get("max", 1),
            "values": [
                {
                    "id": v.get("id"),
                    "name": v.get("name"),
                    "price_delta": (v.get("price") or 0) / 100,
                    "default": v.get("default", False),
                }
                for v in (o.get("values") or [])
            ],
        })
    return out


# ---------------------------------------------------------------------------
# Deeplink + helpers
# ---------------------------------------------------------------------------


def _venue_deeplink(slug: Optional[str], country: str = DEFAULT_COUNTRY,
                    city: str = DEFAULT_CITY) -> Optional[str]:
    if not slug:
        return None
    return f"{WOLT_WEB_BASE}/{DEFAULT_LANG}/{country}/{city}/restaurant/{slug}"


def _confirm_token_for(payload: dict) -> str:
    """Deterministic token from order payload — agent must echo this back to
    the second call to actually submit. Prevents accidental double-submits and
    confirms the user has reviewed the dry-run output."""
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


# ---------------------------------------------------------------------------
# MCP server — tools
# ---------------------------------------------------------------------------

mcp = FastMCP("wolt-mcp")
_client = WoltClient()


# --- v0.1: anonymous tools --------------------------------------------------


@mcp.tool()
async def search_venues(
    query: str = "",
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    limit: int = 20,
    only_online: bool = True,
    max_estimate_min: Optional[int] = None,
) -> list[dict]:
    """Search Wolt venues near a location."""
    venues = await _client.list_venues(lat, lon)
    q = (query or "").strip().lower()

    def matches(v: dict) -> bool:
        if only_online and not v.get("online"):
            return False
        if max_estimate_min and v.get("estimate_min") and v["estimate_min"] > max_estimate_min:
            return False
        if not q:
            return True
        haystack = " ".join(filter(None, [
            v.get("name") or "",
            v.get("short_description") or "",
            " ".join(v.get("tags") or []),
        ])).lower()
        return q in haystack

    return [v for v in venues if matches(v)][:limit]


@mcp.tool()
async def get_venue(slug: str) -> dict:
    """Get detailed metadata for one venue."""
    return await _client.get_venue(slug)


@mcp.tool()
async def get_venue_menu(slug: str) -> dict:
    """Get the full menu for a venue."""
    return await _client.get_menu(slug)


@mcp.tool()
async def find_items(slug: str, query: str, limit: int = 10) -> list[dict]:
    """Search within a venue's menu."""
    menu = await _client.get_menu(slug)
    q = query.lower().strip()
    if not q:
        return menu["items"][:limit]
    return [
        it for it in menu["items"]
        if q in (it.get("name") or "").lower()
        or q in (it.get("description") or "").lower()
    ][:limit]


@mcp.tool()
async def add_to_cart(slug: str, items: list[dict]) -> dict:
    """Add items to the local virtual cart. See README for items shape."""
    cart = _load_cart()
    if cart.venue_slug and cart.venue_slug != slug:
        cart = Cart()

    venue = await _client.get_venue(slug)
    menu = await _client.get_menu(slug)
    items_by_id = {it["id"]: it for it in menu["items"]}

    cart.venue_slug = venue["slug"]
    cart.venue_name = venue["name"]
    cart.venue_id = venue["id"]
    cart.currency = venue.get("currency") or menu.get("currency")
    cart.country = (venue.get("country") or DEFAULT_COUNTRY).lower()
    cart.city = (venue.get("city") or DEFAULT_CITY).lower().replace(" ", "-")

    added: list[str] = []
    skipped: list[dict] = []
    for entry in items:
        item_id = entry.get("item_id")
        qty = int(entry.get("qty", 1))
        if not item_id or qty < 1:
            skipped.append({"reason": "missing item_id or qty<1", "entry": entry})
            continue
        menu_item = items_by_id.get(item_id)
        if not menu_item:
            skipped.append({"reason": "item not in venue menu", "entry": entry})
            continue
        cart.lines.append(CartLine(
            item_id=item_id,
            name=menu_item["name"],
            qty=qty,
            unit_price=menu_item["price"],
            options=entry.get("options", []),
            notes=entry.get("notes"),
        ))
        added.append(menu_item["name"])

    _save_cart(cart)
    return {
        "added": added, "skipped": skipped,
        "cart": cart.to_dict(),
        "instructions": _human_order_instructions(cart),
    }


@mcp.tool()
async def view_cart() -> dict:
    """View the current virtual cart with subtotal and deeplink."""
    cart = _load_cart()
    return {
        **cart.to_dict(),
        "instructions": _human_order_instructions(cart) if cart.lines else None,
    }


@mcp.tool()
async def remove_from_cart(item_ids: list[str]) -> dict:
    """Remove cart lines by item_id."""
    cart = _load_cart()
    before = len(cart.lines)
    cart.lines = [l for l in cart.lines if l.item_id not in item_ids]
    _save_cart(cart)
    return {"removed_count": before - len(cart.lines), "cart": cart.to_dict()}


@mcp.tool()
async def clear_cart() -> dict:
    """Empty the virtual cart entirely."""
    _save_cart(Cart())
    return {"cleared": True, "cart": Cart().to_dict()}


@mcp.tool()
async def get_deeplink(slug: Optional[str] = None) -> dict:
    """Get a Wolt web link for a venue."""
    if not slug:
        cart = _load_cart()
        if not cart.venue_slug:
            return {"error": "No cart and no slug provided"}
        return {
            "venue_slug": cart.venue_slug,
            "venue_name": cart.venue_name,
            "deeplink": _venue_deeplink(cart.venue_slug, cart.country, cart.city),
            "instructions": _human_order_instructions(cart),
        }
    venue = await _client.get_venue(slug)
    return {
        "venue_slug": venue["slug"],
        "venue_name": venue["name"],
        "deeplink": venue["deeplink"],
    }


# --- v0.2: authenticated tools ---------------------------------------------


@mcp.tool()
async def set_session(token: str, kind: str = "bearer") -> dict:
    """
    Save a Wolt session token. Required before any authenticated tool.

    RECOMMENDED: kind="cookie". Capture the full 'cookie:' request header
    (Chrome DevTools → Network tab → any /consumer-api/ request → Headers →
    Request Headers → 'cookie:' value) and pass the whole string as `token`.
    It includes Wolt's __wtoken (access) and __wrtoken (refresh) cookies.
    From then on, wolt-mcp behaves like a real logged-in browser tab: every
    authenticated call captures whatever Set-Cookie rotation Wolt sends back
    and persists it, so the session renews itself indefinitely — no more
    re-pasting every time it expires. Re-run this only if a call starts
    returning 401 (meaning __wrtoken itself finally expired).

    LEGACY: kind="bearer" — paste just the Authorization: Bearer JWT. This
    is short-lived (often ~1h) and does NOT self-renew; you'll need to
    repeat this call manually every time it expires.

    Args:
      token: full cookie string (kind="cookie") or bare JWT (kind="bearer").
      kind: "cookie" (recommended, self-renewing) or "bearer" (legacy).

    Token is stored in ~/.wolt-mcp/session.json (mode 600). To rotate
    manually, call this again.
    """
    if not token or not token.strip():
        return {"ok": False, "error": "token is empty"}
    s = Session(token=token.strip(), kind=kind)
    _save_session(s)
    return {
        "ok": True,
        "kind": kind,
        "preview": (token[:8] + "…" + token[-4:]) if len(token) > 16 else "(short)",
        "saved_at": s.saved_at,
    }


@mcp.tool()
async def get_session_status() -> dict:
    """Check whether a Wolt session token is saved, and how long it's still
    valid (for Bearer JWTs)."""
    s = _load_session()
    if not s.is_set():
        return {"ok": False, "message": "No session token saved. Use set_session()."}
    out = {
        "ok": True, "kind": s.kind, "saved_at": s.saved_at,
        "preview": (s.token[:8] + "…" + s.token[-4:]) if len(s.token) > 16 else "(short)",
    }
    if s.kind == "cookie":
        out["mode"] = "self-renewing"
        out["last_renewed_at"] = s.last_renewed_at
        out["note"] = (
            "Cookie-mode session — Wolt rotates the access cookie via "
            "ordinary Set-Cookie responses and wolt-mcp persists that "
            "rotation automatically. Only call set_session() again if a "
            "call starts returning 401."
        )
        return out
    ttl = _token_ttl_seconds(s)
    if ttl is not None:
        if ttl <= 0:
            out["status"] = "EXPIRED"
            out["expired_seconds_ago"] = -ttl
        elif ttl < 120:
            out["status"] = "EXPIRING_SOON"
            out["seconds_left"] = ttl
        else:
            out["status"] = "VALID"
            out["seconds_left"] = ttl
            out["minutes_left"] = round(ttl / 60, 1)
        # Decode user info from JWT if present
        try:
            import base64
            parts = s.token.split(".")
            if len(parts) == 3:
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                user = payload.get("user", {})
                if isinstance(user, dict):
                    name = user.get("name", {})
                    full_name = " ".join(filter(None, [name.get("first_name"), name.get("last_name")])) if isinstance(name, dict) else None
                    out["user"] = {
                        "name": full_name,
                        "email": user.get("email"),
                        "country": user.get("country"),
                    }
        except Exception:
            pass
    return out


@mcp.tool()
async def get_my_orders(limit: int = 20, days_back: Optional[int] = None) -> list[dict]:
    """
    Fetch your past Wolt orders. Use this to compute favorites — group by
    venue / item, count, find most-ordered, etc.

    Args:
      limit: max orders to return (default 20, Wolt may cap higher).
      days_back: optional — filter to orders within the last N days.

    Returns a list of orders. Shape varies by Wolt API version, but typically
    includes: id, venue_id, venue_name, items[], total_price, created_at.
    """
    orders = await _client.my_orders(limit=limit)
    if days_back:
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=days_back)
        def fresh(o: dict) -> bool:
            ts = o.get("created_at") or o.get("placed_time") or o.get("submitted_at")
            if not ts:
                return True
            try:
                t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
                return t >= cutoff
            except Exception:
                return True
        orders = [o for o in orders if fresh(o)]
    return orders


@mcp.tool()
async def get_wolt_baskets(
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
) -> dict:
    """
    List your live Wolt-side baskets (carts) — items you've added in the app
    or web that haven't been ordered yet. Read-only.

    Args:
      lat, lon: coordinates (Wolt filters baskets by delivery zone).
                Defaults to Tallinn city centre. For Salv office, pass
                lat=59.4279, lon=24.7488.

    Useful to: see what's already in your in-progress carts, resume an
    order you started in the app, or compute "items I tend to add" pre-order.
    """
    return await _client.list_baskets(lat, lon)


@mcp.tool()
async def get_payment_methods() -> list[dict]:
    """List your saved Wolt payment methods (cards). Required to build an
    order. Returns cards with id, last4, brand, default flag."""
    return await _client.my_payment_methods()


# (place_pickup_order removed in v0.5 — replaced by sync_basket_to_wolt + checkout link)
async def _UNUSED_place_pickup_order(
    slug: str,
    items: list[dict],
    payment_method_id: str,
    pickup_time_iso: str,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Place a pickup order at a Wolt venue. Two-step:

    Step 1: call with dry_run=True (default). Returns the planned order
            with total price + a confirm_token (12-char hash).
    Step 2: call again with dry_run=False AND confirm_token=<that token>
            to actually submit.

    If confirm_token doesn't match, submission is rejected.

    Args:
      slug: venue slug.
      items: [{ item_id, qty, options? }] — same shape as add_to_cart.
      payment_method_id: from get_payment_methods().
      pickup_time_iso: ISO 8601 timestamp from get_pickup_slots(),
                       e.g. "2026-04-27T13:30:00Z".
      dry_run: true to preview, false to submit (with valid confirm_token).
      confirm_token: token returned by the dry-run call.
      notes: optional note to the venue.

    ⚠️ Submitting an order will charge your saved card. Always review the
    dry-run output (especially total + pickup_time) before confirming.
    """
    venue = await _client.get_venue(slug)
    menu = await _client.get_menu(slug)
    items_by_id = {it["id"]: it for it in menu["items"]}

    # Build line items in Wolt's expected shape (best-effort guess)
    order_items: list[dict] = []
    subtotal = 0.0
    for entry in items:
        iid = entry.get("item_id")
        qty = int(entry.get("qty", 1))
        if iid not in items_by_id:
            return {"error": f"item_id {iid} not found in venue menu"}
        m = items_by_id[iid]
        opts = entry.get("options") or []
        opt_delta = sum(o.get("price_delta", 0.0) for o in opts)
        line_price_cents = int(round((m["price"] + opt_delta) * 100))
        subtotal += (m["price"] + opt_delta) * qty
        order_items.append({
            "id": iid,
            "count": qty,
            "options": [
                {"id": o.get("option_id"), "value": o.get("value_id")}
                for o in opts if o.get("option_id") and o.get("value_id")
            ],
            "baseprice": line_price_cents,
        })

    payload = {
        "venue_id": venue["id"],
        "items": order_items,
        "delivery_method": "takeaway",
        "preorder_time": pickup_time_iso,
        "payment_method": {"id": payment_method_id},
        "comment": notes or "",
        "currency": venue.get("currency", "EUR"),
    }

    expected_token = _confirm_token_for(payload)
    summary = {
        "venue_slug": slug,
        "venue_name": venue["name"],
        "venue_address": venue.get("address"),
        "currency": venue.get("currency", "EUR"),
        "items_summary": [
            f"{it['count']}× {items_by_id[it['id']]['name']} "
            f"({it['baseprice']/100:.2f})"
            for it in order_items
        ],
        "subtotal": round(subtotal, 2),
        "pickup_time": pickup_time_iso,
        "payment_method_id": payment_method_id,
    }

    if dry_run:
        return {
            "dry_run": True,
            "summary": summary,
            "confirm_token": expected_token,
            "instructions": (
                f"Review the summary. To submit: call place_pickup_order again "
                f"with dry_run=False and confirm_token=\"{expected_token}\""
            ),
            "_payload_preview": payload,
        }

    if confirm_token != expected_token:
        return {
            "error": "confirm_token mismatch — re-run with dry_run=True first",
            "expected_token_hint": expected_token[:4] + "…",
        }

    # Actually submit
    result = await _client.place_order(payload)
    return {
        "submitted": True,
        "summary": summary,
        "wolt_response": result,
    }


# --- v0.3: delivery address management + delivery orders ------------------


@mcp.tool()
async def set_delivery_address(
    label: str,
    address: str,
    lat: float,
    lon: float,
    floor: Optional[str] = None,
    instructions: Optional[str] = None,
    name: Optional[str] = None,
    set_default: bool = False,
) -> dict:
    """
    Save a named delivery address. Pre-seeded with 'salv-office'
    (Veerenni 38, Tallinn).

    Args:
      label: short id, e.g. "salv-office", "home". Used to reference
             this address in place_delivery_order.
      address: human-readable street address.
      lat, lon: coordinates (Wolt needs these for delivery zone check).
      floor: optional floor/door info.
      instructions: optional courier note (e.g. "leave at reception").
      name: friendly name (defaults to label).
      set_default: if True, this address becomes the default for orders.
    """
    cfg = _load_config()
    cfg.addresses[label] = {
        "label": label,
        "name": name or label,
        "address": address,
        "lat": lat,
        "lon": lon,
        "floor": floor,
        "instructions": instructions,
    }
    if set_default:
        cfg.default_address_label = label
    _save_config(cfg)
    return {"ok": True, "label": label, "default": cfg.default_address_label}


@mcp.tool()
async def get_delivery_address(label: Optional[str] = None) -> dict:
    """Get a saved delivery address. If label omitted, returns the default."""
    cfg = _load_config()
    use_label = label or cfg.default_address_label
    addr = cfg.addresses.get(use_label)
    if not addr:
        return {"error": f"No address with label '{use_label}'",
                "available": list(cfg.addresses.keys())}
    return {**addr, "is_default": use_label == cfg.default_address_label}


@mcp.tool()
async def list_delivery_addresses() -> dict:
    """List all saved delivery addresses."""
    cfg = _load_config()
    return {
        "default": cfg.default_address_label,
        "addresses": cfg.addresses,
        "default_payment_method_id": cfg.default_payment_method_id,
    }


@mcp.tool()
async def set_default_payment_method(payment_method_id: str) -> dict:
    """Save a default payment method id so you don't have to pass it every time."""
    cfg = _load_config()
    cfg.default_payment_method_id = payment_method_id
    _save_config(cfg)
    return {"ok": True, "default_payment_method_id": payment_method_id}


# --- v0.5: cart sync to Wolt + checkout link ------------------------------


def _build_basket_items(menu_items_by_id: dict, items: list[dict]) -> tuple[list[dict], float]:
    """Convert agent-friendly items list into Wolt basket payload shape.
    Returns (items_for_wolt, subtotal_eur).
    items input shape: [{ item_id, qty, options?: [{option_id, value_id, count?}] }]
    """
    out = []
    subtotal = 0.0
    for entry in items:
        iid = entry.get("item_id")
        qty = int(entry.get("qty", 1))
        m = menu_items_by_id.get(iid)
        if not m:
            raise RuntimeError(f"item_id {iid} not in menu")
        opts_input = entry.get("options") or []
        opts_payload = []
        opt_delta = 0.0
        for o in opts_input:
            oid = o.get("option_id")
            vid = o.get("value_id")
            ocount = o.get("count", 1)
            if not oid or not vid:
                continue
            # find price_delta from menu
            menu_opt = next((mo for mo in m.get("options", []) if mo["id"] == oid), None)
            value_price = 0
            if menu_opt:
                v = next((mv for mv in menu_opt.get("values", []) if mv["id"] == vid), None)
                if v:
                    value_price = int(round(v["price_delta"] * 100))
                    opt_delta += v["price_delta"] * ocount
            opts_payload.append({
                "id": oid,
                "values": [{"id": vid, "count": ocount, "price": value_price}],
            })
        # Item base price in cents
        item_price_cents = int(round(m["price"] * 100))
        line_total_cents = item_price_cents + int(round(opt_delta * 100))
        subtotal += (m["price"] + opt_delta) * qty
        out.append({
            "id": iid,
            "count": qty,
            "name": m["name"],
            "price": line_total_cents,
            "options": opts_payload,
            "substitution_settings": {"is_allowed": True},
        })
    return out, round(subtotal, 2)


@mcp.tool()
async def sync_basket_to_wolt(slug: str, items: list[dict]) -> dict:
    """
    Push items into a real Wolt-side basket (cart). After this, opening the
    venue page in the Wolt app/web will show these items pre-filled.

    Args:
      slug: venue slug.
      items: [{ item_id, qty, options?: [{option_id, value_id, count?}] }]
             Same shape as add_to_cart (local cart). Options are looked up
             from the menu — option_id + value_id required.

    Returns: { basket_id, venue_id, items_pushed, subtotal_eur, checkout_link }

    NOTE: This writes to your real Wolt account. To remove items, edit the
    basket in the Wolt app/web (or call this again with the corrected items —
    Wolt typically replaces basket contents on POST).
    """
    venue = await _client.get_venue(slug)
    menu = await _client.get_menu(slug)
    items_by_id = {it["id"]: it for it in menu["items"]}

    try:
        wolt_items, subtotal = _build_basket_items(items_by_id, items)
    except RuntimeError as e:
        return {"error": str(e)}

    result = await _client.create_basket(
        venue_id=venue["id"],
        items=wolt_items,
        currency=venue.get("currency", "EUR"),
    )
    basket_id = result.get("id")
    return {
        "basket_id": basket_id,
        "venue_id": venue["id"],
        "venue_name": venue["name"],
        "venue_slug": slug,
        "items_pushed": [
            f"{it['count']}× {it['name']} ({it['price']/100:.2f}€)"
            for it in wolt_items
        ],
        "subtotal_eur": subtotal,
        "checkout_link": f"{WOLT_WEB_BASE}/{DEFAULT_LANG}/{venue.get('country','est').lower()}/{(venue.get('city','tallinn') or 'tallinn').lower().replace(' ','-')}/restaurant/{slug}/checkout",
        "wolt_response": result,
    }


@mcp.tool()
async def prepare_wolt_checkout(
    slug: str,
    items: list[dict],
    delivery_method: str = "homedelivery",
    address_label: Optional[str] = None,
) -> dict:
    """
    Prepare a Wolt checkout: push the basket, then POST a purchase plan
    to /pages/checkout to validate pricing, delivery options, and get a
    checkout_id.

    Args:
      slug: venue slug.
      items: same shape as sync_basket_to_wolt.
      delivery_method: "homedelivery" or "takeaway".
      address_label: which saved address (for delivery). Default = config default.

    Returns checkout state with payable_amount, payment_breakdown, time_slots,
    plus a checkout_link for the user to open and confirm in the Wolt app.
    """
    cfg = _load_config()
    venue = await _client.get_venue(slug)
    menu = await _client.get_menu(slug)
    items_by_id = {it["id"]: it for it in menu["items"]}

    try:
        wolt_items, subtotal = _build_basket_items(items_by_id, items)
    except RuntimeError as e:
        return {"error": str(e)}

    # Sync basket first
    basket_resp = await _client.create_basket(
        venue_id=venue["id"], items=wolt_items, currency=venue.get("currency", "EUR")
    )
    basket_id = basket_resp.get("id")

    # Build menu_items in checkout payload shape
    checkout_menu_items = []
    for it in wolt_items:
        menu_item = items_by_id[it["id"]]
        checkout_menu_items.append({
            "id": it["id"],
            "venue_id": venue["id"],
            "count": it["count"],
            "base_price": int(round(menu_item["price"] * 100)),
            "end_amount": it["price"],
            "options": it["options"],
            "category_id": menu_item.get("category_id"),
            "category_ids": [menu_item["category_id"]] if menu_item.get("category_id") else [],
            "is_weighted_item": False,
            "alcohol_permille": 0,
            "exclude_from_credits": False,
            "exclude_from_discounts": False,
            "exclude_from_discounts_min_basket": False,
            "restrictions": [],
            "age_limit": None,
        })

    purchase_plan = {
        "venue": {
            "id": venue["id"],
            "currency": venue.get("currency", "EUR"),
            "country": (venue.get("country", "EST") or "EST").upper(),
        },
        "delivery_method": delivery_method,
        "menu_items": checkout_menu_items,
    }

    checkout = await _client.prepare_checkout(purchase_plan)
    checkout_link = (
        f"{WOLT_WEB_BASE}/{DEFAULT_LANG}/{venue.get('country','est').lower()}/"
        f"{(venue.get('city','tallinn') or 'tallinn').lower().replace(' ','-')}"
        f"/restaurant/{slug}/checkout"
    )

    return {
        "checkout_id": checkout.get("id"),
        "basket_id": basket_id,
        "venue_slug": slug,
        "venue_name": venue["name"],
        "delivery_method": delivery_method,
        "subtotal_eur": subtotal,
        "payable_amount_cents": checkout.get("payable_amount"),
        "purchasing_disabled": checkout.get("purchasing_disabled"),
        "checkout_link": checkout_link,
        "instructions": (
            f"Open the checkout link in your browser or Wolt app. "
            f"The basket is pre-filled with {len(wolt_items)} item(s). "
            f"Review delivery time/address and tap your payment method (Apple Pay/card)."
        ),
        "raw_checkout_keys": list(checkout.keys()),
    }


@mcp.tool()
async def get_checkout_link(slug: str) -> dict:
    """Build a deeplink to the Wolt checkout page for a venue. Use after
    sync_basket_to_wolt — opens directly into the venue's checkout with
    the basket loaded."""
    venue = await _client.get_venue(slug)
    country = (venue.get("country") or DEFAULT_COUNTRY).lower()
    city = (venue.get("city") or DEFAULT_CITY).lower().replace(" ", "-")
    return {
        "venue_slug": slug,
        "venue_name": venue["name"],
        "checkout_link": f"{WOLT_WEB_BASE}/{DEFAULT_LANG}/{country}/{city}/restaurant/{slug}/checkout",
        "venue_link": f"{WOLT_WEB_BASE}/{DEFAULT_LANG}/{country}/{city}/restaurant/{slug}",
    }


# --- v0.7: favorites, multi-venue, global search, wolt+, refresh ----------


@mcp.tool()
async def get_favorites(orders_limit: int = 100) -> dict:
    """
    Eestikeelne abivahend — analüüsi sinu Wolt'i tellimuste ajalugu ja
    tagasta lemmikud koondatuna.

    Args:
      orders_limit: kui palju viimaseid tellimusi analüüsida (default 100).

    Tagastab:
      top_venues_by_count, top_venues_by_spend, top_items, total_spend,
      avg_order, recent_30d_pattern.
    """
    from collections import Counter, defaultdict
    orders = await _client.my_orders(limit=orders_limit)

    venue_count = Counter()
    venue_spend = defaultdict(float)
    item_count = Counter()
    venue_items = defaultdict(Counter)
    amounts = []
    timestamps = []

    for o in orders:
        # Venue
        v_obj = o.get("venue")
        venue = v_obj.get("name") if isinstance(v_obj, dict) else (
            o.get("venue_name") or o.get("title") or o.get("name"))
        if not venue:
            continue
        # Amount
        tel = o.get("telemetry") or {}
        amount = 0.0
        if isinstance(tel, dict) and isinstance(tel.get("end_amount"), (int, float)):
            amount = tel["end_amount"] / 100
        else:
            total = o.get("total")
            if isinstance(total, str):
                try:
                    amount = float(total.replace("€", "").replace(",", ".").strip())
                except Exception:
                    amount = 0.0

        venue_count[venue] += 1
        venue_spend[venue] += amount
        amounts.append(amount)

        ts = o.get("timestamp") or o.get("created_at")
        if isinstance(ts, str):
            for fmt in ("%d/%m/%Y, %H:%M", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    t = dt.datetime.strptime(ts, fmt)
                    timestamps.append((t, venue, amount))
                    break
                except Exception:
                    pass

        for item in (o.get("items") or []):
            iname = item.get("name") if isinstance(item, dict) else None
            iqty = (item.get("count") if isinstance(item, dict) else None) or 1
            if iname:
                item_count[iname] += iqty
                venue_items[venue][iname] += iqty

    total_spend = round(sum(amounts), 2)
    avg = round(total_spend / len(amounts), 2) if amounts else 0.0

    # 30-day pattern
    recent = []
    if timestamps:
        newest = max(t for t, _, _ in timestamps)
        cutoff = newest - dt.timedelta(days=30)
        recent = [(t, v, a) for t, v, a in timestamps if t >= cutoff]

    return {
        "orders_analyzed": len(orders),
        "total_spend_eur": total_spend,
        "avg_order_eur": avg,
        "top_venues_by_count": [
            {"venue": v, "orders": n, "spend_eur": round(venue_spend[v], 2),
             "top_items": [{"name": i, "count": c}
                           for i, c in venue_items[v].most_common(5)]}
            for v, n in venue_count.most_common(10)
        ],
        "top_venues_by_spend": [
            {"venue": v, "spend_eur": round(s, 2), "orders": venue_count[v]}
            for v, s in sorted(venue_spend.items(), key=lambda x: -x[1])[:10]
        ],
        "top_items": [
            {"name": n, "count": c} for n, c in item_count.most_common(20)
        ],
        "last_30_days": {
            "orders": len(recent),
            "spend_eur": round(sum(a for _, _, a in recent), 2) if recent else 0.0,
        },
    }


@mcp.tool()
async def sync_multi_venue_baskets(orders: list[dict]) -> dict:
    """
    Sünki mitu cart'i Wolt'i poolele paralleelselt — üks tellimus = üks venue.
    Kasulik kui koostad nädala toidulauda mitmest pakkujast korraga.

    Args:
      orders: [{ slug, items: [{item_id, qty, options?}] }, ...]

    Tagastab:
      results: igale venue'le {basket_id, items_pushed, subtotal_eur, checkout_link}
      summary: kogu summa kõikide cart'ide peale, kõikide checkout linkide nimekiri.
    """
    results = []
    total = 0.0
    for o in orders:
        slug = o.get("slug")
        items = o.get("items") or []
        if not slug or not items:
            results.append({"slug": slug, "error": "missing slug or items"})
            continue
        try:
            r = await sync_basket_to_wolt(slug=slug, items=items)
            results.append(r)
            total += r.get("subtotal_eur", 0) or 0
        except Exception as e:
            results.append({"slug": slug, "error": str(e)})

    successful = [r for r in results if "basket_id" in r]
    return {
        "results": results,
        "venues_synced": len(successful),
        "venues_failed": len(results) - len(successful),
        "total_subtotal_eur": round(total, 2),
        "checkout_links": [
            {"venue": r.get("venue_name"), "link": r.get("checkout_link")}
            for r in successful
        ],
        "next_step": (
            f"Avage iga checkout link Chrome'is või Wolt äpis ja kinnitage "
            f"Apple Pay'ga. {len(successful)} cart(i) on Wolt'i poolele sünkitud."
        ),
    }


@mcp.tool()
async def search_items_global(
    query: str,
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    limit: int = 30,
) -> dict:
    """
    Otsi item'eid kõikidest Wolt'i pakkujatest korraga.
    Näiteks "tom yum kana" → kõik kohad Tallinnas, kus seda saab.

    Args:
      query: otsisõna (eesti või inglise keeles).
      lat, lon: koordinaadid (vaikimisi Tallinn).
      limit: max tulemusi (default 30). Wolt vastus on tohutu;
             tagastame ainult koondatud nimekirja.
    """
    raw = await _client.search_global(query, lat, lon)
    matches = []

    def visit(o, depth=0):
        if depth > 12 or len(matches) >= limit * 3:
            return
        if isinstance(o, dict):
            v = o.get("venue")
            if isinstance(v, dict):
                name = o.get("name") or o.get("title")
                if name:
                    price = o.get("price")
                    if isinstance(price, (int, float)) and price > 100:
                        price_eur = round(price / 100, 2)
                    elif isinstance(price, str):
                        try:
                            price_eur = float(price.replace("€", "").replace(",", ".").strip())
                        except Exception:
                            price_eur = None
                    else:
                        price_eur = None
                    rating = v.get("rating")
                    if isinstance(rating, dict):
                        rating = rating.get("score")
                    matches.append({
                        "item_name": name,
                        "venue": v.get("name"),
                        "venue_slug": v.get("slug"),
                        "price_eur": price_eur,
                        "rating": rating,
                        "estimate_min": v.get("estimate"),
                        "online": v.get("online"),
                    })
            for x in o.values():
                visit(x, depth + 1)
        elif isinstance(o, list):
            for x in o:
                visit(x, depth + 1)

    visit(raw)
    # Dedupe by (item_name, venue_slug)
    seen = set()
    unique = []
    for m in matches:
        key = (m["item_name"], m["venue_slug"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)

    # Sort: online first, then by rating desc, then by price asc
    unique.sort(key=lambda m: (
        not m.get("online", False),
        -(m.get("rating") or 0),
        m.get("price_eur") or 9999,
    ))
    return {
        "query": query,
        "total_matches": len(unique),
        "results": unique[:limit],
    }


@mcp.tool()
async def get_wolt_plus_status() -> dict:
    """
    Kontrolli, kas sul on aktiivne Wolt+ tellimus (mis annab tasuta tarne).
    """
    data = await _client.wolt_plus_status()
    # Normalize — find an active subscription
    subs = data.get("subscriptions") or data.get("results") or (
        data if isinstance(data, list) else [])
    active = [s for s in subs if isinstance(s, dict)
              and (s.get("status") == "active" or s.get("active") is True)]
    return {
        "has_wolt_plus": bool(active),
        "active_subscriptions": active[:3],
        "all_subscriptions_count": len(subs) if isinstance(subs, list) else 0,
    }


@mcp.tool()
async def refresh_session() -> dict:
    """
    Legacy bearer-mode refresh — tries guessed Wolt auth endpoints.

    ⚠️ Kept only as a last-resort fallback for kind="bearer" sessions; the
    endpoints below were never confirmed against a real HAR capture and may
    not work. If your session is kind="cookie", you don't need this tool at
    all — the session renews itself automatically on every authenticated
    call (see get_session_status). Switch to kind="cookie" via set_session()
    instead of relying on this.
    """
    sess = _load_session()
    if not sess.is_set():
        return {"ok": False, "error": "Sessiooni pole salvestatud."}
    if sess.kind == "cookie":
        return {
            "ok": True,
            "message": "Cookie-mode session already self-renews on every "
                        "authenticated call — no action needed. This "
                        "fallback only applies to kind=\"bearer\" sessions.",
        }
    new_tok = await _client.refresh_access_token()
    if new_tok:
        sess.token = new_tok
        sess.kind = "bearer"
        _save_session(sess)
        ttl = _token_ttl_seconds(sess)
        _audit("session_refreshed", new_ttl=ttl)
        return {"ok": True, "new_ttl_seconds": ttl}
    return {
        "ok": False,
        "error": "Auto-refresh ebaõnnestus — refresh endpoint on tundmatu. "
                 "Värskenda käsitsi DevTools'ist (Copy as fetch).",
        "manual_steps": [
            "1. Chrome → wolt.com → DevTools (Cmd+Opt+I)",
            "2. Network tab → filter consumer-api → Cmd+R",
            "3. Vali ükskõik milline päring → right-click → Copy → Copy as fetch",
            "4. Eralda 'authorization: Bearer eyJ...' osa pärast 'Bearer '",
            "5. Kutsu set_session(token='eyJ...', kind='bearer')",
        ],
    }


@mcp.tool()
async def get_audit_log(limit: int = 20) -> dict:
    """
    Vaata viimaseid MCP tegevusi audit logist (~/.wolt-mcp/audit.jsonl).
    Kasulik kontrollimaks, mida MCP server on hiljuti teinud.
    """
    if not AUDIT_FILE.exists():
        return {"entries": [], "message": "Audit log on tühi."}
    lines = AUDIT_FILE.read_text().strip().splitlines()
    last = lines[-limit:]
    entries = []
    for line in last:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return {
        "entries": entries,
        "total_in_log": len(lines),
        "shown": len(entries),
    }


# (get_delivery_slots & place_delivery_order removed in v0.5 — replaced by
#  sync_basket_to_wolt + prepare_wolt_checkout + get_checkout_link below)
async def _UNUSED_place_delivery_order(
    slug: str,
    items: list[dict],
    delivery_time_iso: str,
    payment_method_id: Optional[str] = None,
    address_label: Optional[str] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Place a HOME DELIVERY order. Two-step: dry_run=True first, then
    re-call with dry_run=False + confirm_token from the dry-run response.

    Args:
      slug: venue slug.
      items: [{ item_id, qty, options? }] — same shape as add_to_cart.
      delivery_time_iso: ISO 8601 timestamp from get_delivery_slots,
                         e.g. "2026-04-28T14:00:00Z".
      payment_method_id: from get_payment_methods. If omitted, uses
                         config.default_payment_method_id.
      address_label: which saved address. Default = config default.
      dry_run: true to preview, false to submit (with valid confirm_token).
      confirm_token: token returned by the dry-run call.
      notes: optional note to the courier (overrides address.instructions).

    ⚠️ Submitting will charge your saved card. Always review dry-run
    output (especially total + delivery_time + address) before confirming.
    """
    cfg = _load_config()
    use_label = address_label or cfg.default_address_label
    addr = cfg.addresses.get(use_label)
    if not addr:
        return {"error": f"Unknown address label '{use_label}'"}

    pm_id = payment_method_id or cfg.default_payment_method_id
    if not pm_id:
        return {"error": "No payment_method_id provided and no default set. "
                          "Use get_payment_methods then set_default_payment_method."}

    venue = await _client.get_venue(slug)
    menu = await _client.get_menu(slug)
    items_by_id = {it["id"]: it for it in menu["items"]}

    order_items: list[dict] = []
    subtotal = 0.0
    for entry in items:
        iid = entry.get("item_id")
        qty = int(entry.get("qty", 1))
        if iid not in items_by_id:
            return {"error": f"item_id {iid} not found in venue menu"}
        m = items_by_id[iid]
        opts = entry.get("options") or []
        opt_delta = sum(o.get("price_delta", 0.0) for o in opts)
        line_price_cents = int(round((m["price"] + opt_delta) * 100))
        subtotal += (m["price"] + opt_delta) * qty
        order_items.append({
            "id": iid,
            "count": qty,
            "options": [
                {"id": o.get("option_id"), "value": o.get("value_id")}
                for o in opts if o.get("option_id") and o.get("value_id")
            ],
            "baseprice": line_price_cents,
        })

    courier_comment = notes or addr.get("instructions") or ""
    if addr.get("floor"):
        courier_comment = f"Floor {addr['floor']}. {courier_comment}".strip()

    payload = {
        "venue_id": venue["id"],
        "items": order_items,
        "delivery_method": "home_delivery",
        "delivery_location": {
            "lat": addr["lat"],
            "lon": addr["lon"],
            "address": addr["address"],
            "floor": addr.get("floor"),
            "comment": courier_comment,
        },
        "preorder_time": delivery_time_iso,
        "payment_method": {"id": pm_id},
        "comment": notes or "",
        "currency": venue.get("currency", "EUR"),
    }

    expected_token = _confirm_token_for(payload)
    summary = {
        "venue_slug": slug,
        "venue_name": venue["name"],
        "venue_address": venue.get("address"),
        "currency": venue.get("currency", "EUR"),
        "items_summary": [
            f"{it['count']}× {items_by_id[it['id']]['name']} "
            f"({it['baseprice']/100:.2f})"
            for it in order_items
        ],
        "subtotal": round(subtotal, 2),
        "delivery_time": delivery_time_iso,
        "delivery_to": addr["address"],
        "delivery_label": use_label,
        "courier_note": courier_comment,
        "payment_method_id": pm_id,
    }

    if dry_run:
        return {
            "dry_run": True,
            "summary": summary,
            "confirm_token": expected_token,
            "instructions": (
                f"Review the summary. To submit: call place_delivery_order again "
                f"with dry_run=False and confirm_token=\"{expected_token}\""
            ),
            "_payload_preview": payload,
        }

    if confirm_token != expected_token:
        return {
            "error": "confirm_token mismatch — re-run with dry_run=True first",
            "expected_token_hint": expected_token[:4] + "…",
        }

    result = await _client.place_order(payload)
    return {
        "submitted": True,
        "summary": summary,
        "wolt_response": result,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _human_order_instructions(cart: Cart) -> str:
    if not cart.lines:
        return "Cart is empty."
    lines: list[str] = [f"Open Wolt → {cart.venue_name} → add:"]
    for l in cart.lines:
        opts = ""
        if l.options:
            opts = " (" + ", ".join(
                f"{o.get('option_name','?')}: {o.get('value_name','?')}"
                for o in l.options
            ) + ")"
        notes = f" — note: {l.notes}" if l.notes else ""
        lines.append(f"  {l.qty}× {l.name}{opts}{notes}")
    cur = cart.currency or "EUR"
    lines.append(f"\nSubtotal: {cart.subtotal:.2f} {cur} (delivery fee not included)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        mcp.run()
    finally:
        try:
            asyncio.run(_client.close())
        except Exception:
            pass


if __name__ == "__main__":
    main()
