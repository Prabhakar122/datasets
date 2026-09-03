"""
Kalimati Market daily price scraper
------------------------------------
Pulls daily wholesale fruit/vegetable prices from kalimatimarket.gov.np
by replaying the same POST request its own /price date-picker uses.

Usage:
    python kalimati_price_scraper.py --start 2024-01-01 --end 2026-09-02 --out kalimati_prices.csv

Notes:
- Dates use the Gregorian (A.D.) calendar (YYYY-MM-DD), as expected by the site.
- One request per day. Be polite: keep the sleep delay, don't parallelize hard.
- This is an unofficial method reverse-engineered from the site's own public
  source code (github.com/coderkoala/kalimati) - not a documented API.
  It may break if the site changes.
- Flaky connections (e.g. RemoteDisconnected) are retried with exponential
  backoff. Any date that still fails after all per-request retries is
  collected and retried once more in a final cleanup pass at the end.
"""

import argparse
import csv
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

BASE = "https://kalimatimarket.gov.np"
PRICE_URL = f"{BASE}/price"
LANG_EN_URL = f"{BASE}/lang/en"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


def start_session() -> requests.Session:
    """Create a session, switch locale to English, and return it."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Switch session locale to English so commodity names come back in English.
    session.get(LANG_EN_URL, timeout=15)
    return session


def get_csrf_token(session: requests.Session) -> str:
    resp = session.get(PRICE_URL, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.find("input", {"name": "_token"})
    if not token_input:
        raise RuntimeError("Could not find CSRF token on /price page.")
    return token_input["value"]


def post_with_retries(session: requests.Session, token: str, date_str: str,
                       retries: int = 3, backoff: float = 2.0) -> requests.Response:
    """POST the date-pricing request, retrying on transient connection errors."""
    payload = {"_token": token, "datePricing": date_str}
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return session.post(PRICE_URL, data=payload, timeout=15)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                wait = backoff * (2 ** attempt)  # 2s, 4s, 8s, ...
                print(f"    retry {attempt + 1}/{retries} for {date_str} "
                      f"after error: {exc!r} (waiting {wait:.0f}s)")
                time.sleep(wait)
    # All retries exhausted - bubble the last error up to the caller.
    raise last_exc


def fetch_price_for_date(session: requests.Session, token: str, date_str: str,
                          retries: int = 3, backoff: float = 2.0):
    resp = post_with_retries(session, token, date_str, retries=retries, backoff=backoff)
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", {"id": "commodityPriceParticular"})
    rows = []
    if table and table.find("tbody"):
        for tr in table.find("tbody").find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) == 5:
                rows.append(cells)  # [commodity, unit, min, max, avg]
    return rows


def daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def scrape(start: date, end: date, out_path: str, delay: float = 1.0,
           retries: int = 3, backoff: float = 2.0):
    session = start_session()
    token = get_csrf_token(session)

    failed_dates = []

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "commodity", "unit", "min_price", "max_price", "avg_price"])

        for day in daterange(start, end):
            date_str = day.strftime("%Y-%m-%d")
            try:
                rows = fetch_price_for_date(session, token, date_str, retries=retries, backoff=backoff)
                for r in rows:
                    writer.writerow([date_str, *r])
                print(f"{date_str}: {len(rows)} commodities")
            except Exception as exc:
                print(f"{date_str}: FAILED after {retries} retries ({exc})")
                failed_dates.append(date_str)
            time.sleep(delay)

        # Final cleanup pass: give any still-failed dates one more shot with a
        # fresh session/token, in case the original session had gone stale.
        if failed_dates:
            print(f"\nRetrying {len(failed_dates)} date(s) that failed earlier: {failed_dates}")
            session = start_session()
            token = get_csrf_token(session)
            still_failed = []

            for date_str in failed_dates:
                try:
                    rows = fetch_price_for_date(session, token, date_str, retries=retries, backoff=backoff)
                    for r in rows:
                        writer.writerow([date_str, *r])
                    print(f"{date_str}: recovered, {len(rows)} commodities")
                except Exception as exc:
                    print(f"{date_str}: FAILED again ({exc})")
                    still_failed.append(date_str)
                time.sleep(delay)

            if still_failed:
                print(f"\nStill missing after cleanup pass: {still_failed}")
            else:
                print("\nAll previously failed dates recovered.")


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape Kalimati Market daily prices.")
    parser.add_argument("--start", type=parse_date, required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=parse_date, required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--out", type=str, default="kalimati_prices.csv", help="Output CSV path")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between requests")
    parser.add_argument("--retries", type=int, default=3, help="Retries per request on connection errors")
    parser.add_argument("--backoff", type=float, default=2.0, help="Base seconds for exponential backoff")
    args = parser.parse_args()

    scrape(args.start, args.end, args.out, args.delay, args.retries, args.backoff)
    print(f"Done. Saved to {args.out}")