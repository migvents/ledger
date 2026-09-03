"""Daily alert check for the Ledger portfolio.
Reads config.json, fetches one year of daily closes per fund from Twelve Data,
computes the target-weighted drawdown of the equity funds from their 1-year peak,
and sends phone notifications through ntfy.sh when a dip tier is crossed,
when the rebalance month starts, and on the monthly TOB reminder day.
State (which tiers have already fired) is kept in state.json and committed back.
"""
import json, os, time, base64, datetime as dt, urllib.request, urllib.parse, urllib.error

API = os.environ["TWELVE_DATA_KEY"]
TOPIC = os.environ["NTFY_TOPIC"]
cfg = json.load(open("config.json"))
try:
    state = json.load(open("state.json"))
except FileNotFoundError:
    state = {"fired": [], "last_rebalance_alert": "", "last_tob_alert": "", "setup_warned": ""}

def get(url):
    """Return parsed JSON; Twelve Data sends errors as JSON with a 4xx status."""
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"status": "error", "message": f"HTTP {e.code}"}

def notify(title, msg, prio="default"):
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=msg.encode("utf-8"),
        headers={"Title": title, "Priority": prio})
    urllib.request.urlopen(req, timeout=30)
    print("sent:", title, "|", msg)

def stooq(sym):
    """Free daily CSV for European listings. Returns closes newest-first, or None."""
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(sym)}&i=d"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            rows = r.read().decode().strip().splitlines()
        if len(rows) < 30 or not rows[0].lower().startswith("date"):
            return None
        out = []
        for line in rows[1:]:
            p = line.split(",")
            if len(p) >= 5:
                try: out.append((p[0], float(p[4])))
                except ValueError: pass
        out = [c for _, c in sorted(out)][-400:]
        return list(reversed(out)) if len(out) >= 30 else None
    except Exception as e:
        return None

def live(f):
    """Latest traded price for the fund's US twin, for an intraday view."""
    try:
        q = urllib.parse.urlencode({"symbol": f["symbol"], "apikey": API})
        j = get("https://api.twelvedata.com/price?" + q)
        p = float(j.get("price", 0))
        return p if p > 0 else None
    except Exception:
        return None

