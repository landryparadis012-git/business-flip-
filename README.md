# 🤝 Flip Broker

> Finds underpriced listings on Facebook Marketplace, checks them against real
> comps, and sends cards to its OWN Telegram bot with buttons: TAKEN / CONTACTED
> / POSTED. You never buy inventory — you source deals: lock the seller, advertise,
> find the buyer, collect the spread or fee. Runs free on GitHub Actions.
> OpsHub watches every status change and manages your reminders.

## The model

1. Engine scans Marketplace daily for your watches
2. New underpriced listing → 🔥 card with:
   - listing facts + COMP-BASED math (never LLM-guessed):
     MAX SELLER OFFER · SUGGESTED LIST PRICE · EST. SPREAD
   - seller probe draft (availability + permission, never auto-sent)
3. You press buttons (next scan records them):
   ✅ TAKEN → 📞 CONTACTED SELLER → 📤 POSTED → /won <price> or /lost
4. Every status change → signed event to OpsHub →
   hub registers the deal + auto-sets follow-up reminders (48h after CONTACTED)
5. Buyer commits via written sourcing/fee agreement — collect the spread/fee

## Buttons — what triggers what

| You press | Engine does | OpsHub does |
|---|---|---|
| ✅ TAKEN | locks deal as pursued | registers deal, next-action set |
| 📞 CONTACTED | waits on seller | **48h follow-up reminder** auto-set |
| 📤 POSTED | waits on buyer lead | follow-up reminder on your listing |
| `/won <price>` or `/lost` | closes deal | outcome logged → flips P&L in digest |

Buttons register on the next 15-min poll. The persisted Telegram update offset
is sent to Telegram so acknowledged updates are not repeatedly returned.
Discovery scan runs once/day inside those runs (Apify result-request guard —
800 results/month, warn at 80%). The request size is clamped to the remaining
monthly allowance; failed scans leave the daily gate open for retry. This is a
result-count guard, not a dollar/CU spend cap: set an Apify account spending
limit in the Apify console because actor pricing varies by actor and usage.

## Files

| File | Purpose |
|---|---|
| `flip_broker.py` | The entire engine (stdlib only) |
| `watches.json` | Your hunts: term, FB search URL, max price, result cap |
| `comps.json` | Real sold/asking prices per watch — YOU keep fresh |
| `data/state.json` | Seen listings, deal statuses, Telegram offset, usage counter |
| `.github/workflows/flip.yml` | 15-min poll runs; discovery gated to once daily |

## Setup from zero

1. Create repo, add all files
2. **@BotFather → /newbot → copy token** (FlipBrokerBot, separate from OpsHub bot)
3. Send the new bot any message → `getUpdates` → your chat ID
   (can be the same as your OpsHub chat ID)
4. Secrets (Settings → Secrets and variables → Actions):
   - `FLIP_TG_TOKEN` — the FlipBrokerBot token
   - `TELEGRAM_CHAT_ID` — your chat id (numeric)
   - `APIFY_TOKEN` — apify.com → Settings → API & Integrations
   - `HUB_URL` — OpsHub `.convex.site` URL
   - `HUB_FETCH_URL` — `https://<your-convex-deployment>.convex.site/flipwatches?token=<TELEGRAM_WEBHOOK_SECRET>`; the query token must match OpsHub's Convex `TELEGRAM_WEBHOOK_SECRET` (not `HUB_WEBHOOK_SECRET`). A 403 means the URL/token pair is wrong; omit the secret only if you intentionally want local `watches.json` fallback.
   - `HUB_WEBHOOK_SECRET` — same secret as OpsHub
5. Edit `watches.json` → Actions → Flip Broker → Run workflow (manual first)

## Budget rules (Apify free = $5/month credit)

- Hard cap 800 results/month, split across watches (~26/day)
- Also set a spending limit in Apify Settings → Billing
- One discovery run/day until the data proves more is worth it

## Comps are the whole system

Garbage comps = fake spreads. 3–5 completed-sale prices per watch in `comps.json`,
refresh monthly. Deal scoring ignores sources not explicitly tagged with a `_sold`
suffix; retail and asking prices are not proof of realized sale value. Expired
comps are intentionally rejected; do not extend dates or invent prices to force
cards. Match model, storage, condition and included accessories. Every card
shows the sold-comp source + date. MAX OFFER =
lowest comp − 10% costs − target fee − buffer (constants in flip_broker.py).

## Optional eBay market-data integration

The optional eBay Browse API connector enriches deal cards with active fixed-price
asking context only. It requires an eBay developer application and these optional
GitHub Actions secrets:

- `EBAY_CLIENT_ID` — eBay Application ID (App ID)
- `EBAY_CLIENT_SECRET` — eBay Certificate ID (Cert ID)
- `EBAY_MARKETPLACE_ID` — optional; defaults to `EBAY_US`
- `EBAY_CURRENCY` — optional; defaults to `USD`

The broker fetches a small active fixed-price sample once per watch scan and
shows its minimum/median only as market context. It does not change deal tiers,
maximum offers or spread calculations. If credentials are absent or the API
fails, Facebook discovery and sold-comp scoring continue unchanged.

Browse results must be labelled **asking prices** and must never enter sold-price
scoring. eBay Marketplace Insights can provide completed-sale history, but it is
a limited-release API and must not be assumed available until eBay approves the
application. Until then, keep verified completed-sale records in `comps.json`.

## Warnings

- Sellers must consent before you advertise their listing/photos
- Cars/houses may need licensing in some states — start with ordinary goods
- Spread is an ESTIMATE until a buyer commits in writing

## Troubleshooting

| Symptom | Fix |
|---|---|
| Button press not registering | Next poll is ≤15 min — check the Actions log |
| No card but run green | No new underpriced listings — or cap hit (log says) |
| 401 from Telegram | Wrong `FLIP_TG_TOKEN` |
| 400 from Telegram | `TELEGRAM_CHAT_ID` must be numeric |
| Apify 4xx | Wrong `APIFY_TOKEN`, or actor name changed |
| Everything PASS | comps.json spelling doesn't match listing titles |
