# CoinBids Auction Intelligence 3.0 — Source Feasibility Matrix

Researched 2026-08-15 via live web search of each provider's own published
Terms of Service / Terms of Use. This is NOT an assumption-based table —
each status below is backed by an actual quoted clause or an explicit
absence-of-permission finding.

| Source | Automated access status | Evidence | Notes |
|---|---|---|---|
| **CoinArchives** (coinarchives.com / pro.coinarchives.com) | **NOT_SUPPORTED** (automatic) / **REQUIRES_LICENSE** for full depth | ToS (pro.coinarchives.com/terms.php): *"Information accessed from the Service may not be stored or harvested using automated means. CoinArchives, LLC monitors access and has put into place safeguards to prevent automated harvesting of data."* | Free archive covers recent results only; full historical depth is a paid subscription product. CoinArchives' own FAQ states "price realized" values are **hammer prices**, excluding buyer's fee — confirms the spec's assumption in §4/§16. No automated adapter may be built against this source without a separate, explicit licensing agreement with CoinArchives, LLC. |
| **acsearch.info** | **NOT_SUPPORTED** | ToS (acsearch.info/terms.html), §2.1: *"the use of so called web scrapers, web robots and other software for the systematic collection of data and content...is forbidden to the highest degree."* | Explicit, unambiguous prohibition. Price/realized data is additionally gated behind premium access. No adapter may be built. |
| **Biddr** (biddr.com) | **UNDER_REVIEW** | Public Terms of Use reviewed; no explicit anti-scraping clause was found in the retrieved terms text, but no public API or bulk-data license is documented either. Absence of an explicit prohibition is **not** the same as affirmative permission. | Biddr is a live, transactional bidding platform (real bids/payments flow through it), which raises the stakes of any automated interaction well above passive data reading. Do not build an automated adapter until CoinBids has directly contacted Biddr and obtained explicit written permission (and, ideally, an API) for the intended read-only use. Mark disabled/manual-only until then. |
| **NumisBids** (numisbids.com) | **UNDER_REVIEW** | ToS (numisbids.com/terms): *"All externally-generated source material available through the Service is used by permission."* — meaning NumisBids itself redistributes individual auction houses' data under its own bilateral permission agreements. No public API or bulk-access license is documented for third parties. | The "used by permission" language implies NumisBids' own content rights are third-party-licensed, which if anything makes it *less* likely that further automated re-harvesting by an unrelated third party (CoinBids) is authorized. Requires direct outreach to NumisBids for explicit permission before any automated adapter is built. Mark disabled/manual-only until then. |
| **Individual auction houses** (Künker, Heritage, CNG, Leu, Nomos, etc.) | **UNDER_REVIEW / case-by-case** | Not reviewed individually — dozens of distinct operators with distinct ToS, several already list on NumisBids/Biddr/CoinArchives (which raises its own re-licensing questions), some offer public APIs (e.g. some larger US houses), most do not. | Default to **NOT_SUPPORTED** for any specific house until that house's own ToS/API terms are individually reviewed and, where required, explicit permission or a licensed API key is obtained. No blanket adapter. |
| **Manual user-entered comparables** | **ENABLED_MANUAL** | N/A — user supplies data they already legitimately obtained themselves (e.g. read off an auction house's public results page in their own browser). No automation, no ToS conflict. | Already the only realized-comparable input CoinBids has today. Auction Intelligence 3.0's structured Manual/CSV adapter (Phase 3, built in this pass) upgrades this from a bare price-per-line textarea to a structured record (date/hammer/currency/grade/house/URL) without removing the legacy one-number-per-line mode. |
| **CSV/XLSX import adapter** | **ENABLED_MANUAL** | N/A — same reasoning as manual entry; the person exports/prepares their own file. | Built in this pass (`CSVComparableAdapter`). |

## Bottom line for this implementation pass

**Zero external auction-result scraping adapters are enabled.** The two
sources the spec explicitly named as likely candidates (CoinArchives,
acsearch) both have unambiguous contractual prohibitions on automated
harvesting in their own published Terms of Service, discovered via live
search of those exact documents — not assumed. Biddr and NumisBids lack an
explicit prohibition but also lack any documented public API or bulk-access
permission, so per the spec's own instruction ("ΜΗ υποθέσεις ότι επιτρέπεται
scraping"), they are marked UNDER_REVIEW rather than enabled.

This means, honestly: **Auction Intelligence 3.0's "Realized Auction
Comparable Engine" can only be populated by the Manual and CSV adapters in
this delivery** — real automated realized-sale ingestion is blocked on
business development (obtaining actual licensed/API access from a provider),
not on engineering. The statistics/valuation engine underneath is built to
be source-agnostic, so a licensed adapter can be plugged in later via the
same `AuctionSourceAdapter` interface without changing the valuation logic.
