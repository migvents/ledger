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

def series(f):
    """Try the symbol several ways; return list of closes (newest first) or None."""
    attempts = [
        {"symbol": f["symbol"], "exchange": f.get("exchange", "")},
        {"symbol": f["symbol"]},
    ]
    for params in attempts:
        time.sleep(2)  # free plan: 8 requests per minute
        params = {k: v for k, v in params.items() if v}
        params.update({"interval": "1day", "outputsize": 260, "apikey": API})
        j = get("https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(params))
        if j.get("values"):
            print(f"{f['ticker']}: ok via {params.get('exchange') or params.get('mic_code') or 'default'}")
            return [float(v["close"]) for v in j["values"]]
        print(f"{f['ticker']}: {j.get('message') or j.get('status')} (tried {params})")
    return None

today = dt.date.today()
lines, missing, dd_sum, wsum = [], [], 0.0, 0.0
for f in cfg["funds"]:
    closes = series(f)
    if not closes:
        missing.append(f["ticker"]); continue
    last, peak = closes[0], max(closes)
    dd = (last / peak - 1) * 100
    lines.append(f"{f['ticker']} (via {f['symbol']}) {dd:+.1f}% vs 1y high")
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

    if eq_dd > -5 and state["fired"]:
        state["fired"] = []
        notify("Ledger: recovery", "Equities back within 5% of their high. Dip tiers reset.")

    worst = [l for l in lines if "-1" in l or "-2" in l or "-3" in l]
    per_fund = [f["ticker"] for f in cfg["funds"] if f.get("equity")]
    due = [t for t in cfg["dip_tiers_percent"] if eq_dd <= -t and t not in state["fired"]]
    # individual funds far below their own high, even when the portfolio is not
    solo = []
    for ln in lines:
        try:
            pct = float(ln.split("(")[1].split("%")[0])
            if pct <= -cfg["dip_tiers_percent"][0]:
                solo.append(ln.split(" (")[0].split(" (via")[0] + f" {pct:.0f}%")
        except Exception:
            pass
    if solo and not due and state.get("last_solo_alert") != today.strftime("%Y-%m"):
        notify("Ledger: a fund is well below its high",
               "Below their 1-year high: " + "; ".join(solo) +
               ". The portfolio as a whole has not crossed a dip tier, so no extra tranche is due; "
               "your monthly plan already buys more of what has fallen. Act only if the app shows it outside the rebalance band.")
        state["last_solo_alert"] = today.strftime("%Y-%m")
    if due:
        extra = cfg["extra_tranche_eur"] * len(due)
        amt = f"EUR {extra:,.0f}" if extra else "your pre-set tranche (not configured)"
        notify("Ledger: buy-the-dip tier reached",
               f"Equities {abs(eq_dd):.1f}% below their 1-year high. Tiers: {', '.join('-%d%%' % t for t in due)}. "
               f"Invest {amt} extra from spare money outside the buffer, into the funds furthest below target. "
               f"Keep the EUR {cfg['monthly_eur']} plan running.\n" + "\n".join(lines), "high")
        state["fired"] += due
else:
    print("No equity data at all; skipping dip check.")

if today.month == cfg["rebalance_month"] and state.get("last_rebalance_alert") != str(today.year):
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
if today.day == cfg["tob_reminder_day"] and state.get("last_tob_alert") != key:
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
if today.day == plan_day and state.get("last_plan_alert") != today.strftime("%Y-%m"):
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
