# Two-sided Claude photo recognition

Price Research and Auction Intelligence show two JPEG/PNG upload fields.
ANTHROPIC_API_KEY on the backend enables the endpoint. No Numista image-search
activation is needed. ANTHROPIC_VISION_MODEL optionally overrides the default
claude-haiku-4-5-20251001 model.

Two resized images use one Messages API call. Images and output are not persisted
by this feature. Candidate identities require user confirmation and a year;
the selected text feeds the existing research flow. Image guesses never supply
metal purity, weight, authenticity, or market value. Incomplete/invalid model
responses return explicit errors, not invented identities.

Install either updated requirements file. Limits are 10 calls/minute and 100/day
per worker, reset on restart. Set account-level spending controls in Anthropic
for the overall budget. Requests are not automatically retried.

Verification: python -m unittest test_coin_photo test_resolved_query_handoff -v
Frontend: node --check coin-photo.js
Contract: https://platform.claude.com/docs/en/build-with-claude/vision
