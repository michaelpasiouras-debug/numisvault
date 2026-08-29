from pathlib import Path
import re

PATH = Path("ma_shops_shipping_scraper.py")
text = PATH.read_text(encoding="utf-8")

old = '''                # Weight-only free-shipping tables may contain no monetary token
                # anywhere in the row/header. Since this scraper explicitly
                # requests curr=EUR, preserve EUR instead of emitting null.
                if currency is None and "curr=EUR" in source_url:
                    currency = "EUR"
'''

new = '''                # Weight-only free-shipping tables may contain no monetary token
                # anywhere in the row/header. Fall back to the explicit `curr=`
                # query parameter used for this shipping-page request, rather
                # than emitting a null currency for an otherwise valid zero-cost
                # shipping tier. EUR remains the conservative default if the
                # URL has no usable currency parameter.
                if currency is None:
                    m_curr = re.search(r"(?:[?&])curr=([A-Za-z]{3})(?:&|$)", source_url or "", re.I)
                    currency = m_curr.group(1).upper() if m_curr else "EUR"
'''

if new in text:
    print("ma_shops_shipping_scraper.py: currency fallback already applied")
elif old in text:
    PATH.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("ma_shops_shipping_scraper.py: applied generic free-shipping currency fallback")
else:
    raise SystemExit("ma_shops_shipping_scraper.py: expected shipping fallback block not found; refusing unsafe patch")