def series(f):
    """Try the symbol several ways; return list of closes (newest first) or None."""
    attempts = [
        {"symbol": f["symbol"], "exchange": f.get("exchange", "")},
        {"symbol": f["symbol"]},
    ]
    base = (f.get("stooq") or "").split(".")[0] or f["ticker"].lower()
    if f.get("stooq") is not None and f.get("stooq") != "":
        remembered = state.get("stooq_ok", {}).get(f["ticker"])
        cands = ([remembered] if remembered else []) + [f"{base}.{sfx}" for sfx in ("uk", "de", "fr", "it", "nl")]
        for sym in dict.fromkeys(cands):
            c = stooq(sym)
            if c:
                print(f"{f['ticker']}: ok via stooq {sym} (European listing)")
                state.setdefault("stooq_ok", {})[f["ticker"]] = sym
                return c
        print(f"{f['ticker']}: no stooq listing found, using {f['symbol']}")
    global budget_low
    for params in attempts:
        if budget_low:
            return None
        time.sleep(2)  # free plan: 8 requests per minute
        params = {k: v for k, v in params.items() if v}
        params.update({"interval": "1day", "outputsize": 260, "apikey": API})
        j = get("https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(params))
        if j.get("values"):
            print(f"{f['ticker']}: ok via {params.get('exchange') or params.get('mic_code') or 'default'}")
            return [float(v["close"]) for v in j["values"]]
        m = str(j.get("message") or j.get("status"))
        if "for the day" in m or "daily" in m.lower():
            budget_low = True
            print(f"{f['ticker']}: daily API budget exhausted; stopping price lookups for this run")
            return None
        print(f"{f['ticker']}: {m} (tried {params})")
    return None

today = dt.date.today()
evening = dt.datetime.utcnow().hour >= 15   # last run of the day handles calendar reminders
budget_low = False
lines, missing, dd_sum, wsum = [], [], 0.0, 0.0
fund_dd, peaks, intraday = {}, {}, {}
for f in cfg["funds"]:
    closes = series(f)
    if not closes:
        missing.append(f["ticker"]); continue
    last, peak = closes[0], max(closes)
    dd = (last / peak - 1) * 100
    fund_dd[f["ticker"]] = dd
    peaks[f["ticker"]] = peak
    lines.append(f"{f['ticker']} {dd:+.1f}% vs 1y high")
    if cfg.get("intraday", True) and not budget_low:
        time.sleep(2)
        lp = live(f)
        if lp:
            intraday[f["ticker"]] = {"dd": (lp / peak - 1) * 100, "move": (lp / last - 1) * 100, "eq": bool(f.get("equity")), "target": f["target"]}
    if f.get("equity"):
        dd_sum += f["target"] * dd; wsum += f["target"]

if missing:
    msg = ("No price data for: " + ", ".join(missing) +
           ". Check the symbol/exchange in config.json (twelvedata.com/symbol-search) or whether the free plan covers it.")
    print("WARNING:", msg)
    if state.get("setup_warned") != today.strftime("%Y-%m"):
        notify("Ledger: setup problem", msg)
        state["setup_warned"] = today.strftime("%Y-%m")

if wsum:
    eq_dd = dd_sum / wsum
    print("equity drawdown %.1f%%" % eq_dd); print("\n".join(lines))

    if eq_dd > -5 and (state["fired"] or state.get("breach") or state.get("watched")):
        state["fired"] = []; state["breach"] = {}; state["watched"] = []
        notify("Ledger: recovery", "Equities back within 5% of their high. Dip tiers reset.")

    worst = [l for l in lines if "-1" in l or "-2" in l or "-3" in l]
    per_fund = [f["ticker"] for f in cfg["funds"] if f.get("equity")]
    confirm_days = int(cfg.get("confirm_days", 3))
    breach = state.setdefault("breach", {})          # tier -> first date it was breached
    watched = state.setdefault("watched", [])        # tiers already announced as "watching"
    due = []
    for t in cfg["dip_tiers_percent"]:
        key = str(t)
        if eq_dd <= -t:
            if key not in breach:
                breach[key] = today.isoformat()
                if t not in state["fired"] and t not in watched:
                    notify("Ledger: watching a dip",
                           f"Equities {abs(eq_dd):.1f}% below their 1-year high, past the -{t}% tier. "
                           f"No action yet: the fall has to hold for {confirm_days} days before the extra tranche is due. "
                           "Keep the monthly plan running.")
                    watched.append(t)
            else:
                held = (today - dt.date.fromisoformat(breach[key])).days
                if held >= confirm_days and t not in state["fired"]:
                    due.append(t)
        else:
            breach.pop(key, None)
            if t in watched:
                watched.remove(t)
    # individual funds: same three-day confirmation, own tiers
    fbreach = state.setdefault("fund_breach", {})     # "TICKER|tier" -> first date
    ffired = state.setdefault("fund_fired", {})       # ticker -> [tiers already acted on]
    fund_msgs = []
    for f in cfg["funds"]:
        tk = f["ticker"]
        if tk not in fund_dd or not f.get("equity"):
            continue
        d = fund_dd[tk]; done = ffired.setdefault(tk, [])
        if d > -5:
            done.clear()
            for t in cfg["dip_tiers_percent"]:
                fbreach.pop(f"{tk}|{t}", None)
            continue
        for t in cfg["dip_tiers_percent"]:
            k = f"{tk}|{t}"
            if d <= -t:
                if k not in fbreach:
                    fbreach[k] = today.isoformat()
                elif (today - dt.date.fromisoformat(fbreach[k])).days >= confirm_days and t not in done:
                    share = f["target"] / sum(x["target"] for x in cfg["funds"] if x.get("equity"))
                    extra = cfg["extra_tranche_eur"] * share
                    fund_msgs.append(
                        f"{tk} is {abs(d):.1f}% below its 1-year high and has stayed there for {confirm_days} days"
                        + (f"; its share of a tranche is about EUR {extra:.0f}" if cfg["extra_tranche_eur"] else ""))
                    done.append(t)
            else:
                fbreach.pop(k, None)
    # intraday view (live prices, information only)
    if intraday:
        eqs = {k: v for k, v in intraday.items() if v["eq"]}
        wsum2 = sum(v["target"] for v in eqs.values())
        if wsum2:
            live_dd = sum(v["target"] * v["dd"] for v in eqs.values()) / wsum2
            day_move = sum(v["target"] * v["move"] for v in eqs.values()) / wsum2
            crossed = [t for t in cfg["dip_tiers_percent"] if live_dd <= -t]
            seen_today = state.get("intraday_alert") == today.isoformat()
            big_move = day_move <= -float(cfg.get("intraday_move_percent", 3))
            if (crossed or big_move) and not seen_today and not due:
                notify("Ledger: market moving now",
                       f"Live: equities {abs(live_dd):.1f}% below their 1-year high, {day_move:+.1f}% versus yesterday's close"
                       + (f", past the -{max(crossed)}% tier" if crossed else "") +
                       ". This is a live snapshot, not a signal: tranches are confirmed on closing prices held for "
                       f"{confirm_days} days. No action today.")
                state["intraday_alert"] = today.isoformat()

    # daily digest, one message with every fund's distance from its high
    if evening and cfg.get("daily_digest", True) and state.get("last_digest") != today.isoformat():
        body = " · ".join(f"{tk} {fund_dd[tk]:+.1f}%" for tk in fund_dd)
        notify("Ledger: daily fund check",
               f"Distance from 1-year highs: {body}. Portfolio equities {eq_dd:+.1f}%. "
               "Your monthly plan already buys more of whatever has fallen.")
        state["last_digest"] = today.isoformat()

    if fund_msgs and not due:
        notify("Ledger: a fund is deep below its high",
               "; ".join(fund_msgs) + ". The portfolio as a whole has not confirmed a tier, so this is a single-fund move. "
               "Buying more of it is reasonable if you have spare money; your monthly plan already tilts toward whatever has fallen. "
               "Do not sell anything to fund it.")
else:
    print("No equity data this run (API budget or source outage); nothing sent, will retry next run.")

if evening and today.month == cfg["rebalance_month"] and state.get("last_rebalance_alert") != str(today.year):
    msg = "Open the app: Transactions > Rebalance shows the steps."
    L2 = load_ledger()
    if L2:
        try:
            st2, assets2, tx2 = L2["settings"], L2["assets"], L2.get("tx", [])
            band = float(st2.get("band", 5) or 5); hold2 = []
            for a_ in assets2:
                u = sum(t["units"] for t in tx2 if t.get("ticker") == a_["ticker"] and t["type"] == "Buy") - \
                    sum(t["units"] for t in tx2 if t.get("ticker") == a_["ticker"] and t["type"] == "Sell")
                hold2.append((a_["ticker"], float(a_["target"]), u * float(a_.get("price") or 0)))
            inv2 = sum(v for _, _, v in hold2)
            if inv2:
                parts = []
                for tk, tg, v in hold2:
                    w = v / inv2 * 100; d = w - tg
                    if abs(d) > band:
                        delta = tg / 100 * inv2 - v
                        parts.append(f"{tk} {w:.0f}% vs {tg:.0f}%: {'buy' if delta > 0 else 'sell'} about EUR {abs(delta):.0f}")
                msg = ("Out of band: " + "; ".join(parts) + ". Prefer fixing with deposits; otherwise sell the excess first, then buy. Record the trades under Transactions > Rebalance.") if parts else \
                      "Everything is within the band. Nothing to trade; open the app and mark it done."
        except Exception as e:
            print("rebalance plan failed:", e)
    notify("Ledger: annual rebalance", msg)
    state["last_rebalance_alert"] = str(today.year)

key = today.strftime("%Y-%m")
if evening and today.day == cfg["tob_reminder_day"] and state.get("last_tob_alert") != key:
    two_ago = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1) - dt.timedelta(days=1)
    notify("Ledger: TOB declaration",
           f"Declare and pay the TOB for trades made in {two_ago.strftime('%B %Y')} by the end of this month (FPS Finance). Amounts are in the app.")
    state["last_tob_alert"] = key


