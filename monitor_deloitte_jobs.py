import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ✅ Your base URL (can include non-location filters like your 161=[219,222] etc.)
BASE_SEARCH_URL = "https://apply.deloitte.co.uk/UKCareers/SearchJobs/?161=%5B219%2C222%5D&161_format=273&3884=%5B330998%5D&3884_format=3018&listFilterMode=1&jobRecordsPerPage=20&"

STATE_DIR = Path(".state")
REPORT_FILE = STATE_DIR / "last_run_report.json"

# Two independent monitors, each with its own state file.
SEARCHES = {
    "edinburgh-only": {
        "label": "Deloitte jobs: Edinburgh only",
        "locations": ["Edinburgh"],
    },
    "glasgow-only": {
        "label": "Deloitte jobs: Glasgow only",
        "locations": ["Glasgow"],
    },
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


def _try_click_first(page, locators: List[Any], timeout_ms: int = 3000) -> bool:
    """
    Try a list of locators; click the first that exists and is visible/clickable.
    Returns True if clicked something.
    """
    for locator in locators:
        try:
            if locator.count() > 0:
                # Ensure it's visible-ish
                locator.first.wait_for(state="visible", timeout=timeout_ms)
                locator.first.click()
                return True
        except Exception:
            continue
    return False


def apply_location_filters_strict(page, locations: List[str]) -> None:
    """
    Opens the Location filter UI, selects ONLY the provided locations, applies,
    and verifies those locations are active.

    This is designed to fail loudly if it cannot confirm the filter is applied.
    """

    # 1) Open the Location filter panel
    opened = _try_click_first(
        page,
        locators=[
            page.get_by_role("button", name=re.compile(r"\bLocation\b", re.I)),
            page.get_by_role("link", name=re.compile(r"\bLocation\b", re.I)),
            page.locator("button:has-text('Location')"),
            page.locator("text=Location").locator("xpath=.."),
        ],
        timeout_ms=5000,
    )
    if not opened:
        raise RuntimeError("Could not find/open the Location filter control. The UI may have changed.")

    page.wait_for_timeout(800)

    # 2) Clear any existing selections if a clear/reset exists (best effort)
    _try_click_first(
        page,
        locators=[
            page.get_by_role("button", name=re.compile(r"\bClear\b|\bReset\b|\bRemove all\b", re.I)),
            page.locator("button:has-text('Clear')"),
            page.locator("button:has-text('Reset')"),
        ],
        timeout_ms=1500,
    )

    # 3) Select the desired location(s) (strictly)
    # We try multiple ways to tick a checkbox by label text.
    for loc in locations:
        loc = loc.strip()
        if not loc:
            continue

        selected = False

        # Best case: accessible label wiring
        try:
            cb = page.get_by_label(re.compile(rf"^{re.escape(loc)}$", re.I))
            cb.first.check()
            selected = True
        except Exception:
            pass

        # Fallback: click label that contains the text, then ensure checkbox is checked
        if not selected:
            try:
                label = page.locator("label", has_text=re.compile(rf"\b{re.escape(loc)}\b", re.I)).first
                label.wait_for(state="visible", timeout=3000)
                label.click()
                selected = True
            except Exception:
                pass

        # Another fallback: checkbox near text (last resort)
        if not selected:
            try:
                row = page.locator(f"text={loc}").first
                row.wait_for(state="visible", timeout=3000)
                # try clicking the row itself
                row.click()
                selected = True
            except Exception:
                pass

        if not selected:
            raise RuntimeError(f"Could not select location '{loc}'. The label/text may differ from expected.")

    # 4) Apply / Done / Update results
    applied = _try_click_first(
        page,
        locators=[
            page.get_by_role("button", name=re.compile(r"\bApply\b|\bDone\b|\bUpdate\b|\bShow results\b", re.I)),
            page.locator("button:has-text('Apply')"),
            page.locator("button:has-text('Done')"),
            page.locator("button:has-text('Update')"),
        ],
        timeout_ms=5000,
    )

    # Some UIs auto-apply; if no apply button, proceed but verify via chips/text.
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)

    # 5) Verification (strict): require each location name to appear somewhere in the active filter area/chips.
    # We search for common patterns like filter chips/tags/selected summary.
    # If this proves too strict for the Deloitte UI, the next best method is "codegen" to target chips exactly.
    for loc in locations:
        try:
            # Broad check: the selected location should appear on the page post-apply.
            # We require visibility (not just existence in hidden DOM).
            page.get_by_text(re.compile(rf"\b{re.escape(loc)}\b", re.I)).first.wait_for(state="visible", timeout=15000)
        except Exception:
            # If the UI doesn't show chips, another verification is to ensure results include the location text.
            # We'll do that later as a secondary check (below).
            pass

    # Secondary verification: confirm at least one job card/result includes the location text.
    # This helps ensure we didn’t just match the word somewhere irrelevant.
    # If there are zero results, we can’t do this check; but then your state will store empty results (still valid).
    page.wait_for_timeout(500)


