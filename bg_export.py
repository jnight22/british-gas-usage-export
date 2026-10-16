"""Export daily British Gas energy usage to CSV and build a dashboard.

Opens a real browser so you can log in to British Gas, captures the auth
headers the site uses for its own API calls, then fetches month-by-month
daily usage for each meter, writes CSV files and builds dashboard.html.

On the first run your account and meter IDs are detected from the site's own
requests and saved to bg_config.json, so later runs don't need to detect them again.

Usage:
    python bg_export.py                 # asks how much history you want
    python bg_export.py --all           # everything available
    python bg_export.py --months 6      # last 6 months
    python bg_export.py --years 2       # last 2 years
    python bg_export.py --start 2025-07-01 --end 2025-09-30
    python bg_export.py --reconfigure   # detect account and meters again
"""

import argparse
import csv
import json
import re
import sys
import time
import webbrowser
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError, sync_playwright

import dashboard

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "bg_config.json"
API_HOST = "api-account.britishgas.co.uk"
API_URL = f"https://{API_HOST}/v2/accounts/{{account}}/meterpoints/{{mp}}/energy-usage/month"
USAGE_RE = re.compile(r"/v2/accounts/(\d+)/meterpoints/(\d+)/energy-usage")
LOGIN_URL = "https://www.britishgas.co.uk/my-account"
SIGN_IN_HOST = "identity.auth.britishgas.co.uk"
ERROR_PATH = "/technical-error"
SITE_ORIGINS = ("https://www.britishgas.co.uk", f"https://{SIGN_IN_HOST}")
FUELS = ("electricity", "gas")

# Smart meter data lags about a day, so the most recent day is usually incomplete.
DATA_LAG_DAYS = 2
# "All history" walks back month by month until this many consecutive months have no data.
EMPTY_MONTHS_TO_STOP = 3
MAX_HISTORY_YEARS = 15

# Headers that must not be replayed verbatim.
DROP_HEADERS = {"host", "content-length", "cookie", "connection", "accept-encoding"}


# ---- date range ----

def shift_months(d: date, months: int) -> date:
    """Move d by a whole number of months, clamping the day to the target month's length."""
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    m += 1
    next_month = date(y + (m == 12), m % 12 + 1, 1)
    return date(y, m, min(d.day, (next_month - timedelta(days=1)).day))


def ask_int(prompt: str) -> int:
    while True:
        raw = input(prompt).strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print("  Please enter a whole number greater than 0.")


def ask_range(end: date):
    """Interactively choose a start date. Returns None for all history."""
    print("\nHow much history do you want to download?")
    print("  1) All available history")
    print("  2) A number of months")
    print("  3) A number of years")
    while True:
        choice = input("Choice [1]: ").strip() or "1"
        if choice == "1":
            return None
        if choice == "2":
            return shift_months(end, -ask_int("How many months? "))
        if choice == "3":
            return shift_months(end, -12 * ask_int("How many years? "))
        print("  Please enter 1, 2 or 3.")


def resolve_range(args):
    end = args.end or date.today() - timedelta(days=DATA_LAG_DAYS)
    if args.start:
        start = args.start
    elif args.all:
        start = None
    elif args.months:
        start = shift_months(end, -args.months)
    elif args.years:
        start = shift_months(end, -12 * args.years)
    elif sys.stdin.isatty():
        start = ask_range(end)
    else:
        start = None
    print(f"\nRange: {start or 'all available history'} to {end}")
    return start, end


# ---- browser & discovery ----

def launch_browser(p, profile_dir: Path):
    # Prefer installed Chrome and hide the automation flag; some login pages reject
    # the bundled test browser.
    options = dict(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
        no_viewport=True,
    )
    try:
        return p.chromium.launch_persistent_context(profile_dir, channel="chrome", **options)
    except PlaywrightError:
        print("Installed Chrome not found, using Playwright's Chromium.")
        return p.chromium.launch_persistent_context(profile_dir, **options)


class ApiWatcher:
    """Watches the site's own API calls for auth headers and the meters it asks about."""

    def __init__(self, context):
        self.headers = {}
        self.meters = {}  # (account id, meterpoint id) -> isTimeOfUse
        context.on("request", self._on_request)

    def _on_request(self, request):
        if API_HOST not in request.url or request.method == "OPTIONS":
            return
        headers = request.headers
        if not self.headers and "authorization" in headers:
            self.headers = {k: v for k, v in headers.items()
                            if not k.startswith(":") and k.lower() not in DROP_HEADERS}
        match = USAGE_RE.search(request.url)
        if match:
            query = parse_qs(urlparse(request.url).query)
            tou = query.get("isTimeOfUse", ["false"])[0].lower() == "true"
            key = match.groups()
            self.meters[key] = self.meters.get(key, False) or tou


