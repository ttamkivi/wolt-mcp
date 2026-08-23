"""
analyze_history.py — Wolt order history analyzer.

v0.2 — fixed for actual Wolt order-xp response shape:
  - Amount from telemetry.end_amount (cents int) preferred over "€67.97" string
  - Timestamp parsed from "DD/MM/YYYY, HH:MM" Estonian format
  - Venue from nested venue.name

Usage:
    /Users/taavi/Downloads/wolt-mcp/.venv/bin/python \\
        /Users/taavi/Downloads/wolt-mcp/analyze_history.py [LIMIT] [--offline]

  --offline: skip API call, analyze ~/.wolt-mcp/orders_export.json directly.
             Use this to re-analyze without burning a fresh token.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import wolt_mcp as W


def fmt_eur(amount):
    return f"{amount:>8.2f} €"


def parse_amount(o):
    """Wolt order-xp gives both 'total' as '€67.97' string AND
    'telemetry.end_amount' as int cents. Telemetry is more reliable."""
    tel = o.get("telemetry") or {}
    if isinstance(tel, dict) and isinstance(tel.get("end_amount"), (int, float)):
        return float(tel["end_amount"]) / 100

    total = o.get("total") or o.get("total_price") or o.get("price")
    if isinstance(total, str):
        clean = (total.replace("€", "").replace("$", "")
                      .replace("\xa0", "").replace(" ", "").replace(",", "."))
        try:
            return float(clean)
        except Exception:
            return 0.0
    if isinstance(total, dict):
        a = total.get("amount") or total.get("value")
        if isinstance(a, (int, float)):
            return float(a) / 100 if a > 1000 else float(a)
    if isinstance(total, (int, float)):
        return float(total) / 100 if total > 1000 else float(total)
    return 0.0


def parse_timestamp(o):
    """Wolt order-xp gives 'timestamp' as '17/04/2026, 15:38' (Estonian DD/MM)."""
    ts = (o.get("timestamp") or o.get("created_at") or o.get("submitted_at")
          or o.get("delivery_time") or o.get("time"))
    if not ts:
        return None
    if isinstance(ts, dict):
        ts = ts.get("iso") or ts.get("$date") or ts.get("epoch")
    if isinstance(ts, (int, float)):
        try:
            return dt.datetime.fromtimestamp(ts if ts < 10**12 else ts / 1000)
        except Exception:
            return None
    if not isinstance(ts, str):
        return None
    for fmt in ("%d/%m/%Y, %H:%M", "%d/%m/%Y %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return dt.datetime.strptime(ts, fmt)
        except Exception:
            pass
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return t.replace(tzinfo=None) if t.tzinfo else t
    except Exception:
        return None


def parse_venue(o):
    v_obj = o.get("venue")
    if isinstance(v_obj, dict):
        return v_obj.get("name")
    return o.get("venue_name") or o.get("title") or o.get("name")


def parse_items(o):
    items = o.get("items") or []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        iname = item.get("name")
        iqty = item.get("count") or item.get("qty") or 1
        iprice = item.get("price") or item.get("baseprice") or 0
        if isinstance(iprice, str):
            iprice = iprice.replace("€", "").replace(",", ".").strip()
            try:
                iprice = float(iprice)
            except Exception:
                iprice = 0.0
        elif isinstance(iprice, (int, float)) and iprice > 1000:
            iprice = iprice / 100
        if iname:
            out.append((iname, iqty, float(iprice)))
    return out


async def main():
    args = sys.argv[1:]
    offline = "--offline" in args
    args = [a for a in args if not a.startswith("--")]
    limit = int(args[0]) if args else 100

    export_path = Path.home() / ".wolt-mcp" / "orders_export.json"

    if offline and export_path.exists():
        print(f"Offline mode — reading {export_path}")
        orders = json.loads(export_path.read_text())
    else:
        sess = W._load_session()
        if not sess.is_set():
            print("ERROR: no session token. Refresh ~/.wolt-mcp/session.json.")
            sys.exit(1)
        print(f"Pulling last {limit} orders from Wolt…")
        orders = await W._client.my_orders(limit=limit)
        await W._client.close()
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(json.dumps(orders, indent=2, ensure_ascii=False))
        print(f"Raw saved: {export_path}")

    if not isinstance(orders, list):
        print(f"Unexpected response shape: {type(orders).__name__}")
        sys.exit(1)
    print(f"Total order records: {len(orders)}\n")

    # Aggregate
    venue_spend = defaultdict(float)
    venue_count = Counter()
    item_count = Counter()
    item_spend = defaultdict(float)
    venue_items = defaultdict(Counter)
    order_amounts = []
    timestamps = []

    for o in orders:
        venue = parse_venue(o)
        if not venue:
            continue
        amount = parse_amount(o)
        venue_spend[venue] += amount
        venue_count[venue] += 1
        order_amounts.append((amount, venue))

        t = parse_timestamp(o)
        if t:
            timestamps.append((t, venue, amount))

        for iname, iqty, iprice in parse_items(o):
            item_count[iname] += iqty
            item_spend[iname] += iprice * iqty
            venue_items[venue][iname] += iqty

    total_spend = sum(a for a, _ in order_amounts)
    n_orders = len(order_amounts)
    avg = total_spend / n_orders if n_orders else 0

    bar = "=" * 70

    print(bar)
    print(f"OVERALL — {n_orders} orders, {fmt_eur(total_spend)} total, "
          f"avg {fmt_eur(avg)}/order")
    print(bar)
    print()

    print(bar)
    print("TOP 10 VENUES BY SPEND")
    print(bar)
    for v, total in sorted(venue_spend.items(), key=lambda x: -x[1])[:10]:
        avg_v = total / venue_count[v] if venue_count[v] else 0
        print(f"  {fmt_eur(total)}  ({venue_count[v]:>2}× orders, avg {fmt_eur(avg_v)})  {v}")
    print()

    print(bar)
    print("TOP 10 VENUES BY ORDER COUNT")
    print(bar)
    for v, n in venue_count.most_common(10):
        print(f"  {n:>3}× orders   {fmt_eur(venue_spend[v])}   {v}")
    print()

    print(bar)
    print("TOP 20 MOST-ORDERED ITEMS")
    print(bar)
    for name, n in item_count.most_common(20):
        print(f"  {n:>4}×   {fmt_eur(item_spend[name])}   {name}")
    print()

    print(bar)
    print("LARGEST SINGLE ORDERS")
    print(bar)
    for amount, venue in sorted(order_amounts, key=lambda x: -x[0])[:10]:
        print(f"  {fmt_eur(amount)}   {venue}")
    print()

    print(bar)
    print("LAST 30 DAYS PATTERN")
    print(bar)
    if timestamps:
        # Use the newest order timestamp as 'now' — handles offline analysis
        # without time-zone weirdness
        newest = max(t for t, _, _ in timestamps)
        cutoff = newest - dt.timedelta(days=30)
        recent = [t for t in timestamps if t[0] >= cutoff]
        print(f"  Window: {cutoff.date()} → {newest.date()}")
        print(f"  Orders in window: {len(recent)}")
        if recent:
            print(f"  Total spend: {fmt_eur(sum(a for _, _, a in recent))}")
            weeks = defaultdict(lambda: {"count": 0, "spend": 0.0, "venues": Counter()})
            for t, v, a in recent:
                wk = t.strftime("%G-W%V")
                weeks[wk]["count"] += 1
                weeks[wk]["spend"] += a
                weeks[wk]["venues"][v] += 1
            print()
            print(f"  {'Week':<10} {'Orders':>6} {'Spend':>12}   Top venue")
            for wk in sorted(weeks.keys()):
                w = weeks[wk]
                top = w["venues"].most_common(1)[0] if w["venues"] else ("—", 0)
                print(f"  {wk:<10} {w['count']:>6} {fmt_eur(w['spend']):>12}   "
                      f"{top[0]} ({top[1]}×)")
    else:
        print("  (no parseable timestamps)")
    print()

    print(bar)
    print("WHAT YOU BUY FROM EACH TOP VENUE")
    print(bar)
    for v, _ in venue_count.most_common(5):
        print(f"\n  {v}  ({venue_count[v]} orders, {fmt_eur(venue_spend[v])})")
        for iname, n in venue_items[v].most_common(8):
            print(f"      {n:>3}×  {iname}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
