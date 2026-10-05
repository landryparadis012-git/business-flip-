#!/usr/bin/env python3
"""
Flip Broker v1 — deal-sourcing engine (no-inventory broker model).

Finds underpriced Facebook Marketplace listings (via Apify), prices them
against REAL comps from comps.json (never LLM-guessed), and sends cards to
its own Telegram bot with buttons: TAKEN / CONTACTED / POSTED.
/won <price> and /lost close a deal. Every card + status change is emitted
as a signed event to OpsHub, which registers deals and manages reminders.

You never buy: lock the seller, advertise, find the buyer, collect the fee.
Stdlib only. Runs free on GitHub Actions.
"""
import os, re, json, time, hmac, hashlib, urllib.request, urllib.parse
from datetime import datetime, timezone

# ---------------- paths & constants ----------------
STATE_DIR = "data"
STATE_PATH = os.path.join(STATE_DIR, "state.json")
WATCHES_PATH = "watches.json"
COMPS_PATH = "comps.json"
TG_API = "https://api.telegram.org/bot{}/{}"
APIFY_RUN = ("https://api.apify.com/v2/acts/"
             "apify~facebook-marketplace-scraper/run-sync-get-dataset-items")

MONTHLY_CAP = 800.0        # Apify results/month (free $5 credit)
MIN_COMPS = 3              # fewer real comps = no card
DEAL_MARGIN = 0.20         # spread/low_comp needed for FIRE DEAL
WATCH_MARGIN = 0.10        # threshold for WATCH
COSTS_PCT = 0.10           # transport/fees/detailing/surprises
BUFFER_PCT = 0.05          # uncertainty buffer
BUFFER_MIN = 25.0
FEE_PCT = 0.35             # your fee as a share of expected spread
FEE_MIN, FEE_MAX = 50.0, 1500.0
LIST_DISCOUNT = 0.05       # list just under the lowest comp to move fast
STALE_DAYS = 30            # comps older than this are ignored
MAX_ACTIVE = 5             # don't chase more deals than this at once
RISK_WORDS = ("salvage", "check engine", "no title", "cracked", "broken",
              "not working", "parts only", "for parts", "torn", "missing", "junk")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# ---------------- http ----------------
def http_json(url, payload=None, headers=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or UA)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# ---------------- telegram ----------------
def tg_token():
    tok = os.environ.get("FLIP_TG_TOKEN", "").strip()
    if tok.lower().startswith("bot"):
        tok = tok[3:].strip()
    return tok

def tg(method, payload):
    tok = tg_token()
    if not tok:
        return None
    return http_json(TG_API.format(tok, method), payload)

def tg_send(text, buttons=None):
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not tg_token() or not chat:
        return
    p = {"chat_id": chat, "text": text}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    try:
        tg("sendMessage", p)
    except Exception as e:
        print("[tg] send failed:", e)

def tg_answer(cb_id, text):
    try:
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})
    except Exception:
        pass

def tg_get_updates(offset):
    tok = tg_token()
    if not tok:
        return []
    try:
        url = TG_API.format(tok, "getUpdates") + "?timeout=0"
        resp = http_json(url, headers=UA)
    except Exception as e:
        print("[tg] getUpdates failed:", e)
        return []
    ups = resp.get("result", []) if isinstance(resp, dict) else []
    if not ups:
        return []
    ids = [int(u["update_id"]) for u in ups if isinstance(u, dict) and "update_id" in u]
    if not ids:
        return []
    stored = int(offset or 0)
    newest = max(ids)
    if stored > newest and stored - newest > 1000:
        stored = 0
    return [u for u in ups if int(u.get("update_id", -1)) >= stored]

# ---------------- state ----------------
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def load_state():
    s = load_json(STATE_PATH, None) or {}
    s.setdefault("tg_offset", 0)
    s.setdefault("last_discovery_date", None)
    s.setdefault("usage", {"month": None, "fetched": 0, "warned": False, "blocked": False})
    s.setdefault("seen", {})
    s.setdefault("deals", {})
    return s

def save_state(s):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(s, f, indent=1)

