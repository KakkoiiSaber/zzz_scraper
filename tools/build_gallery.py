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
import io
import os
from pathlib import Path
from urllib.parse import quote

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DOWNLOAD_ROOT = Path(os.getenv("GALLERY_DOWNLOADS", "downloads"))
DOCS_DIR = Path("docs")
OUT_FILE = DOCS_DIR / "index.html"
TITLE = os.getenv("GALLERY_TITLE", "Downloads Gallery")
THUMB_DIR = DOCS_DIR / "thumbs"
MAX_BYTES = int(os.getenv("GALLERY_MAX_BYTES", "100000"))  # compress each preview under this size (bytes)


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
                "fs_path": str(path),
                "mtime": path.stat().st_mtime,
            }
        )
    return items


def build_previews(items: list[dict[str, str]]) -> None:
    try:
        from PIL import Image
    except Exception as e:
        print(f"[gallery] Pillow not available, skipping previews: {e}")
        return

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    for it in items:
        src_path = Path(it["fs_path"])
        rel = src_path.relative_to(DOWNLOAD_ROOT)
        preview_path = THUMB_DIR / rel.with_suffix(".jpg")
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(src_path) as img:
                img = img.convert("RGB")
                # Compress until under MAX_BYTES, lowering quality stepwise
                preview_bytes = None
                for q in range(85, 24, -5):
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=q, optimize=True)
                    if buf.tell() <= MAX_BYTES:
                        preview_bytes = buf.getvalue()
                        break
                    preview_bytes = buf.getvalue()
                if preview_bytes is None:
                    it["thumb"] = None
                    continue
                preview_path.write_bytes(preview_bytes)
                it["thumb"] = str(preview_path.relative_to(DOCS_DIR)).replace("\\", "/")
        except Exception as e:
            print(f"[gallery] preview failed for {src_path}: {e}")
            it["thumb"] = None


def render(items: list[dict[str, str]]) -> str:
    cards = []
    for item in items:
        name = html.escape(item["name"])
        folder = html.escape(item["folder"] or "/")
        thumb = item.get("thumb")
        img_src = html.escape(thumb if thumb else item["src"])
        full_src = html.escape(item["src"])
        cards.append(
            f"""
      <article class="card">
        <a href="{full_src}" target="_blank" rel="noopener">
          <img loading="lazy" src="{img_src}" alt="{name}" />
        </a>
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
    # newest first
    items = sorted(items, key=lambda x: x.get("mtime", 0), reverse=True)
    build_previews(items)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    html_out = render(items)
    OUT_FILE.write_text(html_out, encoding="utf-8")
    print(f"[gallery] Wrote {OUT_FILE} with {len(items)} image(s)")


if __name__ == "__main__":
    main()
