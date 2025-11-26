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
META_FILE = DOCS_DIR / "meta.json"
CACHE_FILE = DOCS_DIR / "cached.json"
TITLE = os.getenv("GALLERY_TITLE", "Downloads Gallery")
THUMB_DIR = DOCS_DIR / "thumbs"
MAX_BYTES = int(os.getenv("GALLERY_MAX_BYTES", "100000"))


def find_images() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not DOWNLOAD_ROOT.exists():
        return items
    cache = load_cache()
    for path in DOWNLOAD_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMG_EXTS:
            continue
        rel_from_root = path.relative_to(Path("."))
        web_path = "../" + quote(str(rel_from_root).replace("\\", "/"), safe="/")
        folder = str(path.parent.relative_to(DOWNLOAD_ROOT)).replace("\\", "/")
        stat = path.stat()
        cache_key = str(path)
        cached = cache.get(cache_key)
        items.append(
            {
                "src": web_path,  # original path (likely LFS; may 404 on Pages)
                "fs_path": str(path),
                "folder": folder if folder != "." else "",
                "name": path.stem,
                "mtime": stat.st_mtime,
                "cached_thumb": cached["thumb"] if cached and cached.get("mtime") == stat.st_mtime else None,
                "cache_key": cache_key,
            }
        )
    # preserve discovery order (no sorting)
    return items


def build_previews(items: list[dict[str, str]]) -> None:
    try:
        from PIL import Image
    except Exception as e:
        print(f"[gallery] Pillow not available, skipping previews: {e}")
        return

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    for it in items:
        # reuse cached thumbnail if unchanged
        if it.get("cached_thumb"):
            it["thumb"] = it["cached_thumb"]
            continue

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


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(items: list[dict[str, str]]) -> None:
    data = {}
    for it in items:
        if it.get("thumb"):
            data[it["cache_key"]] = {"thumb": it["thumb"], "mtime": it["mtime"]}
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"[gallery] Wrote cache for {len(data)} items -> {CACHE_FILE}")


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
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      grid-auto-rows: 10px;
      grid-auto-flow: dense;
      gap: 12px;
      padding: 0 16px 32px;
    }}
    @media (max-width: 900px) {{
      .grid {{ grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }}
    }}
    @media (max-width: 640px) {{
      .grid {{ grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }}
    }}
    .card {{
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
    .pager {{
      text-align: center;
      padding: 12px 0 20px;
    }}
    .load-more {{
      background: var(--card);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 16px;
      cursor: pointer;
      font-weight: 600;
    }}
    .load-more:disabled {{
      opacity: 0.4;
      cursor: default;
    }}
  </style>
</head>
<body>
  <header>{html.escape(TITLE)}<span class="count">{count} images</span></header>
  <section class="grid" id="grid">
    <p class="empty" id="loading">Loading…</p>
  </section>
  <div class="pager">
    <button class="load-more" id="loadMore" disabled>Load more</button>
  </div>
  <script>
    const PAGE_SIZE = 60;
    let items = [];
    let cursor = 0;
    const gridEl = document.getElementById('grid');

    function setSpan(card, img) {{
      const styles = window.getComputedStyle(gridEl);
      const rowHeight = parseFloat(styles.gridAutoRows) || 10;
      const rowGap = parseFloat(styles.rowGap || styles.gap) || 0;
      const cardWidth = card.getBoundingClientRect().width || 260;
      const ratio = img.naturalHeight && img.naturalWidth ? (img.naturalHeight / img.naturalWidth) : 1;
      const height = cardWidth * ratio;
      const span = Math.max(1, Math.ceil((height + rowGap) / (rowHeight + rowGap)));
      card.style.gridRowEnd = `span ${span}`;
    }}

    function renderMore() {{
      const grid = gridEl;
      if (!items.length) return;
      const end = Math.min(cursor + PAGE_SIZE, items.length);
      const frag = document.createDocumentFragment();
      for (let i = cursor; i < end; i++) {{
        const it = items[i];
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
        img.addEventListener('load', () => setSpan(art, img));
        a.appendChild(img);
        art.appendChild(a);
        frag.appendChild(art);
      }}
      if (cursor === 0) grid.innerHTML = '';
      grid.appendChild(frag);
      cursor = end;
      const btn = document.getElementById('loadMore');
      if (cursor >= items.length) {{
        btn.disabled = true;
        btn.textContent = 'All loaded';
      }} else {{
        btn.disabled = false;
        btn.textContent = 'Load more (' + cursor + '/' + items.length + ')';
      }}
    }}

    async function load() {{
      const grid = document.getElementById('grid');
      const loading = document.getElementById('loading');
      const btn = document.getElementById('loadMore');
      try {{
        const res = await fetch('meta.json?_=' + Date.now());
        const data = await res.json();
        items = data.items || [];
        const countEl = document.querySelector('.count');
        countEl.textContent = items.length + ' images';
        if (loading) loading.remove();
        btn.disabled = false;
        renderMore();
      }} catch (e) {{
        if (loading) loading.textContent = 'Failed to load gallery.';
        console.error(e);
      }}
    }}
    load();

    document.getElementById('loadMore').addEventListener('click', () => {{
      renderMore();
    }});
  </script>
</body>
</html>"""


def main() -> None:
    items = find_images()
    build_previews(items)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    write_meta(items)
    save_cache(items)
    print(f"[gallery] Previews + meta ready for manual index (items: {len(items)})")


if __name__ == "__main__":
    main()
