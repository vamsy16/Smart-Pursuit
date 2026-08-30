#!/usr/bin/env python3
"""
sales_automation.py
===============

A free, lightweight local-business lead scraper.

It fetches pages from a business directory, extracts:
    * Company Name
    * Public Email
    * Public Phone
    * Website

...then writes everything into a clean, deduplicated CSV file.

Dependencies
------------
    pip install requests beautifulsoup4

Usage
-----
    # Quick start (edit TARGET_URL in the CONFIG block below first)
    python lead_scraper.py

    # Override the target directory URL from the command line
    python lead_scraper.py --url "https://www.example-directory.com/plumbers"

    # Custom output file and number of pages to crawl
    python lead_scraper.py --url "..." --pages 5 --output leads.csv

How to point it at YOUR directory
---------------------------------
Every directory has different HTML. This script uses a small, central
CONFIG block so you only tweak the CSS selectors in one place:

    LISTING_SELECTOR  -> the repeating "card" element for one business
    NAME_SELECTOR     -> company name (an <h2>/<h3>/<a> typically)
    WEBSITE_SELECTOR  -> link to the business's own site (often a "Visit
                         website" anchor). Leave blank to auto-detect.

If a selector is left blank, the script falls back to sensible
heuristics (mailto: links, tel: links, and regex over the text).

Find the right selectors by opening the directory in your browser,
right-clicking a listing, and choosing "Inspect".

Politeness
----------
* A custom User-Agent is sent so site owners can identify/contact you.
* A small delay between requests (REQUEST_DELAY_SECONDS) avoids hammering
  the server.
* robots.txt is respected when the site publishes one.

Please review the target site's Terms of Service before scraping, and
keep this to publicly-listed business data.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# CONFIG — tweak these to match your target directory.                        #
# --------------------------------------------------------------------------- #
TARGET_URL = ""  # e.g. "https://www.example-directory.com/category/plumbers"

# CSS selectors (BeautifulSoup's select()). Leave "" to auto-detect.
LISTING_SELECTOR = ""   # e.g. "div.listing", "article.search-result"
NAME_SELECTOR = ""      # e.g. "h2.business-name", "h3.listing-title a"
WEBSITE_SELECTOR = ""   # e.g. "a.website", "a[data-test='website']"

# How many directory pages to crawl (set 1 if there is no pagination).
MAX_PAGES = 1

# Be polite: pause between requests.
REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 20

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "(lead-research-bot; contact: your-email@example.com)"
)

# Pagination URL pattern. Use {page} as the placeholder.
# e.g. "https://example-directory.com/plumbers?page={page}"
PAGE_URL_TEMPLATE = ""  # blank = only crawl the base URL
# --------------------------------------------------------------------------- #

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{6,}\d)")

# Domains we never treat as a business's own website.
_IGNORED_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "yelp.com", "google.com", "maps.google.com",
    "g.page", "tripadvisor.com", "pinterest.com", "tiktok.com",
}


def build_urls(base_url: str, max_pages: int, template: str) -> list[str]:
    """Return the list of page URLs to crawl."""
    if max_pages <= 1 or not template:
        return [base_url]

    urls = [base_url]
    for page in range(2, max_pages + 1):
        urls.append(template.format(page=page))
    return urls


def can_fetch(session: requests.Session, url: str) -> bool:
    """Respect robots.txt when the site publishes one."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        # No robots.txt or unreadable -> assume allowed.
        return True