def wait_until(page, condition, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while not condition():
        if time.time() > deadline:
            return False
        page.wait_for_timeout(1000)
    return True


def reset_site_data(context, page):
    """Forget the saved British Gas login: all cookies plus the site's local storage."""
    context.clear_cookies()
    cdp = context.new_cdp_session(page)
    for origin in SITE_ORIGINS:
        cdp.send("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"})
    cdp.detach()


def open_login(context, page, watcher: ApiWatcher):
    """Open the account page, recovering if a stale saved login sends us to the error page."""
    for attempt in range(2):
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        # Wait to see where the site sends us: the sign-in page, its error page,
        # or straight into the account if the saved login is still valid.
        wait_until(page, lambda: SIGN_IN_HOST in page.url or ERROR_PATH in page.url or watcher.headers, 30)
        if ERROR_PATH not in page.url:
            return
        if attempt == 0:
            print("British Gas showed its 'technical error' page, which usually means an expired saved login.")
            print("Clearing the saved login and trying again...")
            reset_site_data(context, page)
    print("British Gas is still showing its error page; the site may be having problems. Try again later.")


def ask_fuel(mp: str, tou: bool) -> str:
    default = "electricity" if tou else "gas"
    while True:
        raw = input(f"  Meter {mp} (time-of-use: {str(tou).lower()}) - electricity or gas? [{default}]: ").strip().lower()
        if not raw:
            return default
        for fuel in FUELS:
            if fuel.startswith(raw):
                return fuel
        print("  Please type electricity or gas (or e / g).")


def discover_config(page, watcher: ApiWatcher, timeout_s: int) -> dict:
    if not sys.stdin.isatty():
        sys.exit(f"No {CONFIG_PATH.name} found. Run once in a terminal so the meters can be detected.")
    print("\n>>> First run: detecting your account and meters.")
    print(">>> After logging in, open your energy usage page in the browser.")
    if not wait_until(page, lambda: watcher.meters, timeout_s):
        sys.exit("Didn't see the site load any energy usage data. Run again and open the usage page after logging in.")
    page.wait_for_timeout(3000)  # let the page finish loading any other fuels

    print("\nFound meters: " + ", ".join(mp for _, mp in sorted(watcher.meters)))
    input(">>> If a fuel is missing, switch to it on the usage page in the browser, then press Enter here... ")
    page.wait_for_timeout(1500)

    accounts = sorted({acct for acct, _ in watcher.meters})
    account = accounts[0]
    if len(accounts) > 1:
        print("More than one account found:")
        for i, acct in enumerate(accounts, 1):
            print(f"  {i}) {acct}")
        account = accounts[min(ask_int("Which account? "), len(accounts)) - 1]

    print("\nWhich fuel is each meter? (Time-of-use meters are almost always electricity.)")
    meters = [{"id": mp, "fuel": ask_fuel(mp, tou), "time_of_use": tou}
              for (acct, mp), tou in sorted(watcher.meters.items()) if acct == account]
    fuels = [m["fuel"] for m in meters]
    if len(set(fuels)) < len(fuels):
        print("Warning: two meters share a fuel; the dashboard will add them together.")

    config = {"account_id": account, "meters": meters}
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    print(f"Saved to {CONFIG_PATH.name}. Delete it or run with --reconfigure to detect again.")
    return config


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return None


# ---- fetching ----

def fetch_month(context, headers: dict, account: str, meter: dict, m: date, raw_dir: Path):
    resp = context.request.get(
        API_URL.format(account=account, mp=meter["id"]),
        params={"startDate": m.isoformat(), "isTimeOfUse": str(meter["time_of_use"]).lower()},
        headers=headers,
    )
    label = f"{meter['fuel']:<11} {m:%Y-%m}"
    if not resp.ok:
        print(f"FAILED {label}: HTTP {resp.status} {resp.text()[:300]}")
        redacted = {k: (v[:12] + "...") if k.lower() == "authorization" else v
                    for k, v in headers.items()}
        (raw_dir / "captured_headers_redacted.json").write_text(json.dumps(redacted, indent=2))
        return None
    data = resp.json()
    (raw_dir / f"{meter['id']}_{m:%Y-%m}.json").write_text(json.dumps(data, indent=2))
    days = sum(1 for dp in data.get("dataPoints", []) if dp.get("hasData"))
    print(f"OK     {label}: {days} days with data")
    return data


