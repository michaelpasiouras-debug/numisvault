from pathlib import Path
import importlib.util

spec = importlib.util.spec_from_file_location('numisvault_backend', 'numisvault_backend.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

cases = [
    ('<html><body>We are verifying that you are a human</body></html>', 'https://www.ma-shops.com/_creoline/checkpoint?return=x'),
    ('<html><body><div>Creoline checkpoint</div></body></html>', 'https://www.ma-shops.com/shops/search.php?searchstr=x'),
    ('<html><body>Checking your browser before accessing MA-Shops</body></html>', 'https://www.ma-shops.com/shops/search.php?searchstr=x'),
]
for html, url in cases:
    assert mod._is_mashops_checkpoint_html(html, url), (html, url)

normal = '<html><body><a href="item.php?id=123">Greece 10 Euro 2022 Antikythera Mechanism</a></body></html>'
assert not mod._is_mashops_checkpoint_html(normal, 'https://www.ma-shops.com/shops/search.php?searchstr=x')

src = Path('numisvault_backend.py').read_text(encoding='utf-8')
assert 'MA-Shops human-verification checkpoint blocked automated search' in src
assert '_is_mashops_checkpoint_html(r.text, r.url)' in src
assert src.count('human-verification/WAF checkpoint detected at') == 1

ui = Path('index.html').read_text(encoding='utf-8')
assert "MA-Shops temporarily unavailable" in ui
assert 'source-availability problem' in ui

print('MA-Shops checkpoint detection regression: PASS')