# ---------------- comps & pricing ----------------
def load_comps(watch_id):
    data = load_json(COMPS_PATH, {})
    entry = (data.get("comps") or {}).get(watch_id) or {}
    out = []
    today = datetime.now(timezone.utc).date()
    for c in entry.get("comps", []):
        try:
            price = float(c["price"])
        except (KeyError, TypeError, ValueError):
            continue
        d = None
        try:
            d = datetime.strptime(str(c.get("date", "")), "%Y-%m-%d").date()
        except ValueError:
            pass
        if d and (today - d).days > STALE_DAYS:
            continue  # stale comp — ignored
        out.append((price, c.get("source", "?")))
    return out

def risk_flags(title):
    t = (title or "").lower()
    return [w for w in RISK_WORDS if w in t]

def price_deal(watch, asking, title):
    """REAL comps only -> honest broker math. Never guesses prices."""
    comps = load_comps(watch["id"])
    if len(comps) < MIN_COMPS:
        return None
    low = min(c[0] for c in comps)
    src = ", ".join(sorted({c[1] for c in comps}))
    flags = risk_flags(title)
    fee = min(FEE_MAX, max(FEE_MIN, FEE_PCT * max(low - asking, 0.0)))
    buffer = max(BUFFER_MIN, low * BUFFER_PCT)
    max_offer = round(low * (1 - COSTS_PCT) - fee - buffer, 2)
    est_list = round(low * (1 - LIST_DISCOUNT), 2)
    spread = round(low - asking, 2)
    margin = (spread / low) if low > 0 else 0.0
    tier = "PASS"
    if not flags and margin >= DEAL_MARGIN:
        tier = "DEAL"
    elif not flags and margin >= WATCH_MARGIN:
        tier = "WATCH"
    return {"low": round(low, 2), "src": src, "n": len(comps),
            "max_offer": max_offer, "est_list": est_list, "spread": spread,
            "fee": round(fee, 2), "margin": round(margin, 3),
            "tier": tier, "flags": flags}

# ---------------- cards ----------------
def seller_draft(title):
    return ('Hi! Is the ' + title + ' still available? I connect sellers with '
            'serious local buyers - if it is still there, may I share your '
            'listing with a buyer who is hunting? Not buying it myself, and I '
            "won't repost your photos without your OK.")

def deal_card(watch, it, pr, tag=""):
    icon = "🔥" if pr["tier"] == "DEAL" else "🟡"
    lines = [
        f"{icon} {pr['tier']} — {it['title'][:60]} {tag}".rstrip(),
        f"Asking ${it['price']:,.0f} | Comps low ${pr['low']:,.0f} "
        f"({pr['n']} comps: {pr['src']})",
        f"MAX OFFER to seller: ${pr['max_offer']:,.0f}",
        f"Suggested list price: ${pr['est_list']:,.0f}",
        f"EST. spread ${pr['spread']:,.0f} ({pr['margin']*100:.0f}%) | "
        f"your fee ≈ ${pr['fee']:,.0f}",
        f"{it['url']}",
        "",
        "Draft to seller:",
        seller_draft(it['title'][:50]),
    ]
    if pr["flags"]:
        lines.insert(1, "⚠️ Risk: " + ", ".join(pr["flags"]))
    return "\n".join(lines)

def deal_buttons(did):
    return [[{"text": "✅ TAKEN", "callback_data": f"T|take:{did}"},
             {"text": "📞 CONTACTED", "callback_data": f"T|contacted:{did}"}],
            [{"text": "📤 POSTED", "callback_data": f"T|posted:{did}"}]]

# ---------------- OpsHub events ----------------
def hub_emit(ev_type, deal_id, payload):
    url = os.environ.get("HUB_URL", "").strip()
    secret = os.environ.get("HUB_WEBHOOK_SECRET", "").strip()
    if not url or not secret:
        return
    ev = {"schema_version": 1,
          "event_id": f"flip-{deal_id}-{ev_type.split('.')[1]}",
          "type": ev_type, "source": "flip", "occurred_at": int(time.time())}
    ev.update(payload)
    try:
        body = json.dumps(ev).encode()
        mac = hmac.new(secret.encode(), body, hashlib.sha256)
        req = urllib.request.Request(
            url.rstrip("/") + "/ingest", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "x-hub-signature-256": "sha256=" + mac.hexdigest()})
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"[hub] {ev_type} -> {r.status}")
    except Exception as e:
        print(f"[hub] {ev_type} failed (non-fatal): {e}")

# ---------------- deal statuses ----------------
NEXT = {
    "taken": "Next: message the seller with the probe draft. Press CONTACTED when sent.",
    "contacted": "Next: seller OK? Post your buyer listing, then press POSTED.",
    "posted": "Next: work buyer leads. Close with /won <price> or /lost.",
}