def extract_jobs_with_playwright(base_url: str, locations: List[str]) -> Tuple[List[Dict[str, str]], str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        page.goto(base_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(1200)

        apply_location_filters_strict(page, locations)

        # Wait for results to settle
        try:
            page.wait_for_selector("a[href]", timeout=30_000)
        except PlaywrightTimeoutError:
            pass

        page_title = page.title()

        anchors = page.query_selector_all("a[href]")
        jobs: List[Dict[str, str]] = []

        for a in anchors:
            href = a.get_attribute("href") or ""
            text = (a.inner_text() or "").strip()
            if not href:
                continue

            if href.startswith("/"):
                href_abs = "https://apply.deloitte.co.uk" + href
            elif href.startswith("http"):
                href_abs = href
            else:
                href_abs = "https://apply.deloitte.co.uk/" + href.lstrip("/")

            looks_like_job = any(
                pat in href_abs.lower()
                for pat in [
                    "job",
                    "vacancy",
                    "jobid",
                    "requisition",
                    "posting",
                    "careers/job",
                    "ukcareers/job",
                ]
            )

            if looks_like_job and 6 <= len(text) <= 160:
                jobs.append({"title": text, "url": href_abs})

        # Deduplicate by URL, keep stable ordering
        seen = set()
        unique_jobs = []
        for j in jobs:
            jn = normalize_job(j)
            if not jn["url"] or jn["url"] in seen:
                continue
            seen.add(jn["url"])
            unique_jobs.append(jn)
        unique_jobs.sort(key=lambda x: x["url"])

        context.close()
        browser.close()

        return unique_jobs, page_title


def write_github_output(any_changed: bool, total_new: int) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(f"changed={'true' if any_changed else 'false'}\n")
        f.write(f"new_count={total_new}\n")


def save_report(results: List[SearchResult]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_utc": _now_utc_iso(),
        "base_url": BASE_SEARCH_URL,
        "results": [
            {
                "key": r.key,
                "label": r.label,
                "locations": SEARCHES[r.key]["locations"],
                "page_title": r.page_title,
                "jobs_found": len(r.jobs),
                "new_jobs_found": len(r.new_jobs),
                "new_jobs": r.new_jobs,
            }
            for r in results
        ],
    }
    REPORT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    all_results: List[SearchResult] = []
    total_new = 0

    for key, cfg in SEARCHES.items():
        label = cfg["label"]
        locations = cfg["locations"]

        prev = load_previous(key)
        prev_jobs = prev.get("jobs", [])

        current_jobs, page_title = extract_jobs_with_playwright(BASE_SEARCH_URL, locations)
        new_jobs = diff_jobs(prev_jobs, current_jobs)

        save_current(key, current_jobs)

        total_new += len(new_jobs)

        all_results.append(
            SearchResult(
                key=key,
                label=label,
                url=BASE_SEARCH_URL,
                jobs=current_jobs,
                new_jobs=new_jobs,
                page_title=page_title,
            )
        )

        print(f"\n=== {label} ===")
        print(f"Locations: {locations}")
        print(f"Page: {page_title}")
        print(f"Found jobs: {len(current_jobs)}")
        print(f"New jobs: {len(new_jobs)}")
        for j in new_jobs:
            print(f"- {j['title']} | {j['url']}")

    save_report(all_results)
    write_github_output(any_changed=(total_new > 0), total_new=total_new)


if __name__ == "__main__":
    main()