def fetch(session: requests.Session, url: str) -> BeautifulSoup:
    """Fetch a URL and return a parsed BeautifulSoup document."""
    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def clean(text: str | None) -> str:
    """Normalize whitespace in extracted text."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def extract_email(text: str, soup: BeautifulSoup) -> str:
    """Find a public email, preferring explicit mailto: links."""
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if href.startswith("mailto:"):
            match = EMAIL_RE.search(href)
            if match:
                return match.group(0).lower()

    match = EMAIL_RE.search(text)
    return match.group(0).lower() if match else ""


def extract_phone(text: str, soup: BeautifulSoup) -> str:
    """Find a public phone number, preferring explicit tel: links."""
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if href.startswith("tel:"):
            phone = clean(href[4:])
            if phone:
                return phone

    match = PHONE_RE.search(text)
    return clean(match.group(1)) if match else ""


def extract_website(base_url: str, soup: BeautifulSoup) -> str:
    """Find the business's own website, skipping social/map links."""
    base_domain = _domain(base_url)

    # 1) Honour an explicit selector if configured.
    if WEBSITE_SELECTOR:
        node = soup.select_one(WEBSITE_SELECTOR)
        if node:
            href = node.get("href") or (node.find("a") or {}).get("href")
            if href:
                return clean(urljoin(base_url, href))

    # 2) Otherwise scan anchors for a plausible outbound domain link.
    for anchor in soup.find_all("a", href=True):
        href = clean(urljoin(base_url, anchor["href"]))
        if not href.startswith(("http://", "https://")):
            continue
        domain = _domain(href)
        if domain in _IGNORED_DOMAINS or domain == base_domain:
            continue
        return href

    return ""


def parse_listing(listing: BeautifulSoup, base_url: str) -> dict[str, str]:
    """Extract lead fields from a single listing element."""
    text = listing.get_text(" ", strip=True)

    name = ""
    if NAME_SELECTOR:
        node = listing.select_one(NAME_SELECTOR)
        name = clean(node.get_text() if node else "")
    if not name:
        node = listing.find(["h1", "h2", "h3", "h4", "strong"])
        name = clean(node.get_text() if node else "")

    return {
        "Company Name": name or clean(text[:80]),
        "Email": extract_email(text, listing),
        "Phone": extract_phone(text, listing),
        "Website": extract_website(base_url, listing),
    }


def discover_listings(soup: BeautifulSoup) -> list[BeautifulSoup]:
    """Split the page into individual listing elements."""
    if LISTING_SELECTOR:
        return soup.select(LISTING_SELECTOR)

    for selector in (
        "article",
        "div.listing",
        "div.search-result",
        "li[class*='listing']",
        "div[class*='result']",
        "div[class*='card']",
    ):
        found = soup.select(selector)
        if found:
            return found

    return [soup]


def write_csv(rows: list[dict[str, str]], path: str) -> None:
    """Write deduplicated lead rows to a CSV file."""
    fieldnames = ["Company Name", "Email", "Phone", "Website"]
    seen, unique = set(), []
    for row in rows:
        key = tuple(row[f] for f in fieldnames)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(unique)

    print(f"\nSaved {len(unique)} lead(s) -> {path}")


def scrape(url: str, max_pages: int, output: str) -> None:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    if not can_fetch(session, url):
        print("robots.txt disallows this URL. Aborting.", file=sys.stderr)
        sys.exit(1)

    urls = build_urls(url, max_pages, PAGE_URL_TEMPLATE)
    rows: list[dict[str, str]] = []

    for page_url in urls:
        print(f"Fetching {page_url} ...")
        soup = fetch(session, page_url)
        listings = discover_listings(soup)
        print(f"  found {len(listings)} listing(s)")
        for listing in listings:
            rows.append(parse_listing(listing, page_url))
        if len(urls) > 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    write_csv(rows, output)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape local business leads into a CSV.")
    parser.add_argument("--url", default=TARGET_URL,
                        help="Directory URL to scrape.")
    parser.add_argument("--pages", type=int, default=MAX_PAGES,
                        help="Number of pages to crawl.")
    parser.add_argument("--output", default="leads.csv",
                        help="Output CSV filename.")
    args = parser.parse_args()

    if not args.url:
        parser.error(
            "No target URL given. Set TARGET_URL in the CONFIG block, "
            "or pass --url 'https://example-directory.com/...'"
        )

    scrape(args.url, args.pages, args.output)


if __name__ == "__main__":
    main()
