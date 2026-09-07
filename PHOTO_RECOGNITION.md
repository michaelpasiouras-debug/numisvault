Two-sided photo recognition
===========================

Price Research and Auction Intelligence accept two JPEG/PNG photos. The browser
resizes each to at most 1000 pixels and sends both in one Numista image-search
request. Candidates require user confirmation and a year before entering the
existing text research flow. A visual match does not certify authenticity,
fineness, weight, or an exact issue. Existing catalogue lookup supplies specs.

Activation: Numista image search requires paid endpoint access. After the owner
has enabled that access, set NUMISTA_IMAGE_SEARCH_ENABLED=true on Render and
retain the existing server-only NUMISTA_API_KEY. Install the updated requirements.
Without activation the endpoint returns an explicit unavailable message. No paid
plan is activated by this code. Do not enable publicly without appropriate
account-level usage/budget controls; the semaphore bounds concurrent image calls
within one worker, not total daily usage or traffic across workers.

Photos, candidates and Numista fields are not written to database/files/browser
storage by this feature. HTTP responses use no-store. Photo metadata is stripped
before forwarding. No automatic retries of potentially chargeable requests.

Contract: https://en.numista.com/api/doc/swagger.yaml?v=3.36
Pricing: https://en.numista.com/api/pricing.php
Tests: python -m unittest test_coin_photo -v
