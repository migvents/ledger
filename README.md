# Ledger: live prices on the web, alerts on your phone

You need three free things: a GitHub account (github.com), a Twelve Data API key (twelvedata.com → Get free API key), and the ntfy app on your phone (App Store / Play Store, "ntfy").

## Part 1: publish the app (10 minutes)
1. GitHub → "+" → New repository. Name `ledger`, **Public** (needed for free GitHub Pages; the page holds no personal data, your numbers stay in your phone's browser). Tick "Add a README". Create.
2. "Add file" → "Upload files". Upload **all** the files in this folder: `index.html`, `config.json`, `alerts.py`, and the folder `.github/workflows/alerts.yml` (drag the whole `.github` folder). Commit.
3. Settings → Pages → Source "Deploy from a branch", branch `main`, folder `/ (root)` → Save. After a minute your address appears: `https://<username>.github.io/ledger/`
4. Open it on your phone → Settings tab → paste the Twelve Data key → Save rules. Safari: Share → Add to Home Screen.

## Part 2: alerts (5 minutes)
1. Open the ntfy app → "+" → subscribe to a topic. Choose a name nobody would guess, e.g. `miguel-ledger-7q2x9`. This name is your only password: keep it private.
2. GitHub repository → Settings → Secrets and variables → Actions → New repository secret:
   - `TWELVE_DATA_KEY` = your Twelve Data key
   - `NTFY_TOPIC` = the topic name from step 1
3. Repository → Actions tab → "Ledger alerts" → "Run workflow" once to test. You should get a notification within a minute (a test run sends nothing if no rule fires; check the run log to see the drawdown it computed).
4. From then on it runs every weekday after the Xetra close and notifies you when:
   - equities fall past a dip tier (15%, 25%, 35% below their 1-year high): tells you the extra tranche to invest
   - equities recover to within 5% of the high: tiers reset
   - your rebalance month begins (January by default)
   - the 20th of each month: TOB declaration reminder

## Settings you may change (edit `config.json` on GitHub, pencil icon)
- `extra_tranche_eur`: the extra amount per dip tier. Set it now, while calm. 0 means the alert will remind you but not name an amount.
- `dip_tiers_percent`, `monthly_eur`, `rebalance_month`, `tob_reminder_day`.
- Fund symbols: if a fund shows "no data", check its Twelve Data symbol at twelvedata.com/symbol-search and edit here and in the app's Settings.

## Notes
- The app's data lives in your phone's browser. Use "Copy backup" in the app's Settings now and then.
- The alert script only sees prices, not your holdings, so drift and rebalance amounts are shown in the app, not in notifications.
- Free Twelve Data plan: 800 requests/day. The daily alert uses 5; the app uses 5 per refresh.
- To update the app later, upload a new `index.html` over the old one.
