"""
MinasDownloader.py — ZIP-first downloader for Seafile/Minas shares.

What it does (batch mode):
- Read metafiles/米游社-官方资讯-minas.csv
- Columns used: post_name, minas_link, minas_pwd
- For each row:
    - If ./downloads/米游社-官方资讯-minas/<post_name> exists -> SKIP (no network)
    - Else: open share, click ZIP, print progress, download, unzip to <post_name>, delete .zip

Quick start:
  pip install playwright requests bs4
  python -m playwright install chromium

Examples:
  # Single share (zip root)
  python src/downloader/MinasDownloader.py \
    --url "https://minas.mihoyo.com/d/97936e3e62a949b2930d/" \
    --password "zzzbz2025" \
    --out ./downloads \
    --zip --path "/"

  # Batch from CSV (skips already-downloaded posts)
  python src/downloader/MinasDownloader.py \
    --out ./downloads \
    --batch \
    --csv "metafiles/米游社-官方资讯-minas.csv"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse
import urllib.parse

import requests
from bs4 import BeautifulSoup

# --- Defaults (override via CLI or env) ---
DEFAULT_URL = os.getenv("MINAS_URL", "https://minas.mihoyo.com/d/97936e3e62a949b2930d/")
DEFAULT_PASSWORD = os.getenv("MINAS_PASSWORD", "zzzbz2025")
DEFAULT_OUTDIR = os.getenv("MINAS_OUTDIR", "./downloads")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

API_BASE = "https://minas.mihoyo.com"
ROW_TIMEOUT_SEC = int(os.getenv("MINAS_ROW_TIMEOUT", "420"))  # per-row timeout to avoid getting stuck
MAX_ROWS = os.getenv("MINAS_MAX_ROWS")  # optional limit for CI/debug

# --------------------- Utilities ---------------------

def sanitize_filename(name: str) -> str:
    # return re.sub(r"[\\/:*?\"<>|]", "_", name or "").strip() or "untitled"
    return re.sub(r"[\\/:*?\"]", "_", name or "").strip() or "untitled"


def _norm_cell(val: str | None) -> str:
    """Normalize CSV cells: strip whitespace and stray quotes (even unmatched)."""
    v = (str(val) if val is not None else "").strip()
    # Strip matching outer quotes if present
    if (len(v) >= 2) and ((v[0] == v[-1]) and v[0] in ('"', "'")):
        v = v[1:-1].strip()
    # Also defensively strip stray quotes on either end
    v = v.strip('\"\'')
    return v

# Only strip a single pair of matching outer quotes (either " or ')
def _norm_outer_quotes(val: str | None) -> str:
    v = (str(val) if val is not None else "").strip()
    if (len(v) >= 2) and ((v[0] == v[-1]) and v[0] in ('"', "'")):
        v = v[1:-1].strip()
    return v

# --------------------- Static path (requests) ---------------------

def authenticate_with_password(session: requests.Session, url: str, password: str) -> requests.Response:
    headers = {"User-Agent": USER_AGENT, "Referer": url}
    r = session.get(url, headers=headers, allow_redirects=True, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    pwd_input = soup.find("input", {"type": "password"})
    if not pwd_input:
        return r  # already accessible

    form = pwd_input.find_parent("form")
    if not form:
        raise RuntimeError("Password form not found in static HTML (site likely JS-driven).")

    action = form.get("action") or url
    method = (form.get("method") or "post").lower()

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        if inp.get("type") == "password":
            payload[name] = password
        else:
            payload[name] = inp.get("value") or ""

    if method == "post":
        auth = session.post(action, data=payload, headers=headers, allow_redirects=True, timeout=30)
    else:
        auth = session.get(action, params=payload, headers=headers, allow_redirects=True, timeout=30)

    auth.raise_for_status()
    if BeautifulSoup(auth.text, "html.parser").find("input", {"type": "password"}):
        raise RuntimeError("Static auth failed — password field still present.")
    return auth

# --------------------- ZIP via browser (Playwright) ---------------------

async def _zip_current_path(
    url: str,
    password: str,
    path: str,
    out_dir: Path,
    force_extract_dir: Path | None = None
) -> Path:
    """
    Open {url} with ?p={path}, click the green ZIP button, and save the archive.
    While zipping, print progress if the site emits /api/v2.1/query-zip-progress/?token=...
    Then unzip to force_extract_dir (if provided) or out_dir, flatten if needed, and delete the .zip.
    """
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    # Normalize '/.../'
    if not path.startswith('/'):
        path = '/' + path
    if not path.endswith('/'):
        path = path + '/'

    print(f"Zipping {path}…")
    q = urllib.parse.urlencode({"p": path, "mode": "list"})
    target = f"{url.rstrip('/')}/?{q}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT, accept_downloads=True)
        page = await context.new_page()
        await page.goto(target, wait_until="domcontentloaded")
        print(f"  page: {page.url}")

        # Password gate if present (tolerant + patient)
        pwd = await page.query_selector('input[type="password"]')
        if pwd:
            await pwd.fill(password)
            try:
                await pwd.press("Enter")
            except Exception:
                pass
            for sel in ('button[type="submit"]', 'button.btn-success', 'button.btn-primary',
                        'input[type="submit"]', '.modal-footer .btn-primary'):
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        break
                except Exception:
                    pass
            try:
                await page.wait_for_selector('.shared-dir-view-main, h2.h3.text-truncate', timeout=20000)
            except Exception:
                await page.wait_for_timeout(2000)

        # Ensure the main content is mounted
        try:
            await page.wait_for_selector('.shared-dir-view-main', timeout=30000)
        except Exception:
            await page.wait_for_timeout(2000)

        # Hook: capture progress token from any response
        progress_token_holder = {"token": None}
        async def _extract_token(u: str) -> str | None:
            try:
                return await page.evaluate(
                    "(u)=>{ try{ const x=new URL(u); return x.searchParams.get('token'); }catch{ return null; } }",
                    u
                )
            except Exception:
                return None

        def _on_response(resp):
            try:
                u = resp.url
                if ("/api/v2.1/query-zip-progress/" in u) and ("token=" in u) and resp.request.method.upper() == "GET":
                    # schedule async extraction
                    async def _grab():
                        tok = await _extract_token(u)
                        if tok and not progress_token_holder["token"]:
                            progress_token_holder["token"] = tok
                    asyncio.create_task(_grab())
            except Exception:
                pass

        page.on("response", _on_response)

        # Locate ZIP button (class is stable; text may be localized)
        zip_btn = page.locator('button.shared-dir-op-btn')
        try:
            await zip_btn.first.wait_for(state='visible', timeout=30000)
            zip_btn = zip_btn.first
        except Exception:
            await page.wait_for_timeout(1000)
            await page.reload(wait_until='domcontentloaded')
            await page.wait_for_selector('.shared-dir-view-main', timeout=15000)
            await page.wait_for_timeout(500)
            zip_btn = page.locator('button.shared-dir-op-btn').first
            await zip_btn.wait_for(state='visible', timeout=15000)

        # Start listening BEFORE clicking
        download_promise = page.wait_for_event("download")
        await page.wait_for_timeout(300)  # let handlers bind
        await zip_btn.click()

        # Try to get the token for up to ~30s
        progress_token = None
        waited = 0
        while not progress_token and waited < 30000:
            if progress_token_holder["token"]:
                progress_token = progress_token_holder["token"]
                print(f"  progress token: {progress_token}")
                break
            await page.wait_for_timeout(250)
            waited += 250

        # Poll progress if we have the token
        poll_task = None
        if progress_token:
            last_pct = -1
            progress_url = f"{API_BASE}/api/v2.1/query-zip-progress/?token={progress_token}"
            # Poll until 100% or download finishes
            async def _poll():
                nonlocal last_pct
                try:
                    while True:
                        if page.is_closed():
                            return
                        try:
                            got = await page.evaluate(
                                "(u)=>fetch(u,{credentials:'same-origin'}).then(r=>r.ok?r.json():null)", progress_url
                            )
                        except Exception:
                            return  # page/context likely closed; exit quietly
                        if not got:
                            try:
                                await page.wait_for_timeout(700)
                            except Exception:
                                return
                            continue
                        zipped = int(got.get("zipped", got.get("done", 0)) or 0)
                        total = int(got.get("total", got.get("count", 0)) or 0)
                        failed = int(got.get("failed", 0) or 0)
                        canceled = int(got.get("canceled", 0) or 0)
                        reason = got.get("failed_reason") or got.get("error") or ""
                        pct = int((zipped * 100) / total) if total else 0
                        if pct != last_pct:
                            print(f"  progress: {zipped}/{total} ({pct}%)")
                            last_pct = pct
                        if canceled:
                            print("  progress: canceled by server"); return
                        elif failed:
                            print(f"  progress: failed ({reason})"); return
                        elif total and zipped >= total:
                            print("  progress: 100% (zipped) — waiting for download…"); return
                        else:
                            try:
                                await page.wait_for_timeout(700)
                            except Exception:
                                return
                except Exception:
                    return
            # fire-and-forget poller; we’ll still await the actual download
            poll_task = asyncio.create_task(_poll())
        else:
            print("  progress: no token captured; proceeding without live progress…")

        # Await the browser download
        download = await download_promise

        # Prefer suggested filename; fall back to dirName.zip
        suggested = download.suggested_filename
        if suggested and ((suggested.startswith('"') and suggested.endswith('"')) or (suggested.startswith("'") and suggested.endswith("'"))):
            suggested = suggested[1:-1]
        if not suggested:
            try:
                dir_name = await page.evaluate("window.shared && window.shared.pageOptions && window.shared.pageOptions.dirName || ''")
            except Exception:
                dir_name = ""
            suggested = f"{dir_name}.zip" if dir_name else "archive.zip"

        dest = out_dir / sanitize_filename(suggested)
        await download.save_as(str(dest))
        size = dest.stat().st_size if dest.exists() else 0
        print(f"Saved {dest} (size {size} bytes)")

        # Unzip & clean — flatten when extracting into a forced directory
        try:
            with zipfile.ZipFile(dest, 'r') as zip_ref:
                members = zip_ref.namelist()
                roots = set()
                for m in members:
                    if not m:
                        continue
                    parts = m.split('/')
                    if len(parts) > 1 and parts[0]:
                        roots.add(parts[0])
                    else:
                        roots.add('')  # a file at the archive root
                has_single_root = ('' not in roots) and (len(roots) == 1)

                # decide extraction target
                if force_extract_dir is not None:
                    extract_dir = force_extract_dir
                    extract_dir.mkdir(parents=True, exist_ok=True)
                elif has_single_root:
                    extract_dir = out_dir
                else:
                    extract_dir = out_dir / dest.stem
                    extract_dir.mkdir(parents=True, exist_ok=True)

                zip_ref.extractall(extract_dir)

                # If forced dir and zip had a single top-level folder, flatten it
                if force_extract_dir is not None and has_single_root:
                    root_name = next(iter(roots))
                    inner = extract_dir / root_name
                    if inner.exists() and inner.is_dir():
                        for p in inner.iterdir():
                            shutil.move(str(p), str(extract_dir / p.name))
                        try:
                            inner.rmdir()
                        except Exception:
                            pass

            dest.unlink()
            print(f"Extracted to {extract_dir} and removed zip.")
        except Exception as e:
            print(f"Failed to extract zip {dest}: {e}")

        if poll_task:
            try:
                await poll_task
            except Exception:
                pass
            if not poll_task.done():
                poll_task.cancel()
        await context.close()
        await browser.close()
        return dest

# --------------------- Batch from CSV ---------------------

DEFAULT_CSV = Path("metafiles") / "米游社-官方资讯-minas.csv"
BATCH_OUT_SUBDIR = "米游社-官方资讯-minas"

async def _process_csv_row(row: dict, out_root: Path) -> None:
    post_name = _norm_outer_quotes(row.get("post_name") or row.get("title") or row.get("name"))
    url = _norm_cell(row.get("minas_link") or row.get("url") or row.get("link"))
    password = _norm_cell(row.get("minas_pwd") or row.get("password") or row.get("passwd"))
    post_time = _norm_cell(row.get("post_time") or "")

    # Normalize post_time to yyyy.mm.dd if possible
    norm_post_time = ""
    if post_time:
        # Acceptable formats: YYYY-MM-DD... or YYYY/MM/DD...
        if len(post_time) >= 10 and (
            (post_time[4] == '-' and post_time[7] == '-') or (post_time[4] == '/' and post_time[7] == '/')
        ):
            # Extract first 10 chars and replace - or / with .
            norm_post_time = post_time[:10].replace("-", ".").replace("/", ".")
        else:
            norm_post_time = ""

    if not url or not password or not post_name:
        print(f"[CSV] Skip (missing field): post_name='{post_name}' url='{url}' pwd={'yes' if bool(password) else 'no'}")
        return

    folder_name = f"{norm_post_time} {post_name}" if norm_post_time else post_name
    target_dir = out_root / sanitize_filename(folder_name)
    if target_dir.exists():
        print(f"[CSV] SKIP (exists): {target_dir}")
        return

    print(f"[CSV] {folder_name}: {url} / -> {target_dir}")
    await _zip_current_path(url, password, path="/", out_dir=out_root, force_extract_dir=target_dir)

def run_batch_from_csv(csv_path: Path, out_dir: Path) -> None:
    out_root = out_dir / BATCH_OUT_SUBDIR
    out_root.mkdir(parents=True, exist_ok=True)

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print(f"[CSV] No rows found in {csv_path}")
        return

    print(f"[CSV] Loaded {len(rows)} rows from {csv_path}")
    max_rows = None
    try:
        max_rows = int(MAX_ROWS) if MAX_ROWS is not None else None
    except Exception:
        max_rows = None
    row_timeout = ROW_TIMEOUT_SEC if ROW_TIMEOUT_SEC > 0 else None

    for idx, row in enumerate(rows, 1):
        if max_rows is not None and idx > max_rows:
            print(f"[CSV] Reached MINAS_MAX_ROWS={max_rows}; stopping early.")
            break
        try:
            if row_timeout:
                asyncio.run(asyncio.wait_for(_process_csv_row(row, out_root), timeout=row_timeout))
            else:
                asyncio.run(_process_csv_row(row, out_root))
            print(f"[CSV] ({idx}/{len(rows)}) done")
        except asyncio.TimeoutError:
            print(f"[CSV] ({idx}/{len(rows)}) TIMEOUT after {row_timeout}s; skipping.")
        except KeyboardInterrupt:
            print("\n[CSV] Aborted by user.")
            break
        except Exception as e:
            print(f"[CSV] ({idx}/{len(rows)}) FAILED: {e}")

# --------------------- Orchestrator ---------------------

def run(url: str, password: str, out_dir: Path) -> None:
    # Default to single ZIP on '/' if neither zip nor batch is selected explicitly
    zip_args = getattr(run, "__zip_args__", None)
    if not zip_args or (not zip_args.get("zip_one")):
        zip_args = {"zip_one": True, "path": "/"}

    if zip_args.get("zip_one"):
        path = zip_args.get("path") or "/"
        saved = asyncio.run(_zip_current_path(url, password, path, out_dir))
        print(f"ZIP completed: {saved}")

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minas ZIP downloader (single or batch).")
    parser.add_argument("--url", default=DEFAULT_URL, help="Share URL for single ZIP mode")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Password for single ZIP mode")
    parser.add_argument("--out", default=DEFAULT_OUTDIR, help="Output directory (default: ./downloads)")

    # ZIP (single)
    parser.add_argument("--zip", action="store_true", help="Click ZIP and download one path from a single share")
    parser.add_argument("--path", default="/", help="Path within the share for single ZIP mode (default: /)")

    # Batch
    parser.add_argument("--batch", action="store_true", help="Read CSV and download each post (zip, extract, rename)")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to CSV (default: metafiles/米游社-官方资讯-minas.csv)")

    args = parser.parse_args(argv)

    # Batch first
    if args.batch:
        run_batch_from_csv(Path(args.csv), Path(args.out))
        return 0

    try:
        run.__zip_args__ = {"zip_one": bool(args.zip), "path": args.path}
        run(args.url, args.password, Path(args.out))
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1

if __name__ == "__main__":
    # sys.exit(main())

    run_batch_from_csv(
        Path("metafiles/米游社-官方资讯-minas.csv"),
        Path("./downloads")
    )
