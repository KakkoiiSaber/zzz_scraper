#!/usr/bin/env python3
"""
Build a lightweight gallery for GitHub Pages:
 - Scans downloads/ for images (newest first)
 - Generates compressed previews (~100 KB max each) in docs/thumbs/
 - Writes docs/meta.json with preview/original info
 - Writes docs/index.html that fetches meta.json client-side and renders cards

Env overrides:
  GALLERY_DOWNLOADS   root folder to scan (default: downloads)
  GALLERY_TITLE       page title (default: Downloads Gallery)
  GALLERY_MAX_BYTES   max preview size in bytes (default: 100000)
"""
from __future__ import annotations

import html
import io
import json
import os
from pathlib import Path
from urllib.parse import quote

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DOWNLOAD_ROOT = Path(os.getenv("GALLERY_DOWNLOADS", "downloads"))
DOCS_DIR = Path("docs")
OUT_FILE = DOCS_DIR / "index.html"
META_FILE = DOCS_DIR / "meta.json"
TITLE = os.getenv("GALLERY_TITLE", "Downloads Gallery")
THUMB_DIR = DOCS_DIR / "thumbs"
MAX_BYTES = int(os.getenv("GALLERY_MAX_BYTES", "100000"))


def find_images() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not DOWNLOAD_ROOT.exists():
        return items
    for path in DOWNLOAD_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMG_EXTS:
            continue
        rel_from_root = path.relative_to(Path("."))
        web_path = "../" + quote(str(rel_from_root).replace("\\", "/"), safe="/")
        folder = str(path.parent.relative_to(DOWNLOAD_ROOT)).replace("\\", "/")
        stat = path.stat()
        items.append(
            {
                "src": web_path,  # original path (likely LFS; may 404 on Pages)
                "fs_path": str(path),
                "folder": folder if folder != "." else "",
                "name": path.stem,
                "mtime": stat.st_mtime,
            }
        )
    # newest first
    return sorted(items, key=lambda x: x["mtime"], reverse=True)


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
                best_bytes = None
                for q in range(85, 24, -5):
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=q, optimize=True)
                    best_bytes = buf.getvalue()
                    if buf.tell() <= MAX_BYTES:
                        break
                if best_bytes is None:
                    it["thumb"] = None
                    continue
                preview_path.write_bytes(best_bytes)
                it["thumb"] = str(preview_path.relative_to(DOCS_DIR)).replace("\\", "/")
        except Exception as e:
            print(f"[gallery] preview failed for {src_path}: {e}")
            it["thumb"] = None


def write_meta(items: list[dict[str, str]]) -> None:
    payload = []
    for it in items:
        thumb = it.get("thumb") or it["src"]  # fall back to original if no preview
        payload.append(
            {
                "thumb": thumb,
                "full": thumb,  # originals often 404 on Pages; serve preview
                "name": it["name"],
                "folder": it["folder"],
                "mtime": it["mtime"],
            }
        )
    META_FILE.write_text(json.dumps({"items": payload}, ensure_ascii=False), encoding="utf-8")
    print(f"[gallery] Wrote {META_FILE} with {len(payload)} entries")


def render_base(count: int) -> str:
    # minimal shell; cards are injected via meta.json
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
    .empty {{ padding: 18px; color: var(--muted); }}
  </style>
</head>
<body>
  <header>{html.escape(TITLE)}<span class="count">{count} images</span></header>
  <section class="grid" id="grid">
    <p class="empty" id="loading">Loading…</p>
  </section>
  <script>
    async function load() {{
      const grid = document.getElementById('grid');
      const loading = document.getElementById('loading');
      try {{
        const res = await fetch('meta.json?_=' + Date.now());
        const data = await res.json();
        const items = data.items || [];
        const countEl = document.querySelector('.count');
        countEl.textContent = items.length + ' images';
        if (loading) loading.remove();
        const frag = document.createDocumentFragment();
        for (const it of items) {{
          const art = document.createElement('article');
          art.className = 'card';
          const a = document.createElement('a');
          a.href = it.full;
          a.target = '_blank';
          a.rel = 'noopener';
          const img = document.createElement('img');
          img.loading = 'lazy';
          img.src = it.thumb;
          img.alt = '';
          a.appendChild(img);
          art.appendChild(a);
          frag.appendChild(art);
        }}
        grid.innerHTML = '';
        grid.appendChild(frag);
      }} catch (e) {{
        if (loading) loading.textContent = 'Failed to load gallery.';
        console.error(e);
      }}
    }}
    load();
  </script>
</body>
</html>"""


def main() -> None:
    items = find_images()
    build_previews(items)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    write_meta(items)
    OUT_FILE.write_text(render_base(len(items)), encoding="utf-8")
    print(f"[gallery] Wrote {OUT_FILE} with {len(items)} image(s)")


if __name__ == "__main__":
    main()
