#!/usr/bin/env python3
"""
Build a static gallery page from files under downloads/.

Outputs: docs/index.html
Environment overrides:
  GALLERY_DOWNLOADS   root folder to scan (default: downloads)
  GALLERY_TITLE       page title (default: Downloads Gallery)
"""
from __future__ import annotations

import html
import os
from pathlib import Path
from urllib.parse import quote

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DOWNLOAD_ROOT = Path(os.getenv("GALLERY_DOWNLOADS", "downloads"))
DOCS_DIR = Path("docs")
OUT_FILE = DOCS_DIR / "index.html"
TITLE = os.getenv("GALLERY_TITLE", "Downloads Gallery")


def find_images() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not DOWNLOAD_ROOT.exists():
        return items
    for path in sorted(DOWNLOAD_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMG_EXTS:
            continue
        rel_from_root = path.relative_to(Path("."))
        # docs/ is the site root; use ../ to reach repository root
        web_path = "../" + quote(str(rel_from_root).replace("\\", "/"), safe="/")
        folder = str(path.parent.relative_to(DOWNLOAD_ROOT)).replace("\\", "/")
        items.append(
            {
                "src": web_path,
                "name": path.stem,
                "folder": folder if folder != "." else "",
            }
        )
    return items


def render(items: list[dict[str, str]]) -> str:
    cards = []
    for item in items:
        name = html.escape(item["name"])
        folder = html.escape(item["folder"] or "/")
        src = html.escape(item["src"])
        cards.append(
            f"""
      <article class="card">
        <img loading="lazy" src="{src}" alt="{name}" />
        <div class="meta">
          <div class="folder">{folder}</div>
          <div class="name">{name}</div>
        </div>
      </article>
            """.strip()
        )

    card_html = "\n".join(cards) if cards else '<p class="empty">No images found in downloads/.</p>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(TITLE)}</title>
  <style>
    :root {{
      --bg: #0a0d12;
      --card: #111826;
      --border: #1c2737;
      --text: #e7ecf4;
      --muted: #9fb4ce;
      --accent: #7ed0ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Noto Sans SC", system-ui, -apple-system, sans-serif;
    }}
    header {{
      padding: 16px 20px;
      font-weight: 600;
      font-size: 18px;
      letter-spacing: 0.2px;
    }}
    header .count {{ color: var(--muted); font-weight: 400; margin-left: 8px; }}
    .grid {{
      column-count: 4;
      column-gap: 12px;
      padding: 0 16px 32px;
    }}
    @media (max-width: 1200px) {{ .grid {{ column-count: 3; }} }}
    @media (max-width: 900px) {{ .grid {{ column-count: 2; }} }}
    @media (max-width: 640px) {{ .grid {{ column-count: 1; }} }}
    .card {{
      break-inside: avoid;
      margin-bottom: 12px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(0,0,0,.28);
    }}
    .card img {{
      display: block;
      width: 100%;
      height: auto;
    }}
    .meta {{
      padding: 10px 12px 12px;
      font-size: 12px;
      line-height: 1.4;
      color: var(--muted);
    }}
    .folder {{ color: var(--accent); word-break: break-all; }}
    .name {{ margin-top: 4px; color: var(--text); word-break: break-word; }}
    .empty {{ padding: 18px; color: var(--muted); }}
  </style>
</head>
<body>
  <header>{html.escape(TITLE)}<span class="count">{len(items)} images</span></header>
  <section class="grid">
{card_html}
  </section>
</body>
</html>"""


def main() -> None:
    items = find_images()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    html_out = render(items)
    OUT_FILE.write_text(html_out, encoding="utf-8")
    print(f"[gallery] Wrote {OUT_FILE} with {len(items)} image(s)")


if __name__ == "__main__":
    main()