# ---------- monthly plan from the synced (encrypted) app data ----------
def load_ledger():
    gid, pw = os.environ.get("GIST_ID", ""), os.environ.get("SYNC_PASSPHRASE", "")
    if not gid or not pw:
        print("no GIST_ID / SYNC_PASSPHRASE secrets; skipping monthly plan"); return None
    try:
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        j = get("https://api.github.com/gists/" + gid)
        f = j["files"]["ledger.enc"]
        content = f["content"] if not f.get("truncated") else urllib.request.urlopen(f["raw_url"], timeout=30).read().decode()
        o = json.loads(content)
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(o["salt"]), iterations=150000)
        key = kdf.derive(pw.encode())
        pt = AESGCM(key).decrypt(base64.b64decode(o["iv"]), base64.b64decode(o["data"]), None)
        return json.loads(pt.decode())
    except Exception as e:
        print("could not read ledger:", e); return None

plan_day = cfg.get("plan_day", 1)
if evening and today.day == plan_day and state.get("last_plan_alert") != today.strftime("%Y-%m"):
    L = load_ledger()
    if L:
        st_, assets, tx = L["settings"], L["assets"], L.get("tx", [])
        D = float(st_.get("monthly", 0) or 0); band = float(st_.get("band", 5) or 5)
        tob = float(st_.get("tob", 0.12) or 0) / 100
        hold = []
        for a in assets:
            units = sum(t["units"] for t in tx if t.get("ticker") == a["ticker"] and t["type"] == "Buy") - \
                    sum(t["units"] for t in tx if t.get("ticker") == a["ticker"] and t["type"] == "Sell")
            hold.append({"a": a, "value": units * float(a.get("price") or 0)})
        invested = sum(h["value"] for h in hold)
        for h in hold:
            h["weight"] = (h["value"] / invested * 100) if invested else 0
            h["drift"] = h["weight"] - float(h["a"]["target"])
        periodic = [h for h in hold if (h["a"].get("buyMonths") or "").strip()]
        monthly = [h for h in hold if h not in periodic]
        pshare = sum(float(h["a"]["target"]) for h in periodic) / 100
        Dm = D * (1 - pshare)
        tw = [float(h["a"]["target"]) * min(1.5, max(0.5, 1 - h["drift"] / (2 * band))) for h in monthly]
        tws = sum(tw) or 1
        lines_ = []
        for h, w in zip(monthly, tw):
            amt = Dm * w / tws; base = D * float(h["a"]["target"]) / 100
            tag = "" if abs(amt - base) < 0.5 else (" (up from %.0f)" % base if amt > base else " (down from %.0f)" % base)
            lines_.append(f"{h['a']['ticker']}: EUR {amt:.0f}{tag}")
        for h in periodic:
            months = [int(x) for x in h["a"]["buyMonths"].split(",") if x.strip().isdigit()]
            share = D * float(h["a"]["target"]) / 100
            due = today.month in months
            lines_.append(f"{h['a']['ticker']} ({h['a'].get('broker','other')}): set aside EUR {share:.0f}" + (" - BUY THIS MONTH" if due else ""))
        msg = f"Monthly amount EUR {D:.0f}. Revolut plan: " + "; ".join(lines_) + f". Then log the purchases in the app. TOB on Revolut buys about EUR {Dm*tob:.2f}."
        notify("Ledger: this month's investments", msg)
        state["last_plan_alert"] = today.strftime("%Y-%m")

json.dump(state, open("state.json", "w"), indent=2)
