"""Regression guards for historical Price Research input and metal messaging."""

from pathlib import Path


html = Path("index.html").read_text(encoding="utf-8")

# A missing denomination makes Create Deep Search return early without making
# a request.  Keep the browser parser aligned with the backend taler aliases.
assert "taler|talers|thaler|thalers" in html
assert "mark|taler|thaler|lira" in html

# A verified precious-metal composition without a verified weight must remain
# visible, while fine grams and melt value stay unavailable rather than being
# mislabelled as a common alloy.
assert "source did not provide a coin weight" in html
assert "Common alloy detected — no precious-metal melt value." in html

print("historical denomination and missing-weight metal UI regression checks passed")
