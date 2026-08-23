COINBIDS EMERGENCY RECOVERY PACK — 2026-08-23

UPLOAD THE FILES IN THIS FOLDER TO THE REPO USING THESE EXACT NAMES.

CRITICAL RESTORED BEHAVIOR
- coinbids.eu/ = PUBLIC homepage, always.
- coinbids.eu/app = authenticated application/login.
- Public homepage no longer auto-redirects to /app just because localStorage
  contains a valid Supabase session.
- Standalone Identify Coin is retired from app and public navigation.
- /identify-coin redirects to /coin-value.
- App keeps Dashboard -> Price Research -> Auction Intelligence -> remaining options.
- Recent Activity remains split into In Collection / Pending / Wishlist.
- Auction buyer premium remains 9%.
- Login logo is embedded directly.
- Google OAuth is wired immediately after Supabase creation, so an unrelated
  app-startup error cannot leave the visible Google button inert.
- Existing initializeAuth() and normal startup order are preserved.
- Latest cron-observability backend remains the base.

UPLOAD:
index.html
public_home.html
public.css
numisvault_backend.py
coin-value.html
auction-intelligence.html
metal-value.html
404.html
robots.txt
sitemap.xml

AFTER RENDER IS LIVE TEST IN THIS ORDER:
1. Incognito -> https://www.coinbids.eu/
   Must show PUBLIC homepage.
2. https://www.coinbids.eu/app
   Must show login with CoinBids logo.
3. Click Continue with Google.
   Must navigate to OAuth, or show a visible error message.
4. Manually return to https://www.coinbids.eu/
   Must stay PUBLIC (no forced /app redirect).
5. In app verify nav and Recent Activity.
6. Run Price Research from free text; Country must remain resolved output, not a required input.
7. Open /identify-coin; it should redirect to /coin-value.

VALIDATION DONE:
- index JavaScript syntax PASS
- backend Python compile PASS
- route split PASS
- Identify removal PASS
- Recent Activity PASS
- Auction 9% PASS
