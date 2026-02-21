#!/usr/bin/env python3
"""
Link checker for AmeriDocs HTML files.
Parses all HTML files for external links, checks each one,
and outputs a CSV report of broken links.
"""

import asyncio
import csv
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

import aiohttp

# --- Configuration ---
SITE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_CSV = os.path.join(SITE_DIR, "broken_links_report.csv")
CONCURRENCY = 20          # max simultaneous requests
TIMEOUT_SECONDS = 30      # per-request timeout
RETRY_COUNT = 1           # retry once on failure
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


# --- HTML link extraction ---
class LinkExtractor(HTMLParser):
    """Pull every href from <a> tags."""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for attr, value in attrs:
                if attr == "href" and value:
                    self.links.append(value)


def extract_links(html_path):
    """Return a list of (url, link_text_placeholder) from an HTML file."""
    with open(html_path, encoding="utf-8") as f:
        content = f.read()
    parser = LinkExtractor()
    parser.feed(content)
    return parser.links


def is_external(url):
    """True if the URL points to an external site (http/https)."""
    return url.startswith("http://") or url.startswith("https://")


# --- Async link checking ---
async def check_one_url(session, url, semaphore, retries=RETRY_COUNT):
    """
    HEAD-then-GET check for a single URL.
    Returns (status_code: int|None, reason: str).
    """
    async with semaphore:
        for attempt in range(1 + retries):
            try:
                # Try HEAD first (faster, less bandwidth)
                async with session.head(
                    url,
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
                    allow_redirects=True,
                    ssl=False,
                ) as resp:
                    # Some servers reject HEAD; fall through to GET on 4xx/5xx
                    if resp.status < 400:
                        return resp.status, "OK"
                    # 405 means HEAD not allowed — retry with GET
                    if resp.status == 405:
                        raise aiohttp.ClientError("HEAD not allowed")
                    return resp.status, resp.reason or "Error"

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass  # fall through to GET

            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
                    allow_redirects=True,
                    ssl=False,
                ) as resp:
                    return resp.status, resp.reason or ("OK" if resp.status < 400 else "Error")

            except asyncio.TimeoutError:
                if attempt < retries:
                    await asyncio.sleep(2)
                    continue
                return None, "Timeout"

            except aiohttp.ClientConnectorError:
                if attempt < retries:
                    await asyncio.sleep(2)
                    continue
                return None, "Connection failed"

            except aiohttp.ClientError as e:
                if attempt < retries:
                    await asyncio.sleep(2)
                    continue
                return None, f"Client error: {e}"

            except OSError as e:
                if attempt < retries:
                    await asyncio.sleep(2)
                    continue
                return None, f"OS error: {e}"

    return None, "Unknown error"


async def check_all_links(link_map):
    """
    link_map: dict  url -> set of filenames that reference it
    Returns list of result dicts.
    """
    semaphore = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, limit_per_host=5)
    headers = {"User-Agent": USER_AGENT}

    results = []
    urls = list(link_map.keys())
    total = len(urls)

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        tasks = {url: asyncio.ensure_future(check_one_url(session, url, semaphore)) for url in urls}

        done = 0
        for url, task in tasks.items():
            status, reason = await task
            done += 1
            if done % 50 == 0 or done == total:
                print(f"  Checked {done}/{total} unique URLs...")

            if status is None or status >= 400:
                for page in sorted(link_map[url]):
                    results.append({
                        "page": page,
                        "url": url,
                        "status": status if status is not None else "N/A",
                        "reason": reason,
                    })

    return results


# --- Main ---
def main():
    print("=" * 60)
    print("AmeriDocs Link Checker")
    print("=" * 60)

    # 1. Collect all HTML files
    html_files = sorted(
        f for f in os.listdir(SITE_DIR)
        if f.endswith(".html")
    )
    print(f"\nFound {len(html_files)} HTML files.")

    # 2. Extract external links and deduplicate
    #    link_map: url -> set of pages that use it
    link_map = {}
    per_page_count = 0
    for fname in html_files:
        fpath = os.path.join(SITE_DIR, fname)
        links = extract_links(fpath)
        external = [l for l in links if is_external(l)]
        per_page_count += len(external)
        for url in external:
            link_map.setdefault(url, set()).add(fname)

    unique = len(link_map)
    print(f"Found {per_page_count} external link references ({unique} unique URLs).")
    print(f"\nChecking links (concurrency={CONCURRENCY}, timeout={TIMEOUT_SECONDS}s)...")
    start = time.time()

    # 3. Run async checks
    results = asyncio.run(check_all_links(link_map))

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s.")

    # 4. Write CSV report
    results.sort(key=lambda r: (r["page"], r["url"]))
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["page", "url", "status", "reason"])
        writer.writeheader()
        writer.writerows(results)

    # 5. Print summary
    broken_urls = {r["url"] for r in results}
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {len(broken_urls)} broken unique URLs found")
    print(f"         ({len(results)} total broken references across pages)")
    print(f"Report saved to: {OUTPUT_CSV}")
    print(f"{'=' * 60}")

    if results:
        # Print a text summary too
        print(f"\n{'—' * 60}")
        current_page = None
        for r in results:
            if r["page"] != current_page:
                current_page = r["page"]
                print(f"\n  {current_page}")
            print(f"    [{r['status']}] {r['reason']}")
            print(f"         {r['url']}")

    return 0 if not results else 1


if __name__ == "__main__":
    sys.exit(main())
