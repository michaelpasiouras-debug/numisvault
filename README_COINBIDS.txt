COINBIDS — LOCAL / DEVELOPMENT BUILD
Public domain: https://www.coinbids.eu

FILES
=====
- index.html               (frontend — served by the backend at "/")
- numisvault_backend.py    (Flask backend — filename kept from the NumisVault
                             lineage; the service identifies itself as
                             "CoinBids backend" at runtime and in /health)
- requirements_coinbids.txt
- Procfile                 (Render: gunicorn numisvault_backend:app)

NOTE: earlier drafts of this README referred to "CoinBids_App.html" and
"coinbids_backend.py". The actual deployed filenames are index.html and
numisvault_backend.py — keep this consistent with Render's Auto-Deploy
expectations (see CoinBids_MASTER_HANDOFF_FOR_CLAUDE.txt, "Expected public
filenames").

WHAT CHANGED
============
This build replaces the old NumisVault development branding with CoinBids and fixes the major
data-integrity and valuation issues found during audit.

Core fixes:
- persistent collection/offers are no longer deleted on every reload
- old NumisVault localStorage is migrated automatically
- XLSX export -> import round-trip supports the app's own "My Database" sheet
- all important schema fields survive round-trip
- Price Research history can be re-imported
- duplicate imported IDs are repaired
- PENDING orders can be marked RECEIVED -> OWNED
- Sales select OWNED inventory only
- Wishlist/Identify handoff writes the actual free-text query
- parser no longer turns "2 euro croatia 2025" into variant "o"
- stale asynchronous MA-Shops/Numista responses cannot overwrite a newer search
- Numista uses hard identity validation; wrong denomination/year/currency candidates are rejected
- ambiguous Numista matches are shown as ambiguous rather than randomly selected
- metal calculator is filled only from a validated Numista match; unknown fineness is left blank
- MA-Shops rejects banknotes and obvious sets/rolls/lots for single-coin searches
- denomination matching is semantic (the "2" inside "2025" cannot satisfy "2 euro")
- item price and shipping parsing are separated; denomination in title is not accepted as item price
- shipping=null means unknown; shipping=0 means confirmed free
- only the two cheapest validated MA-Shops coin offers are returned/displayed
- mixed currencies are normalized to EUR when exchange-rate data are available
- Auction Intelligence tab provides a conservative live-bid/sell estimate from validated market data
  plus optional realized auction comparables. It explicitly returns low confidence rather than inventing
  historical auction evidence.

LOCAL RUN
=========
1. Install:
   pip install -r requirements_coinbids.txt
2. Set your Numista API key:
   Windows PowerShell:
     $env:NUMISTA_API_KEY="YOUR_KEY"
   macOS/Linux:
     export NUMISTA_API_KEY="YOUR_KEY"
3. Run:
   python numisvault_backend.py
4. Open http://127.0.0.1:8765/ (the backend now serves index.html itself).
5. Default backend endpoint:
   http://127.0.0.1:8765/api/coin-search
6. Optional: cap MA-Shops/Numista request rate per client (default 30/min):
   COINBIDS_RATE_LIMIT_PER_MIN=30

PRODUCTION / COINBIDS.EU
========================
Do NOT deploy the current localhost URL as-is.

Recommended production layout:
- https://www.coinbids.eu -> the Flask app serves both the frontend and /api/* endpoints.

The frontend automatically uses the same-origin endpoint:
  https://www.coinbids.eu/api/coin-search

This avoids a separate API subdomain and reduces CORS/configuration complexity.

Server environment:
  NUMISTA_API_KEY=<secret>
  COINBIDS_CORS_ORIGINS=https://coinbids.eu,https://www.coinbids.eu
  PORT=<provided by host>

Use HTTPS. Keep API keys only on the backend. Do not embed Numista credentials in HTML.

AUCTION INTELLIGENCE
====================
The current included advisor is deliberately conservative:
- it uses the two validated current MA-Shops offers
- it accepts realized auction comparables supplied by the user
- realized auction data dominate current dealer asks when supplied
- it calculates buyer premium + shipping + fees before BUY/STOP decisions
- it outputs ranges and confidence, never a guaranteed final auction price

Automatic historical-auction provider adapters (Heritage/Stack's Bowers/Sixbid/NumisBids/PCGS)
should only be added after confirming reliable, permitted automated access and exact price semantics
(hammer vs premium-inclusive realized). The app must never fabricate those records.

IMPORTANT
=========
MA-Shops/other websites can change markup. If parsing fails, the correct behavior is "no reliable result",
not a guessed price.

Before public launch at coinbids.eu, run regression tests using known examples such as:
- 2 euro Croatia 2025 -> must never return Croatian 1 Lipa from Numista
- a title containing "2 €" denomination + actual sale price 3.95 -> item price must be 3.95
- banknote cheaper than coins -> banknote must not enter top two
- coin set cheaper than a single coin -> set must not enter top two
- Include shipping vs Exclude shipping must produce different ordering when appropriate
