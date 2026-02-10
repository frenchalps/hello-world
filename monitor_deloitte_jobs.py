import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


SEARCH_URL = "https://apply.deloitte.co.uk/UKCareers/SearchJobs/?161=%5B219%5D&161_format=273&3884=%5B330998%5D&3884_format=3018&listFilterMode=1&jobRecordsPerPage=20&"

STATE_DIR = Path(".state")
STATE_FILE = STATE_DIR / "deloitte_jobs.json"


def load_previous() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"jobs": [], "last_checked_utc": None, "source_url": SEARCH_URL}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_current(jobs: List[Dict[str, str]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_url": SEARCH_URL,
        "last_checked_utc": datetime.now(timezone.utc).isoformat(),
        "jobs": jobs,
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_job(job: Dict[str, str]) -> Dict[str, str]:
    # Keep it stable across small UI text changes
    return {
        "title": re.sub(r"\s+", " ", job.get("title", "").strip()),
        "url": job.get("url", "").strip(),
    }


def diff_jobs(old_jobs: List[Dict[str, str]], new_jobs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    old_urls = {j.get("url") for j in old_jobs if j.get("url")}
    return [j for j in new_jobs if j.get("url") and j["url"] not in old_urls]


def extract_jobs_with_playwright(url: str) -> Tuple[List[Dict[str, str]], str]:
    """
    Uses a real browser to avoid bot protections and to handle dynamic content.
    Returns (jobs, page_title)
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        page.goto(url, wait_until="domcontentloaded", timeout=90_000)

        # Many ATS pages load results after initial HTML; wait for anchors to appear.
        # We don’t rely on a brittle CSS class — we look for job-like links.
        try:
            page.wait_for_timeout(1500)
            page.wait_for_selector("a[href]", timeout=30_000)
        except PlaywrightTimeoutError:
            pass

        page_title = page.title()

        # Pull all anchors and then filter to those that look like job detail links.
        anchors = page.query_selector_all("a[href]")
        jobs: List[Dict[str, str]] = []

        for a in anchors:
            href = a.get_attribute("href") or ""
            text = (a.inner_text() or "").strip()

            if not href:
                continue

            # Make href absolute if needed
            if href.startswith("/"):
                href_abs = "https://apply.deloitte.co.uk" + href
            elif href.startswith("http"):
                href_abs = href
            else:
                # relative-ish paths
                href_abs = "https://apply.deloitte.co.uk/" + href.lstrip("/")

            # Heuristics: job detail pages typically contain "Job" or "Vacancy" patterns.
            # We keep this broad to avoid breaking if Deloitte changes the ATS UI slightly.
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

            # Ignore nav/footer and empty titles
            if looks_like_job and len(text) >= 6 and len(text) <= 140:
                jobs.append({"title": text, "url": href_abs})

        # Deduplicate by URL, keeping the first title seen
        seen = set()
        unique_jobs = []
        for j in jobs:
            if j["url"] in seen:
                continue
            seen.add(j["url"])
            unique_jobs.append(normalize_job(j))

        # A little extra stability: sort by URL
        unique_jobs.sort(key=lambda x: x["url"])

        context.close()
        browser.close()

        return unique_jobs, page_title


def write_github_output(changed: bool, new_count: int) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(f"changed={'true' if changed else 'false'}\n")
        f.write(f"new_count={new_count}\n")


def main() -> None:
    prev = load_previous()
    prev_jobs = prev.get("jobs", [])

    current_jobs, page_title = extract_jobs_with_playwright(SEARCH_URL)

    new_jobs = diff_jobs(prev_jobs, current_jobs)
    changed = len(new_jobs) > 0

    save_current(current_jobs)
    write_github_output(changed, len(new_jobs))

    print(f"Page: {page_title}")
    print(f"Found jobs: {len(current_jobs)}")
    print(f"New jobs: {len(new_jobs)}")
    if new_jobs:
        for j in new_jobs:
            print(f"- {j['title']} | {j['url']}")


if __name__ == "__main__":
    main()