def set_status(state, did, status):
    d = state["deals"].get(did)
    if not d or d["status"] == status:
        return False
    d["status"] = status
    d["updated_ts"] = int(time.time())
    hub_emit("flip.status_changed", did,
             {"deal_id": did, "status": status, "watch_id": d.get("watch"),
              "title": d.get("title", ""), "asking": d.get("asking")})
    return True

def handle_callback(state, cb):
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    m = re.match(r"T\|(take|contacted|posted):(.+)", str(cb.get("data", "")))
    if not m or str(cb.get("message", {}).get("chat", {}).get("id", "")) != chat:
        return
    action, did = m.group(1), m.group(2)
    status = {"take": "taken", "contacted": "contacted", "posted": "posted"}[action]
    d = state["deals"].get(did)
    if not d:
        tg_answer(cb.get("id"), "Unknown deal — card may have expired.")
        return
    if d["status"] == status:
        tg_answer(cb.get("id"), "Already " + status)
        return
    if set_status(state, did, status):
        tg_answer(cb.get("id"), "Logged: " + status)
        tg_send(f"✅ {d['title'][:40]} → {status.upper()}\n{NEXT[status]}\n"
                f"OpsHub registers this and manages follow-up reminders.")

def pick_active(state, did=None):
    act = {k: d for k, d in state["deals"].items()
           if d["status"] not in ("won", "lost")}
    if did:
        return did if did in act else None
    if len(act) == 1:
        return next(iter(act))
    if act:
        return max(act, key=lambda k: act[k].get("updated_ts", 0))
    return None

def close_deal(state, parts, outcome):
    did, price = None, None
    for p in parts[1:]:
        try:
            price = float(p.replace("$", "").replace(",", ""))
        except ValueError:
            did = p
    did = pick_active(state, did)
    if not did:
        tg_send("Nothing to close (or several active — /status lists them).")
        return
    d = state["deals"][did]
    d["status"] = outcome
    d["updated_ts"] = int(time.time())
    if outcome == "won" and price is not None:
        d["won_price"] = price
    hub_emit("flip.deal_closed", did,
             {"deal_id": did, "status": outcome, "price": price,
              "watch_id": d.get("watch"), "title": d.get("title", ""),
              "asking": d.get("asking")})
    if outcome == "won":
        tg_send(f"🏁 WON — {d['title'][:40]} at ${price:,.0f}."
                f"\nOpsHub logs the P&L under flipping.")
    else:
        tg_send(f"➖ LOST — {d['title'][:40]} logged. Comps stay. Next.")

def handle_command(state, msg):
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if str(msg.get("chat", {}).get("id", "")) != chat:
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    if cmd == "/status":
        act = [d for d in state["deals"].values()
               if d["status"] not in ("won", "lost")]
        u = state["usage"]
        lines = [f"🤝 Flip Broker — {len(act)} active / {len(state['deals'])} total",
                 f"Apify this month: {u['fetched']}/{int(MONTHLY_CAP)}"]
        for d in act[:8]:
            lines.append(f"• {d['title'][:32]} — {d['status']} (${d.get('asking', 0):,.0f})")
        tg_send("\n".join(lines))
    elif cmd == "/won":
        close_deal(state, parts, "won")
    elif cmd == "/lost":
        close_deal(state, parts, "lost")

# ---------------- discovery (Apify) ----------------
def apify_search(watch, cap):
    tok = os.environ.get("APIFY_TOKEN", "").strip()
    if not tok:
        print("[apify] no APIFY_TOKEN — discovery skipped")
        return None
    url = ("https://www.facebook.com/marketplace/" + watch["city"] +
           "/search?query=" + urllib.parse.quote(watch["query"]))
    req_url = APIFY_RUN + "?token=" + urllib.parse.quote(tok) + "&timeout=480"
    try:
        items = http_json(req_url, payload={"urls": [url], "resultsLimit": int(cap)},
                          timeout=540)
    except Exception as e:
        print(f"[apify] {watch['id']} run failed: {e}")
        return None
    return items if isinstance(items, list) else []

