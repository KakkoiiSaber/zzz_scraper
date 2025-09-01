from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Set, Tuple
from urllib.parse import urljoin, urlparse, parse_qs
from datetime import datetime
import re
import json
from pathlib import Path
import csv


from bs4 import BeautifulSoup
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # for src/

# Optional: requests_html for JS-rendered pages
HTMLSession = None  # type: ignore
try:
    from requests_html import HTMLSession as _HTMLSession  # optional, for JS-rendered pages
    HTMLSession = _HTMLSession
except Exception as _e:  # keep going; we will try importing lazily too
    import logging as _logging
    _logging.getLogger("PostScraper").info(f"requests_html import failed: {_e}")

# Robust import to work whether package is run as module or script
try:
    from .BaseScraper import BaseScraper  # package-relative (src/scraper)
except Exception:
    try:
        from src.base.BaseScraper import BaseScraper  # absolute within src
    except Exception:
        from BaseScraper import BaseScraper  # same-folder fallback


@dataclass
class SelectorCfg:
    item: str
    title: str
    href: str
    time: str | None = None


class PostScraper(BaseScraper):
    """
    Scan a listing page of posts and save a CSV with columns:
        post_time, post_name, post_url

    Output path is always: posts/<task_name>.csv

    Supported task.json entry (list or mapping):
      {
        "name": "task_name",
        "source" | "source_url" | "url": "https://...",
        "max_pages": 1,  # <=0 means unlimited (generic path)
        "page_size": 20, # for MiYoUShe API
        "stop_on_seen": true,
        "selectors": {"item": "...", "title": "...", "href": "a", "time": ".date"},
        "pagination": {"param": "page", "start": 1, "stop": 3},
        "keyword": "optional substring filter"
      }
    """

    def __init__(self, task_name: str, **kwargs: Any) -> None:
        super().__init__(task_name, **kwargs)
        t = self.task
        self.source_url: str = t.get("source_url") or t.get("url") or t.get("source")
        if not self.source_url:
            raise ValueError("Task must define 'source_url'/'url'/'source'.")
        self.max_pages: int = int(t.get("max_pages", 1))
        self.pagination: Dict[str, Any] = t.get("pagination", {}) if isinstance(t.get("pagination", {}), dict) else {}
        sel = t.get("selectors", {}) if isinstance(t.get("selectors", {}), dict) else {}
        self.selectors: SelectorCfg | None = None
        if sel:
            self.selectors = SelectorCfg(
                item=sel.get("item", ""),
                title=sel.get("title", ""),
                href=sel.get("href", "a"),
                time=sel.get("time"),
            )
        # optional keyword filter from task.json
        self.keyword: str = str(t.get("keyword") or "").strip()
        # MiYoUShe paging & dedupe behavior
        self.page_size: int = int(t.get("page_size", 20))
        self.stop_on_seen: bool = bool(t.get("stop_on_seen", True))
        # Official site API config (optional, speeds up zzz.mihoyo.com)
        self.mhy_api_url: str = t.get("mhy_api_url", "https://api-takumi-static.mihoyo.com/content_v2_user/app/706fd13a87294881/getContentList")
        self.mhy_channel_id: int = int(t.get("mhy_channel_id", 273))
        self.mhy_lang: str = t.get("mhy_lang", "zh-cn")

    # -------------------- public API --------------------
    def run(self) -> None:
        existing_urls = self._load_existing_urls()
        # MiYoUShe pages are app-driven; use official API instead of HTML
        if self._is_miyoushe(self.source_url):
            rows = self._extract_miyoushe_forum(self.source_url, existing_urls)
            rows = self._apply_keyword_filter(rows)
            self._write_posts_csv(rows, existing_urls)
            return

        # Official website (zzz.mihoyo.com/news) has numbered pages
        if self._is_mihoyo_official(self.source_url):
            # Prefer fast JSON API if available; fall back to DOM/rendering
            rows = self._extract_mihoyo_official_news_api(existing_urls)
            if not rows:
                rows = self._extract_mihoyo_official_news(self.source_url, existing_urls)
            if not rows:
                self.log.info(f"no items extracted from {self.source_url}; check selectors/pagination/api")
            rows = self._apply_keyword_filter(rows)
            self._write_posts_csv(rows, existing_urls)
            return

        rows: List[Dict[str, str]] = []
        for page_idx, page_url in enumerate(self._iter_page_urls(), start=1):
            soup = self.get_soup(page_url)
            items = self._extract_items(soup, base=page_url)
            if not items:
                if not self.pagination or self.pagination.get("stop") is None:
                    break
            rows.extend(items)
            if self.max_pages and page_idx >= self.max_pages:
                break
        rows = self._apply_keyword_filter(rows)
        self._write_posts_csv(rows, existing_urls)

    # -------------------- helpers --------------------
    def _iter_page_urls(self) -> Iterable[str]:
        pag = self.pagination
        if not pag:
            yield self.source_url
            return
        start = int(pag.get("start", 1))
        stop = pag.get("stop")
        param = pag.get("param", "page")
        i = start
        while True:
            sep = '&' if ('?' in self.source_url) else '?'
            yield f"{self.source_url}{sep}{param}={i}"
            if stop is not None and i >= int(stop):
                break
            i += 1

    def _extract_items(self, soup: BeautifulSoup, *, base: str) -> List[Dict[str, str]]:
        if self.selectors and self.selectors.item:
            got = self._extract_by_selectors(soup, self.selectors, base)
            if got:
                return got
        items = self._try_mihoyo_news(soup, base)
        if items:
            return items
        json_items = self._extract_from_embedded_json(soup, base)
        if json_items:
            return json_items
        out: List[Dict[str, str]] = []
        for a in soup.select('article a[href]'):
            title = a.get_text(strip=True)
            href = a.get('href')
            if not title or not href:
                continue
            out.append({
                'post_time': '',
                'post_name': title,
                'post_url': urljoin(base, href),
            })
            self.log.info(f"scanned:  | {title}")
        return out

    def _extract_by_selectors(self, soup: BeautifulSoup, sel: SelectorCfg, base: str) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        for node in soup.select(sel.item):
            title_el = node.select_one(sel.title) if sel.title else None
            href_el = node.select_one(sel.href) if sel.href else None
            time_el = node.select_one(sel.time) if sel.time else None
            title = (title_el.get_text(strip=True) if title_el else '').strip()
            href = href_el.get('href') if href_el else None
            if not href:
                continue
            url = urljoin(base, href)
            when = time_el.get_text(strip=True) if time_el else ''
            rows.append({'post_time': when, 'post_name': title, 'post_url': url})
            self.log.info(f"scanned: {when} | {title}")
        return rows

    def _try_mihoyo_news(self, soup: BeautifulSoup, base: str) -> List[Dict[str, str]]:
        candidates: List[Dict[str, str]] = []
        for li in soup.select('li a[href], .article-list a[href], .news-list a[href], .news__list a[href]'):
            title = li.get_text(strip=True)
            href = li.get('href')
            if not href or not title:
                continue
            when = ''
            parent = li.parent
            if parent:
                t = parent.select_one('.time, .date, time')
                if t:
                    when = t.get_text(strip=True)
            candidates.append({'post_time': when, 'post_name': title, 'post_url': urljoin(base, href)})
        seen = set()
        unique: List[Dict[str, str]] = []
        for row in candidates:
            u = row['post_url']
            if u in seen:
                continue
            seen.add(u)
            unique.append(row)
        return unique

    def _extract_from_embedded_json(self, soup: BeautifulSoup, base: str) -> List[Dict[str, str]]:
        texts: List[str] = []
        for s in soup.find_all('script'):
            if s.string:
                texts.append(s.string)
            elif s.contents:
                try:
                    texts.append(''.join(map(str, s.contents)))
                except Exception:
                    pass
        blob_candidates: List[str] = []
        for t in texts:
            if '__NEXT_DATA__' in t or 'window.__NUXT__' in t or 'pageProps' in t or 'asyncData' in t:
                blob_candidates.append(t)
        if not blob_candidates:
            blob_candidates = texts
        rows: List[Dict[str, str]] = []

        def push(title: str, href: str, when: str = '') -> None:
            if not title or not href:
                return
            rows.append({
                'post_time': when or '',
                'post_name': title.strip(),
                'post_url': urljoin(base, href.strip()),
            })

        json_like = re.findall(r"[{\[][^<>]+[}\]]", '\n'.join(blob_candidates))
        for block in json_like:
            if len(block) > 2_000_000:
                continue
            try:
                data = json.loads(block)
            except Exception:
                continue
            stack = [data]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    keys = set(cur.keys())
                    title_key = 'title' if 'title' in keys else ('name' if 'name' in keys else None)
                    href_key = 'href' if 'href' in keys else ('url' if 'url' in keys else None)
                    when_key = None
                    for k in ('time', 'date', 'created_at', 'publish_time', 'pub_time'):
                        if k in keys:
                            when_key = k
                            break
                    if title_key and href_key:
                        push(str(cur.get(title_key, '')), str(cur.get(href_key, '')), str(cur.get(when_key, '') or ''))
                    for v in cur.values():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(cur, list):
                    for v in cur:
                        if isinstance(v, (dict, list)):
                            stack.append(v)
        seen = set()
        uniq: List[Dict[str, str]] = []
        for r in rows:
            u = r['post_url']
            if u in seen:
                continue
            seen.add(u)
            uniq.append(r)
        return uniq

    def _apply_keyword_filter(self, rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
        kw = (self.keyword or '').strip()
        if not kw:
            return rows
        kw_lower = kw.lower()
        out: List[Dict[str, str]] = []
        for r in rows:
            name = (r.get('post_name') or '').lower()
            if kw_lower in name:
                out.append(r)
        return out

    # --- MiYoUShe detection and API extractor ---

    def _render_html(self, url: str, *, sleep: float = 1.0, scrolldown: int = 0) -> BeautifulSoup | None:
        """Render a JS-heavy page using requests_html/pyppeteer if available.
        Returns BeautifulSoup or None if rendering failed or library missing.
        """
        session_cls = HTMLSession
        if session_cls is None:
            try:
                from requests_html import HTMLSession as session_cls  # type: ignore
                self.log.info("requests_html loaded via lazy import")
            except Exception as e:
                self.log.info(f"requests_html is not available: {e}")
                return None
        try:
            sess = session_cls()
            r = sess.get(url, headers=self.session.headers)
            # render with a small wait; optionally scroll down a few times
            r.html.render(sleep=sleep, scrolldown=scrolldown, timeout=30)
            html = r.html.html
            return BeautifulSoup(html, "html.parser")
        except Exception as e:
            self.log.info(f"JS render failed: {e}")
            return None
    def _is_miyoushe(self, url: str) -> bool:
        try:
            host = urlparse(url).netloc
            return 'miyoushe.com' in host
        except Exception:
            return False

    def _is_mihoyo_official(self, url: str) -> bool:
        try:
            p = urlparse(url)
            return ('zzz.mihoyo.com' in p.netloc) and p.path.startswith('/news')
        except Exception:
            return False

    def _extract_mihoyo_official_news(self, start_url: str, existing_urls: Set[str]) -> List[Dict[str, str]]:
        seen_page_urls: Set[str] = set()
        page_url = start_url
        page = 0
        out: List[Dict[str, str]] = []
        while page_url:
            if page_url in seen_page_urls:
                self.log.info("pagination loop detected; stopping")
                break
            seen_page_urls.add(page_url)
            page += 1
            self.log.info(f"fetching news page {page}: {page_url}")
            soup = self.get_soup(page_url)

            # Extract items on this page
            rows: List[Dict[str, str]] = []
            anchors = []

            def collect_anchors(soup_obj: BeautifulSoup):
                cards = soup_obj.select('.news-list li, .news__list li, .article-list li, li.news-item, article, .news-list__item')
                tmp = []
                if cards:
                    for node in cards:
                        a = node if getattr(node, 'name', '') == 'a' else node.select_one('a[href]')
                        if a:
                            tmp.append((node, a))
                else:
                    for a in soup_obj.select('a[href^="/news/"]'):
                        tmp.append((a.parent, a))
                return tmp

            anchors = collect_anchors(soup)

            # If nothing found, try JS rendering
            if not anchors:
                js_soup = self._render_html(page_url, sleep=1.2, scrolldown=2)
                if js_soup is not None:
                    anchors = collect_anchors(js_soup)

            if not anchors:
                self.log.info("no anchors found on page; page may be JS-only or selectors need update")

            # Fallback: try extracting from embedded JSON blobs
            if not anchors:
                json_rows = self._extract_from_embedded_json(soup, page_url)
                if not json_rows and 'js_soup' in locals() and js_soup is not None:
                    json_rows = self._extract_from_embedded_json(js_soup, page_url)
                if json_rows:
                    # Filter to only /news/<id>
                    filtered = []
                    for jr in json_rows:
                        u = jr.get('post_url') or ''
                        if re.search(r"/news/\d+", u):
                            filtered.append(jr)
                    if filtered:
                        self.log.info(f"extracted {len(filtered)} items from embedded JSON")
                        out.extend([r for r in filtered if r['post_url'] not in existing_urls])
                        # attempt to discover a next page from JSON is site-specific; keep normal pager flow

            page_new = 0
            anchor_seen: Set[str] = set()
            for node, a in anchors:
                href = a.get('href')
                title = a.get_text(strip=True)
                if not href:
                    continue
                # Only keep article detail links like /news/<id>
                if not re.search(r"/news/\d+", href):
                    continue
                url = urljoin(page_url, href)
                if url in anchor_seen:
                    continue
                anchor_seen.add(url)

                when = ''
                # try nearby date elements
                parent = node if getattr(node, 'name', '') != 'a' else node.parent
                if parent:
                    t = parent.select_one('time, .time, .date, .news__date, .news-item__date, .list-date, .date__text')
                    if t:
                        when = t.get_text(strip=True)
                rows.append({'post_time': when, 'post_name': title, 'post_url': url})
                self.log.info(f"scanned: {when} | {title}")

            # filter out ones already present
            new_rows = []
            for r in rows:
                if r['post_url'] in existing_urls:
                    continue
                new_rows.append(r)
                page_new += 1
            out.extend(new_rows)

            # stop conditions
            if self.max_pages > 0 and page >= self.max_pages:
                break
            if self.stop_on_seen and page_new == 0 and page > 1:
                self.log.info("no new items on this page; stopping due to stop_on_seen=True")
                break

            # find the next page link
            next_url = self._find_next_page_url(soup, base=page_url)
            if not next_url and 'js_soup' in locals() and js_soup is not None:
                next_url = self._find_next_page_url(js_soup, base=page_url)
            self.log.info(f"next page -> {next_url}")
            if not next_url:
                break
            page_url = next_url
        # dedupe by url
        uniq_map: Dict[str, Dict[str, str]] = {}
        for r in out:
            uniq_map.setdefault(r['post_url'], r)
        return list(uniq_map.values())

    def _find_next_page_url(self, soup: BeautifulSoup, *, base: str) -> str:
        # 1) rel=next or obvious next classes
        for a in soup.select('a[rel="next"], a.next, .next a, .pager a.next, .pagination a.next'):
            href = a.get('href')
            if href:
                return urljoin(base, href)

        # 2) numbered pagination: pick the link whose text is current+1
        cur = None
        # find current page number from active element or URL
        cur_el = soup.select_one('.pagination .active, .pager .active, .page-item.active, .active > a')
        if cur_el:
            try:
                cur = int(cur_el.get_text(strip=True))
            except Exception:
                cur = None
        if cur is None:
            # parse from URL query (?page=NN)
            try:
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(base).query)
                if 'page' in q:
                    cur = int(q['page'][0])
            except Exception:
                cur = None
        if cur is not None:
            want = str(cur + 1)
            for a in soup.select('.pagination a, .pager a, a.page-link, a'):
                txt = (a.get_text() or '').strip()
                if txt == want:
                    href = a.get('href')
                    if href:
                        return urljoin(base, href)

        # 3) last resort: any anchor with text hint
        for a in soup.find_all('a'):
            txt = (a.get_text() or '').strip()
            if txt in {'>', '›', '下一页', '下一頁', '下一页 ›'}:
                href = a.get('href')
                if href:
                    return urljoin(base, href)

        # 4) ultimate fallback: try incrementing ?page=
        try:
            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            u = urlparse(base)
            q = parse_qs(u.query)
            cur = int(q.get('page', ['1'])[0])
            q['page'] = [str(cur + 1)]
            new_query = urlencode({k: v[0] for k, v in q.items()})
            return urlunparse((u.scheme, u.netloc, u.path, u.params, new_query, u.fragment))
        except Exception:
            return ''

    def _extract_miyoushe_forum(self, base_url: str, existing_urls: Set[str]) -> List[Dict[str, str]]:
        parsed = urlparse(base_url)
        qs = parse_qs(parsed.query)
        sort_type = int(qs.get('type', [3])[0])  # default to 3
        forum_id = None
        parts = [p for p in parsed.path.split('/') if p]
        try:
            idx = parts.index('home')
            forum_id = int(parts[idx + 1])
        except Exception:
            pass

        # Detect game id (gids). For ZZZ it's 8.
        gids = None
        try:
            # e.g., /zzz/home/58?type=3  -> gids=8
            if 'zzz' in parts:
                gids = 8
        except Exception:
            pass

        if forum_id is None:
            return []

        api_url = 'https://bbs-api.miyoushe.com/post/wapi/getForumPostList'
        last_id = ''
        prev_last_id = None
        seen_post_ids: Set[str] = set()
        page = 0
        all_rows: List[Dict[str, str]] = []

        while True:
            page += 1
            params = {
                'forum_id': forum_id,
                'sort_type': sort_type,
                'page_size': self.page_size,
            }
            if last_id:
                params['last_id'] = last_id
            if gids is not None:
                params['gids'] = gids

            resp = self.fetch(api_url, params=params)
            try:
                data = resp.json()
            except Exception:
                break
            payload = (data or {}).get('data') or {}
            lst = payload.get('list') or []
            if not lst:
                break

            self.log.info(f"page {page}: got {len(lst)} items, last_id={payload.get('last_id')}")

            page_new = 0
            for item in lst:
                post = item.get('post') or {}
                post_id = str(post.get('post_id') or '')
                if not post_id or post_id in seen_post_ids:
                    continue
                seen_post_ids.add(post_id)

                title = (post.get('subject') or '').strip() or (post.get('content') or '').strip()
                created = str(post.get('created_at') or post.get('time') or '')
                when = ''
                try:
                    ts = int(created)
                    if 946684800 < ts < 4102444800:
                        when = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    when = created

                url = f'https://www.miyoushe.com/zzz/article/{post_id}' if post_id else ''
                if title and url:
                    if url in existing_urls:
                        continue
                    all_rows.append({'post_time': when, 'post_name': title, 'post_url': url})
                    self.log.info(f"scanned: {when} | {title}")
                    page_new += 1

            # Choose next page cursor
            next_last_id = str(payload.get('last_id') or '')
            if not next_last_id and lst:
                try:
                    next_last_id = str((lst[-1].get('post') or {}).get('post_id') or '')
                except Exception:
                    next_last_id = ''
            # Stop if cursor didn't advance
            if prev_last_id == next_last_id:
                self.log.info("cursor did not advance; stopping to avoid repeat content")
                break
            last_id = next_last_id
            prev_last_id = next_last_id

            # If nothing new on this page and we are allowed to stop upon seen content, break early (but not on very first page)
            if self.stop_on_seen and page_new == 0 and last_id:
                self.log.info("no new items on this page; stopping due to stop_on_seen=True")
                break

            if self.max_pages > 0 and page >= self.max_pages:
                break

        # de-dupe by URL
        seen = set()
        out: List[Dict[str, str]] = []
        for r in all_rows:
            u = r['post_url']
            if u in seen:
                continue
            seen.add(u)
            out.append(r)
        return out

    def _load_existing_urls(self) -> Set[str]:
        posts_root = Path("posts")
        out_path = posts_root / f"{self.task_name}.csv"
        seen: Set[str] = set()
        if not out_path.exists():
            return seen
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    url = (row.get("post_url") or "").strip()
                    if url:
                        seen.add(url)
        except Exception:
            pass
        return seen

    # -------------------- writing --------------------
    def _write_posts_csv(self, rows: List[Dict[str, str]], existing_urls: Set[str] | None = None) -> None:
        posts_root = Path("posts")
        posts_root.mkdir(parents=True, exist_ok=True)
        out_path = posts_root / f"{self.task_name}.csv"
        fieldnames = ["post_time", "post_name", "post_url"]

        # Load existing rows for merge
        existing: List[Dict[str, str]] = []
        if out_path.exists():
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        existing.append({k: r.get(k, "") for k in fieldnames})
            except Exception:
                pass

        # Merge, dedupe by post_url (prefer existing row if duplicate)
        by_url: Dict[str, Dict[str, str]] = {}
        for r in existing:
            u = (r.get("post_url") or "").strip()
            if u:
                by_url[u] = r
        new_rows: List[Dict[str, str]] = []
        for r in rows:
            u = (r.get("post_url") or "").strip()
            if not u:
                continue
            if u not in by_url:
                by_url[u] = {k: r.get(k, "") for k in fieldnames}
                new_rows.append({k: r.get(k, "") for k in fieldnames})

        # Place new rows on top, then the rest (excluding duplicates)
        merged = new_rows + [r for r in existing if (r.get("post_url") or "").strip() not in {row["post_url"] for row in new_rows}]

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in merged:
                writer.writerow({k: r.get(k, "") for k in fieldnames})
        self.log.info(f"wrote CSV: {out_path} (added {len(new_rows)} new, total {len(merged)})")
    def _extract_mihoyo_official_news_api(self, existing_urls: Set[str]) -> List[Dict[str, str]]:
        """Use the official JSON endpoint observed in DevTools to page news quickly.
        Defaults are tuned for zzz.mihoyo.com based on provided network capture.
        """
        api = self.mhy_api_url
        chan = self.mhy_channel_id
        lang = self.mhy_lang
        page = 1
        page_size = self.page_size if self.page_size > 0 else 9
        out: List[Dict[str, str]] = []
        total_seen_new = 0
        while True:
            params = {
                "iPageSize": page_size,
                "iPage": page,
                "sLangKey": lang,
                "iChanId": chan,
            }
            # The API is same-site CORS in browser; for server we just send headers
            headers = {
                "Referer": "https://zzz.mihoyo.com/",
                "Origin": "https://zzz.mihoyo.com",
                "Accept": "application/json, text/plain, */*",
                "X-Rpc-Language": lang,
            }
            self.log.info(f"API page {page}: {api} params={params}")
            resp = self.fetch(api, params=params, headers=headers)
            try:
                data = resp.json()
            except Exception as e:
                self.log.info(f"API JSON parse failed on page {page}: {e}")
                break
            payload = (data or {}).get("data") or {}
            items = payload.get("list") or []
            if not items:
                break
            page_new = 0
            for it in items:
                # Fields seen in capture: iInfoId, sTitle, dtStartTime
                post_id = str(it.get("iInfoId") or "").strip()
                title = str(it.get("sTitle") or "").strip()
                when = str(it.get("dtStartTime") or it.get("dtCreateTime") or "").strip()
                if not post_id or not title:
                    continue
                url = f"https://zzz.mihoyo.com/news/{post_id}"
                if url in existing_urls:
                    continue
                out.append({"post_time": when, "post_name": title, "post_url": url})
                self.log.info(f"scanned(API): {when} | {title}")
                total_seen_new += 1
                page_new += 1
            # Early stop rules
            if self.max_pages > 0 and page >= self.max_pages:
                break
            if self.stop_on_seen and page_new == 0 and page > 1:
                self.log.info("no new items on this API page; stopping due to stop_on_seen=True")
                break
            page += 1
        # Deduplicate just in case
        uniq: Dict[str, Dict[str, str]] = {}
        for r in out:
            uniq[r["post_url"]] = r
        return list(uniq.values())
    
if __name__ == "__main__":
    PostScraper("米游社-官方资讯").run()
    PostScraper("官网-新闻资讯").run()