def fetch_range(context, headers: dict, config: dict, start, end: date, raw_dir: Path) -> dict:
    """Fetch months from end backwards to start. start=None walks back until data runs out."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    m = end.replace(day=1)
    oldest = start.replace(day=1) if start else shift_months(m, -12 * MAX_HISTORY_YEARS)
    empty_run = 0
    while m >= oldest:
        month_has_data = False
        for meter in config["meters"]:
            data = fetch_month(context, headers, config["account_id"], meter, m, raw_dir)
            if data:
                results[(meter["id"], m)] = data
                month_has_data |= any(dp.get("hasData") for dp in data.get("dataPoints", []))
        if start is None:
            empty_run = 0 if month_has_data else empty_run + 1
            if empty_run >= EMPTY_MONTHS_TO_STOP:
                print(f"No data for {EMPTY_MONTHS_TO_STOP} months in a row; reached the start of your history.")
                break
        m = shift_months(m, -1)
    return results


# ---- CSV ----

def flatten(results: dict, config: dict, start, end: date, include_empty: bool):
    start = start or date.min
    fuel_of = {meter["id"]: meter["fuel"] for meter in config["meters"]}
    rows = []
    for (mp, _), data in sorted(results.items()):
        for dp in data.get("dataPoints", []):
            day = date.fromisoformat(dp["datetime"][:10])
            if not (start <= day <= end) or (not dp.get("hasData") and not include_empty):
                continue
            row = {
                "date": day.isoformat(),
                "meterpoint": mp,
                "fuel": fuel_of[mp],
                "has_data": dp.get("hasData"),
                "standing_charge": dp.get("standingCharge", 0),
            }
            # standardRate, peakRate, offPeakRate, ... -> standard_kwh / standard_cost etc.
            for key, val in dp.items():
                if key.endswith("Rate") and isinstance(val, dict):
                    name = key[:-4].lower()
                    row[f"{name}_kwh"] = val.get("unit")
                    row[f"{name}_cost"] = val.get("cost")
            # standardRate is the day's total; peak/off-peak are a breakdown of it.
            row["total_cost"] = round((row.get("standard_cost") or 0) + (row["standing_charge"] or 0), 2)
            rows.append(row)
    return rows


def write_csv(path: Path, rows: list):
    base = ["date", "meterpoint", "fuel", "has_data", "standing_charge",
            "standard_kwh", "standard_cost", "peak_kwh", "peak_cost",
            "offpeak_kwh", "offpeak_cost", "total_cost"]
    extra = sorted({k for r in rows for k in r} - set(base))
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=base + extra)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows -> {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--all", action="store_true", help="download all available history")
    span.add_argument("--months", type=int, help="download the last N months")
    span.add_argument("--years", type=int, help="download the last N years")
    span.add_argument("--start", type=date.fromisoformat, help="start date, YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat,
                        help=f"end date, YYYY-MM-DD (default: {DATA_LAG_DAYS} days ago, the last complete day)")
    parser.add_argument("--out", type=Path, default=HERE / "output")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for login")
    parser.add_argument("--include-empty", action="store_true", help="keep days with hasData=false")
    parser.add_argument("--no-dashboard", action="store_true", help="skip building and opening dashboard.html")
    parser.add_argument("--reconfigure", action="store_true", help=f"ignore {CONFIG_PATH.name} and detect meters again")
    args = parser.parse_args()

    config = None if args.reconfigure else load_config()
    start, end = resolve_range(args)
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = launch_browser(p, HERE / ".browser-profile")  # keeps you logged in between runs
        page = context.pages[0] if context.pages else context.new_page()
        watcher = ApiWatcher(context)
        open_login(context, page, watcher)
        if watcher.headers:
            print("\n>>> Already logged in.")
        else:
            print("\n>>> Log in to British Gas in the browser window (email first, then password).")

        if config is None:
            config = discover_config(page, watcher, args.timeout)
        else:
            print(">>> Once logged in, open your energy usage page if the script doesn't continue on its own.")
            wait_until(page, lambda: watcher.headers, args.timeout)

        if watcher.headers:
            print(f"Captured API headers: {sorted(watcher.headers)}")
        else:
            print("No authorised API request seen before timeout; trying with browser cookies only.")
        results = fetch_range(context, watcher.headers, config, start, end, args.out / "raw")
        context.close()

    rows = flatten(results, config, start, end, args.include_empty)
    combined = args.out / "british_gas_usage.csv"
    write_csv(combined, rows)
    for fuel in sorted({m["fuel"] for m in config["meters"]}):
        write_csv(args.out / f"{fuel}.csv", [r for r in rows if r["fuel"] == fuel])

    if not args.no_dashboard and rows:
        html = dashboard.build(combined, args.out / "dashboard.html")
        print(f"Dashboard -> {html}")
        webbrowser.open(html.resolve().as_uri())


if __name__ == "__main__":
    main()
