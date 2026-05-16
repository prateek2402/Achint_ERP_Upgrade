"""One-shot helper that extracts every <style>...</style> block from
public/index.html into public/css/styles.css and replaces the inline
blocks with a single <link rel="stylesheet"> tag.

Idempotent: running it on an already-extracted file is a no-op.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

HTML_PATH = Path("public/index.html")
CSS_PATH = Path("public/css/styles.css")
LINK_TAG = '    <link rel="stylesheet" href="/static/css/styles.css">\n'

# Match <style ...>...</style> non-greedily, multiline.
STYLE_BLOCK_RE = re.compile(r"[ \t]*<style[^>]*>([\s\S]*?)</style>\s*\n?", re.IGNORECASE)


def main() -> int:
    if not HTML_PATH.exists():
        print(f"[ERR] {HTML_PATH} missing")
        return 1

    html = HTML_PATH.read_text(encoding="utf-8")
    blocks = STYLE_BLOCK_RE.findall(html)
    if not blocks:
        print("[INFO] no <style> blocks found; nothing to extract")
        return 0

    print(f"[INFO] found {len(blocks)} <style> block(s) totalling {sum(len(b) for b in blocks)} chars")

    # Write CSS file (combine with section markers for traceability).
    parts: list[str] = ["/* Auto-extracted from index.html. Edit here, not inline. */\n"]
    for idx, block in enumerate(blocks, 1):
        parts.append(f"\n/* ---- block {idx} ---- */\n")
        parts.append(block.strip("\n") + "\n")
    CSS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CSS_PATH.write_text("".join(parts), encoding="utf-8")
    print(f"[OK] wrote {CSS_PATH} ({CSS_PATH.stat().st_size} bytes)")

    # Replace ONLY the first occurrence with the link, drop the rest.
    new_html, count = STYLE_BLOCK_RE.subn(lambda _m: "", html)  # remove all
    if LINK_TAG.strip() in new_html:
        print("[INFO] link tag already present; skipping insertion")
    else:
        # Re-insert single link tag right before </head>
        new_html = new_html.replace("</head>", LINK_TAG + "</head>", 1)
    print(f"[INFO] removed {count} inline <style> block(s) from HTML")

    HTML_PATH.write_text(new_html, encoding="utf-8")
    print(f"[OK] updated {HTML_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
