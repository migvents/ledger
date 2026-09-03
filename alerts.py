"""Daily alert check for the Ledger portfolio.
Reads config.json, fetches one year of daily closes per fund from Twelve Data,
computes the target-weighted drawdown of the equity funds from their 1-year peak,
and sends phone notifications through ntfy.sh when a dip tier is crossed,
when the rebalance month starts, and on the monthly TOB reminder day.
State (which tiers have already fired) is kept in state.json and committed back.
"""
import json, os, sys, datetime as dt, urllib.request, urllib.parse

API = os.environ["TWELVE_DATA_KEY"]
TOPIC = os.environ["NTFY_TOPIC"]
cfg = json.load(open("config.json"))
try:
    state = json.load(open("state.json"))
except FileNotFoundError:
    state = {"fired": [], "last_rebalance_alert": "", "last_tob_alert": ""}

def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)

def notify(title, msg, prio="default"):
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=msg.encode(),
        headers={"Title": title, "Priority": prio, "Tags": "chart_with_downwards_trend"})
    urllib.request.urlopen(req, timeout=30)
    print("sent:", title, "|", msg)

today = dt.date.today()
lines, dd_sum, wsum = [], 0.0, 0.0
for f in cfg["funds"]:
    q = urllib.parse.urlencode({"symbol": f["symbol"], "exchange": f["exchange"], "interval": "1day",
                                "outputsize": 260, "apikey": API})
    j = get("https://api.twelvedata.com/time_series?" + q)
    if "values" not in j:
        print("no data for", f["ticker"], j.get("message")); continue
    closes = [float(v["close"]) for v in j["values"]]
    last, peak = closes[0], max(closes)
    dd = (last / peak - 1) * 100
    lines.append(f"{f['ticker']} {last:.2f} ({dd:+.1f}% vs 1y high)")
    if f.get("equity"):
        dd_sum += f["target"] * dd; wsum += f["target"]

eq_dd = dd_sum / wsum if wsum else 0.0
print("equity drawdown %.1f%%" % eq_dd); print("\n".join(lines))

# reset episode once back within 5% of peak
if eq_dd > -5 and state["fired"]:
    state["fired"] = []
    notify("Ledger: recovery", f"Equities back within 5% of their high. Dip tiers reset.")

due = [t for t in cfg["dip_tiers_percent"] if eq_dd <= -t and t not in state["fired"]]
if due:
    extra = cfg["extra_tranche_eur"] * len(due)
    amt = f"€{extra:,.0f}" if extra else "your pre-set tranche (not configured)"
    notify("Ledger: buy-the-dip tier reached",
           f"Equities {abs(eq_dd):.1f}% below their 1-year high. Tiers: {', '.join('-%d%%' % t for t in due)}. "
           f"Invest {amt} extra from spare money outside the buffer, into the funds furthest below target. "
           f"Keep the €{cfg['monthly_eur']} plan running.\n" + "\n".join(lines), "high")
    state["fired"] += due

# rebalance month (first day)
if today.month == cfg["rebalance_month"] and state.get("last_rebalance_alert") != str(today.year):
    notify("Ledger: annual rebalance check", "Open the app, check drift against the 5-point band, trade only what is outside it, mark it done.")
    state["last_rebalance_alert"] = str(today.year)

# TOB reminder
key = today.strftime("%Y-%m")
if today.day == cfg["tob_reminder_day"] and state.get("last_tob_alert") != key:
    two_ago = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1) - dt.timedelta(days=1)
    notify("Ledger: TOB declaration", f"Declare and pay the TOB for trades made in {two_ago.strftime('%B %Y')} by the end of this month (FPS Finance). Amounts are in the app.")
    state["last_tob_alert"] = key

json.dump(state, open("state.json", "w"), indent=2)
