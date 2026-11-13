"""Daily collector of NIMH next-day temperature forecasts and observations.

Run once a day at about 13:30 Sofia time (GitHub Actions does this, see
.github/workflows/daily.yml). Each run:

1. Downloads NIMH's forecasts from meteo.bg (city table, mountain table,
   seaside table) and archives the raw HTML and the forecast map images.
2. Adds the forecast for tomorrow to data/days.json and data/forecasts.csv.
   Only the next-day forecast is collected and checked.
3. Downloads Stringmeteo's monthly extremes and fills in the observations
   for every past day in data/days.json that still has none.

Everything is idempotent: running it twice on the same day changes nothing.

Usage:
    python scripts/collect.py                 # normal daily run
    python scripts/collect.py --offline tests/fixtures --today 2026-11-08
                                              # test with saved pages
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# Optional: cloudscraper is more reliable against SuperJS / bot checks.
try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
except ImportError:
    _HAS_CLOUDSCRAPER = False

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ARCHIVE = ROOT / "archive"
TZ = ZoneInfo("Europe/Sofia")

URLS = {
    "gradove": "https://meteo.bg/bg/forecast/gradove",
    "planini": "https://meteo.bg/bg/forecast/planini",
    "moreto": "https://meteo.bg/bg/forecast/moreto",
}
MAP_URL = "https://info.meteo.bg/i/meteo/png/0/PrognozaBG_{n}.png"  # 1 = tomorrow
LEADS = (1,)  # days ahead to collect: next-day forecast only
STRINGMETEO_URL = "https://www.stringmeteo.com/synop/maxmin_month.php?month={m}&year={y}"

# Browser-like headers (needed for Stringmeteo SuperJS check)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "bg-BG,bg;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Station id -> where its forecast comes from and its name on Stringmeteo.
# "src" is the meteo.bg table and the row name in it.
STATIONS = {
    "vidin": {"src": ("gradove", "Видин"), "obs": "Видин"},
    "pleven": {"src": ("gradove", "Плевен"), "obs": "Плевен"},
    "ruse": {"src": ("gradove", "Русе"), "obs": "Русе"},
    "silistra": {"src": ("gradove", "Силистра"), "obs": "Силистра"},
    "vtarnovo": {"src": ("gradove", "В. Търново"), "obs": "В. Търново"},
    "varna": {"src": ("gradove", "Варна"), "obs": "Варна"},
    "sofia": {"src": ("gradove", "София"), "obs": "София"},
    "burgas": {"src": ("gradove", "Бургас"), "obs": "Бургас"},
    "plovdiv": {"src": ("gradove", "Пловдив"), "obs": "Пловдив"},
    "sandanski": {"src": ("gradove", "Сандански"), "obs": "Сандански"},
    "kardzhali": {"src": ("gradove", "Кърджали"), "obs": "Кърджали"},
    "botev": {"src": ("planini", "вр. Ботев"), "obs": "вр. Ботев"},
    "murgash": {"src": ("planini", "вр. Мургаш"), "obs": "вр. Мургаш"},
    "cherni": {"src": ("planini", "Черни връх"), "obs": "Черни вр."},
    "musala": {"src": ("planini", "вр. Мусала"), "obs": "вр. Мусала"},
    "rozhen": {"src": ("planini", "Рожен"), "obs": "Рожен"},
    "shabla": {"src": ("moreto", "н. Шабла"), "obs": "н. Шабла"},
    "kaliakra": {"src": ("moreto", "н. Калиакра"), "obs": "н. Калиакра"},
    "ahtopol": {"src": ("moreto", "Ахтопол"), "obs": "Ахтопол"},
}

MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------- download

_SESSION = None


def _make_session():
    """Create a session that can pass simple SuperJS / bot checks."""
    if _HAS_CLOUDSCRAPER:
        s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    else:
        s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get_session():
    global _SESSION
    if _SESSION is None:
        _SESSION = _make_session()
    return _SESSION


def decode(raw: bytes, header_charset: str | None) -> str:
    """Decode a page: charset from the HTTP header, else from the page's own
    <meta> tag, else UTF-8, else Windows-1251 (common on Bulgarian sites)."""
    candidates = []
    if header_charset and header_charset.lower() != "iso-8859-1":
        candidates.append(header_charset)
    m = re.search(rb"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", raw[:4000])
    if m:
        candidates.append(m.group(1).decode("ascii"))
    candidates += ["utf-8", "windows-1251"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_html(name: str, url: str, offline: Path | None) -> str:
    if offline:
        return decode((offline / f"{name}.html").read_bytes(), None)
    # meteo.bg answers these pages with HTTP 404 but a full page, so the
    # status code is not checked; the parsers check that a table is there.
    r = get_session().get(url, timeout=60)
    m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""))
    print(f"{name}: HTTP {r.status_code}, {len(r.content)} bytes, {r.headers.get('Content-Type')}")
    return decode(r.content, m.group(1) if m else None)


def fetch_map(n: int) -> bytes | None:
    # The image is only served with a meteo.bg referer.
    h = dict(HEADERS, Referer="https://meteo.bg/bg/forecast/dnesUtre")
    try:
        r = get_session().get(MAP_URL.format(n=n), headers=h, timeout=60)
    except requests.RequestException as e:
        print(f"map {n}: {e}")
        return None
    if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
        print(f"map {n}: HTTP {r.status_code}, not saved")
        return None
    return r.content


# ---------------------------------------------------------------- parsing

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
PAIR_RE = re.compile(r"^\s*(-?\d+)\s*/\s*(-?\d+)\s*$")


def cell_texts(tr) -> list[str]:
    return [" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["td", "th"])]


def parse_forecast_table(html: str) -> dict[str, dict[str, tuple[int, int]]]:
    """Parse a meteo.bg forecast table (cities, mountains or seaside).

    Returns {iso_date: {row_name: (tmin, tmax)}}. The header row lists the
    dates; each data row has, per date, a "tmin / tmax" cell followed by one
    or two other cells (weather, snow cover, sea temperature), so the values
    are taken in order from the cells that look like "a / b".
    """
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        dates = [f"{y}-{m}-{d}" for d, m, y in DATE_RE.findall(" ".join(cell_texts(rows[0])))]
        if len(dates) < 2:
            continue
        out: dict[str, dict[str, tuple[int, int]]] = {d: {} for d in dates}
        for tr in rows[1:]:
            cells = cell_texts(tr)
            if len(cells) < 2 or not cells[0]:
                continue
            name = cells[0].rstrip("*").strip()
            pairs = [PAIR_RE.match(c) for c in cells[1:]]
            pairs = [(int(p.group(1)), int(p.group(2))) for p in pairs if p]
            for date, pair in zip(dates, pairs):
                out[date][name] = pair
        return out
    raise ValueError("no forecast table with dates found")


def num(s: str) -> float | None:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return None


TAG_RE = re.compile(r"<[^>]+>")
CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)(?=<t[dh]\b|</tr|$)", re.S | re.I)


def clean(fragment: str) -> str:
    text = TAG_RE.sub(" ", fragment).replace("&nbsp;", " ").replace("&minus;", "-")
    return " ".join(text.split())


def parse_stringmeteo(html: str) -> dict[str, dict[int, list[float | None]]]:
    """Parse Stringmeteo's monthly extremes page.

    Returns {station: {day: [tmin, tmax]}}. Each station has a "Макс." row and
    a "Мин." row; each row holds days 1-15, then the label again and days
    16-31, then the label again and the monthly summary. The page is split on
    "<tr" directly, so it does not matter whether its tags are closed properly.
    """
    out: dict[str, dict[int, list[float | None]]] = {}
    station = None
    for chunk in re.split(r"<tr\b", html, flags=re.I)[1:]:
        texts = [clean(c) for c in CELL_RE.findall(chunk)]
        labels = [i for i, t in enumerate(texts) if t.startswith(("Макс", "Мин"))]
        if not labels:
            continue
        i0 = labels[0]
        if i0 >= 1 and texts[i0 - 1] and not re.fullmatch(r"\[?\d+\]?\.?", texts[i0 - 1]):
            station = texts[i0 - 1]
        if station is None:
            continue
        k = 1 if texts[i0].startswith("Макс") else 0
        days = out.setdefault(station, {})
        block1 = texts[i0 + 1:i0 + 16]
        block2 = texts[labels[1] + 1:labels[2]] if len(labels) >= 3 else []
        for day, t in list(enumerate(block1, 1)) + list(enumerate(block2, 16)):
            v = num(t)
            if v is not None:
                days.setdefault(day, [None, None])[k] = v
    if not out:
        visible = clean(html)
        raise ValueError(
            "no station rows found on the Stringmeteo page "
            f"({len(html)} characters, {html.lower().count('<tr')} rows, "
            f"'Макс' appears {html.count('Макс')} times). Page starts with: {visible[:300]!r}"
        )
    return out


# ---------------------------------------------------------------- data files

def load_days() -> list[dict]:
    p = DATA / "days.json"
    days = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    return [e for e in days if e.get("lead") in LEADS]


def save_days(days: list[dict]) -> None:
    days.sort(key=lambda e: (e["d"], e["lead"]))
    (DATA / "days.json").write_text(json.dumps(days, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def append_csv(name: str, header: list[str], rows: list[list]) -> None:
    p = DATA / name
    new = not p.exists()
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def short_date(d: dt.date, lang: str) -> str:
    return f"{d:%d.%m}" if lang == "bg" else f"{d.day} {MONTHS_EN[d.month - 1]}"


# ---------------------------------------------------------------- steps

def capture_forecasts(today: dt.date, now: dt.datetime, days: list[dict], offline: Path | None) -> bool:
    tables = {}
    for name, url in URLS.items():
        html = fetch_html(name, url, offline)
        tables[name] = parse_forecast_table(html)
        if not offline:
            raw = ARCHIVE / "raw" / today.isoformat()
            raw.mkdir(parents=True, exist_ok=True)
            (raw / f"{name}.html").write_text(html, encoding="utf-8")

    first = min(tables["gradove"])
    if first != today.isoformat():
        print(f"meteo.bg not updated yet: city table starts at {first}, expected {today}")
        return False

    if not offline:
        for n in LEADS:
            png = fetch_map(n)
            if png:
                d = ARCHIVE / "maps"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{today.isoformat()}_PrognozaBG_{n}.png").write_bytes(png)

    hhmm = f"{now:%H:%M}"
    csv_rows = []
    for lead in LEADS:
        valid = (today + dt.timedelta(days=lead)).isoformat()
        f = {}
        for sid, st in STATIONS.items():
            table, row = st["src"]
            v = tables[table].get(valid, {}).get(row)
            if v:
                f[sid] = list(v)
        # every published value goes to the CSV, including towns we cannot verify
        for table, by_date in tables.items():
            for row, (lo, hi) in by_date.get(valid, {}).items():
                csv_rows.append([today.isoformat(), valid, lead, table, row, lo, hi, now.isoformat(timespec="minutes")])
        if not f:
            continue
        entry = {
            "d": valid, "iss": today.isoformat(), "lead": lead, "src": "live",
            "cap": f"{hhmm} на {short_date(today, 'bg')} (автоматично)",
            "capEn": f"{hhmm} on {short_date(today, 'en')} (automatic)",
            "f": f, "o": {},
        }
        old = next((e for e in days if e["d"] == valid and e["iss"] == entry["iss"] and e["lead"] == lead), None)
        if old:
            continue  # already captured today; keep the first capture
        days.append(entry)
        print(f"added forecast for {valid} (lead {lead}): {len(f)} stations")
    if csv_rows:
        append_csv("forecasts.csv", ["issued", "valid", "lead", "table", "point", "tmin", "tmax", "captured"], csv_rows)
    return True


def fill_observations(today: dt.date, days: list[dict], offline: Path | None) -> None:
    pending = [e for e in days if dt.date.fromisoformat(e["d"]) < today and not e.get("o")]
    months = sorted({e["d"][:7] for e in pending})
    pages = {}
    for ym in months:
        y, m = map(int, ym.split("-"))
        html = fetch_html("stringmeteo", STRINGMETEO_URL.format(m=m, y=y), offline)
        # Quick guard against SuperJS challenge / empty replies
        if "SuperJS" in html or len(html) < 2000:
            raise RuntimeError(
                "Stringmeteo returned a challenge / empty page instead of data. "
                "Install cloudscraper (`pip install cloudscraper`) or update headers."
            )
        pages[ym] = parse_stringmeteo(html)
    obs_rows = []
    for e in pending:
        d = dt.date.fromisoformat(e["d"])
        page = pages[e["d"][:7]]
        o = {}
        for sid in e["f"]:
            v = page.get(STATIONS[sid]["obs"], {}).get(d.day)
            o[sid] = v if v else [None, None]
        complete = sum(1 for v in o.values() if None not in v)
        # Wait until most stations have both values: the maximum arrives late.
        if complete < max(1, int(0.8 * len(o))):
            print(f"{e['d']}: observations not complete yet ({complete}/{len(o)})")
            continue
        e["o"] = o
        obs_rows += [[e["d"], sid, v[0], v[1]] for sid, v in o.items()]
        print(f"filled observations for {e['d']} (lead {e['lead']})")
    if obs_rows:
        seen = set()
        p = DATA / "observations.csv"
        if p.exists():
            with p.open(encoding="utf-8") as f:
                seen = {(r[0], r[1]) for r in csv.reader(f)}
        append_csv("observations.csv", ["date", "station", "tmin", "tmax"],
                   [r for r in obs_rows if (r[0], r[1]) not in seen])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", type=Path, help="read saved pages from this folder instead of the web")
    ap.add_argument("--today", help="pretend today is this date (YYYY-MM-DD)")
    args = ap.parse_args()

    now = dt.datetime.now(TZ)
    today = dt.date.fromisoformat(args.today) if args.today else now.date()
    DATA.mkdir(exist_ok=True)

    days = load_days()
    ok = capture_forecasts(today, now, days, args.offline)
    save_days(days)
    fill_observations(today, days, args.offline)
    save_days(days)
    if not ok:
        print("Forecasts not captured on this run; the later run today will try again.")
    return 0  # real errors (site down, layout changed) raise and fail the run


if __name__ == "__main__":
    sys.exit(main())