def parse_item(it):
    url = it.get("posting_url") or it.get("url") or ""
    if not url:
        return None
    price = it.get("listing_price")
    if isinstance(price, dict):
        price = price.get("amount", price.get("price"))
    if price is None:
        price = it.get("price")
    if isinstance(price, str):
        m = re.search(r"[\d,]+(?:\.\d+)?", price)
        if not m:
            return None
        price = float(m.group().replace(",", ""))
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    title = it.get("marketplace_listing_title") or it.get("title") or ""
    did = str(it.get("listing_id") or hashlib.md5(url.encode()).hexdigest()[:16])
    loc = it.get("location")
    city = loc.get("city", "") if isinstance(loc, dict) else ""
    return {"id": did, "title": title or "Untitled listing", "price": price,
            "url": url.split("?")[0], "city": city}

def process_listing(state, w, p):
    if p["id"] in state["deals"]:
        return  # known deal — never re-card or reset its status
    seen = state["seen"]
    old = seen.get(p["id"])
    if p["price"] > float(w.get("max_price", 10**9)):
        return
    pr = price_deal(w, p["price"], p["title"])
    tag = ""
    if old:
        dropped = old["p"] > 0 and p["price"] <= old["p"] * 0.9
        if not dropped or old.get("card") or not pr or pr["tier"] == "PASS":
            return
        tag = "💸 PRICE DROP"
    else:
        if not pr or pr["tier"] == "PASS":
            seen[p["id"]] = {"w": w["id"], "p": p["price"],
                             "t": int(time.time()), "card": False}
            return
        active = sum(1 for d in state["deals"].values()
                     if d["status"] not in ("won", "lost"))
        if active >= MAX_ACTIVE:
            return
    seen[p["id"]] = {"w": w["id"], "p": p["price"], "t": int(time.time()), "card": True}
    state["deals"][p["id"]] = {"watch": w["id"], "title": p["title"],
                               "asking": p["price"], "status": "found",
                               "opened_ts": int(time.time()),
                               "updated_ts": int(time.time())}
    tg_send(deal_card(w, p, pr, tag), deal_buttons(p["id"]))
    hub_emit("flip.deal_found", p["id"],
             {"deal_id": p["id"], "watch_id": w["id"], "title": p["title"],
              "asking": p["price"], "low_comp": pr["low"],
              "max_offer": pr["max_offer"], "est_list": pr["est_list"],
              "est_spread": pr["spread"], "city": p.get("city", ""), "url": p["url"]})
    print(f"[deal] {pr['tier']} {p['title'][:40]} asking {p['price']}")

def discovery(state, watches):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state["last_discovery_date"] == today:
        return
    u = state["usage"]
    if u["month"] != today[:7]:
        u["month"], u["fetched"] = today[:7], 0
        u["warned"], u["blocked"] = False, False
    if u["fetched"] >= MONTHLY_CAP:
        if not u["blocked"]:
            u["blocked"] = True
            tg_send(f"🛑 Apify cap {int(MONTHLY_CAP)} reached — discovery paused "
                    f"until next month. Buttons and commands still work.")
        return
    print(f"[scan] discovery: {len(watches)} watches, "
          f"budget {u['fetched']}/{int(MONTHLY_CAP)}")
    for w in watches:
        if u["fetched"] >= MONTHLY_CAP:
            tg_send("🛑 Apify cap reached mid-scan — remaining watches skipped.")
            break
        items = apify_search(w, w.get("results_cap", 8))
        if items is None:
            continue
        u["fetched"] += len(items)
        for it in items:
            p = parse_item(it)
            if p:
                process_listing(state, w, p)
        if not u["warned"] and u["fetched"] >= MONTHLY_CAP * 0.8:
            u["warned"] = True
            tg_send(f"⚠️ Apify budget at {u['fetched']}/{int(MONTHLY_CAP)} (80%).")
        time.sleep(2)
    state["last_discovery_date"] = today

# ---------------- main ----------------
def main():
    watches = load_json(WATCHES_PATH, {}).get("watches", [])
    state = load_state()
    for u in tg_get_updates(state["tg_offset"]):
        state["tg_offset"] = int(u.get("update_id", state["tg_offset"])) + 1
        if "callback_query" in u:
            handle_callback(state, u["callback_query"])
        elif "message" in u:
            handle_command(state, u["message"])
    if watches:
        discovery(state, watches)
    save_state(state)
    print(f"[done] seen={len(state['seen'])} deals={len(state['deals'])} "
          f"usage={state['usage']['fetched']}/{int(MONTHLY_CAP)}")

if __name__ == "__main__":
    main()
