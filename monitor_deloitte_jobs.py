import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_SEARCH_URL = "https://apply.deloitte.co.uk/UKCareers/SearchJobs/?161=%5B219%2C222%5D&161_format=273&3884=%5B330998%5D&3884_format=3018&listFilterMode=1&jobRecordsPerPage=20&"

STATE_DIR = Path(".state")
REPORT_FILE = STATE_DIR / "last_run_report.json"

SEARCHES = {
    "edinburgh-only": {"label": "Deloitte jobs: Edinburgh only", "locations": ["Edinburgh"]},
    "glasgow-only": {"label": "Deloitte jobs: Glasgow only", "locations": ["Glasgow"]},
}


@dataclass
class SearchResult:
    key: str
    label: str
    url: str
    jobs: List[Dict[str, str]]
    new_jobs: List[Dict[str, str]]
    page_title: str


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_file_for(key: str) -> Path:
    return STATE_DIR / f"deloitte_jobs_{key}.json"


def load_previous(key: str) -> Dict[str, Any]:
    path = _state_file_for(key)
    if not path.exists():
        return {"jobs": [], "last_checked_utc": None, "source_url": BASE_SEARCH_URL, "search_key": key}
    return json.loads(path.read_text(encoding="utf-8"))


def save_current(key: str, jobs: List[Dict[str, str]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "search_key": key,
        "source_url": BASE_SEARCH_URL,
        "last_checked_utc": _now_utc_iso(),
        "jobs": jobs,
    }
    _state_file_for(key).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_job(job: Dict[str, str]) -> Dict[str, str]:
    return {
        "title": re.sub(r"\s+", " ", (job.get("title") or "").strip()),
        "url": (job.get("url") or "").strip(),
    }


def diff_jobs(old_jobs: List[Dict[str, str]], new_jobs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    old_urls = {j.get("url") for j in old_jobs if j.get("url")}
    return [j for j in new_jobs if j.get("url") and j["url"] not in old_urls]


def _safe_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", s.strip())
    return s[:120] if len(s) > 120 else s


def _try_click_first(page, locators: List[Any], timeout_ms: int = 3500) -> bool:
    for locator in locators:
        try:
            if locator.count() > 0:
                locator.first.wait_for(state="visible", timeout=timeout_ms)
                locator.first.click()
                return True
        except Exception:
            continue
    return False


def _open_filters_panel(page) -> None:
    """
    Many ATS sites hide filters behind a "Filter(s)/Refine" button.
    We try several common names before giving up.
    """
    opened = _try_click_first(
        page,
        locators=[
            page.get_by_role("button", name=re.compile(r"\bFilters?\b", re.I)),
            page.get_by_role("button", name=re.compile(r"\bRefine\b|\bRefine search\b", re.I)),
            page.get_by_role("button", name=re.compile(r"\bFilter\b|\bNarrow\b|\bMore filters\b", re.I)),
            page.locator("button:has-text('Filters')"),
            page.locator("button:has-text('Filter')"),
            page.locator("button:has-text('Refine')"),
        ],
        timeout_ms=5000,
    )
    # It's OK if there's no separate panel; some sites show filters inline
    if opened:
        page.wait_for_timeout(600)


def apply_location_filters_strict(page, locations: List[str], diag_prefix: str) -> None:
    """
    Robust filter applier:
      1) try to open a filters panel (optional)
      2) open the Location/Locations/Office/City filter section
      3) select desired location(s)
      4) apply
      5) verify (strict-ish)

    On failure, saves screenshot+html to .state/ so Actions can upload them.
    """

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    def dump_diagnostics(reason: str):
        try:
            shot = STATE_DIR / f"diag_{_safe_filename(diag_prefix)}_{_safe_filename(reason)}.png"
            html = STATE_DIR / f"diag_{_safe_filename(diag_prefix)}_{_safe_filename(reason)}.html"
            page.screenshot(path=str(shot), full_page=True)
            html.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass

    try:
        _open_filters_panel(page)

        # Try to open the actual location section/control
        opened_location = _try_click_first(
            page,
            locators=[
                page.get_by_role("button", name=re.compile(r"\bLocations?\b", re.I)),
                page.get_by_role("button", name=re.compile(r"\bLocation\b", re.I)),
                page.get_by_role("button", name=re.compile(r"\bOffice\b|\bCity\b|\bRegion\b", re.I)),
                page.get_by_role("link", name=re.compile(r"\bLocations?\b|\bLocation\b", re.I)),
                page.locator("button:has-text('Location')"),
                page.locator("button:has-text('Locations')"),
                page.locator("b
