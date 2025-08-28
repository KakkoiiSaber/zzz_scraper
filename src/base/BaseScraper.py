from __future__ import annotations

import csv
import json
import time
import random
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BaseScraper:
    """
    A small convenience base class that centralizes configuration, I/O paths,
    HTTP session/retries, and a few HTML utilities for our scrapers.

    Subclasses should implement `run()` and can rely on:
      - self.task_name: canonical task key passed on construction
      - self.task:     task configuration dictionary loaded from task.json
      - self.config:   global configuration dictionary loaded from config.json
      - self.meta_dir: path where metafiles for this task are stored
      - self.download_dir: path where downloads for this task are stored

    Common helpers:
      - fetch(url): get a Response with retries and default headers
      - get_soup(url): fetch + parse HTML to BeautifulSoup
      - save_csv(rows, filename, fieldnames): write CSV file in meta_dir
      - append_csv(row, filename, fieldnames): append (create if not exists)
      - read_csv(filename): read CSV rows as list[dict]
    """

    # ---------- construction ----------
    def __init__(
        self,
        task_name: str,
        *,
        config_path: str | Path = "src/config/config.json",
        task_path: str | Path = "src/config/task.json",
        meta_root: str | Path = "metafiles",
        download_root: str | Path = "downloads",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.task_name = task_name
        self.config_path = Path(config_path)
        self.task_path = Path(task_path)
        self.meta_root = Path(meta_root)
        self.download_root = Path(download_root)

        # Load configs
        self.config: Dict[str, Any] = self._load_json(self.config_path, default={})
        self._tasks: Any = self._load_json(self.task_path, default={})
        self.task: Dict[str, Any] = self._resolve_task(task_name)

        # Derive I/O directories (do not auto-create per-task subdirs here)
        self.meta_dir = Path(self.meta_root)
        self.download_dir = Path(self.download_root)

        # Logging
        if logger is None:
            logging.basicConfig(
                level=logging.INFO,
                # format="[%(asctime)s] %(levelname)s - %(message)s",
                format="[%(levelname)s] - %(message)s",
            )
            logger = logging.getLogger(self.__class__.__name__)
        self.log = logger

        # HTTP session w/ retries
        self.session = self._build_session()

        # Request pacing
        self.default_sleep = float(self.config.get("sleep_seconds", 1.0))
        self.jitter = float(self.config.get("sleep_jitter", 0.3))

    # ---------- JSON / config ----------
    @staticmethod
    def _load_json(path: Path, *, default: Any) -> Any:
        if not path.exists():
            return default
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default

    def _resolve_task(self, task_name: str) -> Dict[str, Any]:
        """Return task dict by name; accept either dict mapping or list of tasks.

        Expected shapes supported:
          1) {"tasks": {"my_task": {...}}}
          2) {"tasks": [{"name": "my_task", ...}, ...]}
          3) {"my_task": {...}} (top-level mapping)
          4) [{"name": "my_task", ...}, ...] (top-level list)
        """
        data = self._tasks

        def _from_mapping(mapping: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if task_name in mapping:
                t = mapping[task_name]
                if isinstance(t, dict):
                    t = t.copy()
                    t.setdefault("name", task_name)
                    return t
            return None

        if isinstance(data, dict):
            # case 1 or 3
            if "tasks" in data and isinstance(data["tasks"], dict):
                found = _from_mapping(data["tasks"])  # type: ignore[arg-type]
                if found:
                    return found
            if "tasks" in data and isinstance(data["tasks"], list):
                for t in data["tasks"]:
                    if isinstance(t, dict) and t.get("name") == task_name:
                        return t.copy()
            found = _from_mapping(data)
            if found:
                return found
        elif isinstance(data, list):
            # case 4
            for t in data:
                if isinstance(t, dict) and t.get("name") == task_name:
                    return t.copy()

        raise KeyError(f"Task '{task_name}' not found in {self.task_path}")

    # ---------- HTTP utilities ----------
    def _build_session(self) -> requests.Session:
        sess = requests.Session()

        # Default headers may be overridden/merged by config.json -> http.headers
        headers = {
            "User-Agent": (
                self.config.get(
                    "user_agent",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                )
            ),
            "Accept-Language": "en-US,en;q=0.8",
        }
        http_cfg = self.config.get("http", {}) if isinstance(self.config, dict) else {}
        headers.update(http_cfg.get("headers", {}))
        sess.headers.update(headers)

        # Retries
        retry_cfg = http_cfg.get("retry", {})
        total = int(retry_cfg.get("total", 3))
        backoff = float(retry_cfg.get("backoff_factor", 0.5))
        status = retry_cfg.get("status_forcelist", [429, 500, 502, 503, 504])
        retry = Retry(
            total=total,
            read=total,
            connect=total,
            backoff_factor=backoff,
            status_forcelist=status,
            allowed_methods=("GET", "POST"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        return sess

    def _sleep(self, seconds: Optional[float] = None) -> None:
        base = self.default_sleep if seconds is None else float(seconds)
        # small jitter to be polite and avoid rate limits
        jitter = random.uniform(-self.jitter, self.jitter) if self.jitter > 0 else 0
        time.sleep(max(0.0, base + jitter))

    def fetch(self, url: str, *, method: str = "GET", **kwargs: Any) -> requests.Response:
        self.log.info(f"fetching: {url}")
        resp = self.session.request(method=method, url=url, timeout=kwargs.pop("timeout", 20), **kwargs)
        resp.raise_for_status()
        self._sleep()
        return resp

    def get_soup(self, url: str, **kwargs: Any) -> BeautifulSoup:
        resp = self.fetch(url, **kwargs)
        return BeautifulSoup(resp.text, "html.parser")

    # ---------- CSV helpers (in meta_dir) ----------
    def save_csv(
        self,
        rows: Iterable[Dict[str, Any]],
        filename: str,
        fieldnames: Optional[List[str]] = None,
    ) -> Path:
        path = self.meta_dir / filename
        rows = list(rows)
        if not rows and fieldnames is None:
            raise ValueError("save_csv requires fieldnames when rows is empty")
        if fieldnames is None:
            # union of keys in insertion order
            keys: List[str] = []
            for r in rows:
                for k in r.keys():
                    if k not in keys:
                        keys.append(k)
            fieldnames = keys
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k, "") for k in fieldnames})
        self.log.info(f"wrote CSV: {path}")
        return path

    def append_csv(
        self,
        row: Dict[str, Any],
        filename: str,
        fieldnames: Optional[List[str]] = None,
    ) -> Path:
        path = self.meta_dir / filename
        exists = path.exists()
        if not exists and fieldnames is None:
            fieldnames = list(row.keys())
        if not exists:
            return self.save_csv([row], filename, fieldnames)
        # ensure header has all keys
        if fieldnames is None:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, [])
            fieldnames = list(dict.fromkeys([*header, *row.keys()]))
            if header != fieldnames:
                # rewrite with upgraded header
                rows = self.read_csv(filename)
                rows.append(row)
                return self.save_csv(rows, filename, fieldnames)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not exists:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in fieldnames})
        self.log.info(f"appended row to: {path}")
        return path

    def read_csv(self, filename: str) -> List[Dict[str, str]]:
        path = self.meta_dir / filename
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return [dict(row) for row in reader]

    # ---------- filesystem helpers ----------
    @staticmethod
    def sanitize_filename(name: str) -> str:
        bad = "\\/:*?\"<>|\n\r\t"
        for ch in bad:
            name = name.replace(ch, "_")
        return name.strip().strip(".")

    def ensure_subdir(self, *parts: str) -> Path:
        p = self.download_dir.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---------- main entry ----------
    def run(self) -> None:  # pragma: no cover - abstract hook
        raise NotImplementedError("Subclasses must implement run()")


__all__ = ["BaseScraper"]
