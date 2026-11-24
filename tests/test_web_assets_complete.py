"""The bundled web UI must contain every asset its index.html reaches.

Old hashed assets are pruned with scripts/prune_web_assets.py. This catches a
prune (or a bad bundle commit) that removes a file the current build still
loads, directly or as a lazy chunk, font or image of another asset.
"""

import re
from pathlib import Path

HTML = Path(__file__).resolve().parent.parent / "repeater" / "web" / "html"
# "/assets/x.js", "assets/x.js" (Vite's dependency map) and "./x.js" (chunk imports).
REF = re.compile(r"""(?:/?assets/|["'(]\./)([A-Za-z0-9_.-]+\.[A-Za-z0-9]+)""")
TEXT = {".js", ".mjs", ".css", ".html", ".json", ".svg"}


def test_index_reaches_only_existing_assets():
    assets = HTML / "assets"
    missing, seen = set(), set()
    queue = [HTML / "index.html"]
    while queue:
        path = queue.pop()
        for name in REF.findall(path.read_text(errors="ignore")):
            if name in seen:
                continue
            seen.add(name)
            target = assets / name
            if not target.is_file():
                missing.add(f"{name} (referenced by {path.name})")
            elif target.suffix in TEXT:
                queue.append(target)
    assert seen, "index.html references no assets"
    assert not missing, sorted(missing)
