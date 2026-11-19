"""The data layer. Everything the agents can look up lives here.

  Google Trends, interest over time   data/trends.parquet + data/verdicts.parquet (scored offline)
  Google Trends, top / rising queries data/rising.parquet
  The closet                          data/closet.csv (synthetic: 60 items, generated for this project)
  Dress codes                         data/dress_codes.csv (hand-built, each row cites a guide or says it can't)
  Vibes                               data/vibes.csv (15 aesthetics: key pieces, colours, details; cites Wikipedia)
  Weather                             Open-Meteo, live: a forecast if the date is close, else
                                      multi-year normals; data/climate_fallback.csv if offline

All parquet is built by build/build.py and committed. Nothing here calls Google.
"""

import csv
import functools
from datetime import date, datetime
from pathlib import Path

import duckdb
import httpx

DATA = Path(__file__).parent / "data"
TIMEOUT = 20  # five years of dailies can take a few seconds

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


@functools.cache
def db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for name in ("trends", "verdicts", "rising"):
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{(DATA / (name + '.parquet')).as_posix()}')")
    con.execute(f"CREATE VIEW closet AS SELECT * FROM read_csv_auto('{(DATA / 'closet.csv').as_posix()}', all_varchar=false)")
    con.execute(f"CREATE VIEW vibes AS SELECT * FROM read_csv_auto('{(DATA / 'vibes.csv').as_posix()}')")
    con.execute(f"CREATE VIEW dress_codes AS SELECT * FROM read_csv_auto('{(DATA / 'dress_codes.csv').as_posix()}')")
    return con


def _rows(sql: str, params: list | None = None) -> list[dict]:
    cur = db().cursor().execute(sql, params or [])  # a cursor per call: requests run in threads
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, (str(v) if isinstance(v, (date, datetime)) else v for v in row)))
            for row in cur.fetchall()]


# --- Google Trends: interest over time ---


def tracked_terms() -> list[dict]:
    """Every term we have five years of search interest for, with its verdict."""
    return _rows("SELECT term, verdict FROM verdicts ORDER BY term")


def find_terms(query: str) -> list[dict]:
    words = [w for w in query.lower().replace("-", " ").split() if len(w) > 2] or [query.lower()]
    hits = [r for r in tracked_terms() if any(w in r["term"].replace("-", " ") for w in words)]
    return hits


def verdict(term: str) -> dict | None:
    rows = _rows("SELECT * EXCLUDE (batch) FROM verdicts WHERE term = lower(?)", [term])
    if not rows:
        return None
    row = rows[0]
    row["peak_month"] = MONTHS[int(row["peak_month"]) - 1]
    row["note"] = ("Values are Google Trends relative interest (0-100 within one download). "
                   "Months flagged as anomalies were left out of the arithmetic.")
    return row


def series(term: str) -> list[dict]:
    return _rows("SELECT strftime(month, '%Y-%m') AS month, interest FROM trends "
                 "WHERE term = lower(?) ORDER BY month", [term])


# --- Google Trends: top and rising related queries ---


def rising(query: str = "", category: str = "", kind: str = "rising", limit: int = 15) -> list[dict]:
    """Top or rising queries, noise removed. Optional filters on the query text and category."""
    return _rows(
        """
        SELECT query, increase, interest, category, window_start, window_end
        FROM rising
        WHERE NOT noise AND kind = ?
          AND query ILIKE '%' || ? || '%'
          AND category ILIKE '%' || ? || '%'
        ORDER BY increase_pct DESC NULLS LAST
        LIMIT ?
        """,
        [kind, query, category, limit],
    )


def rising_categories() -> list[str]:
    return [r["category"] for r in _rows("SELECT DISTINCT category FROM rising ORDER BY 1")]


# --- The closet ---


def closet(category: str = "", max_warmth: int = 5, min_warmth: int = 0, min_formality: int = 0,
           max_formality: int = 5, season: str = "", occasion: str = "", text: str = "",
           vibe: str = "") -> list[dict]:
    """Closet items matching every filter given. Each row carries trend_term when the item maps to
    a Google Trends term we track, plus that term's verdict."""
    return _rows(
        """
        SELECT c.*, v.verdict AS trend_verdict
        FROM closet c LEFT JOIN verdicts v ON v.term = c.trend_term
        WHERE category ILIKE '%' || ? || '%'
          AND warmth BETWEEN ? AND ? AND formality BETWEEN ? AND ?
          AND (seasons ILIKE '%' || ? || '%' OR seasons = 'all')
          AND (occasions ILIKE '%' || ? || '%' OR occasions = 'all')
          AND vibes ILIKE '%' || ? || '%'
          AND (item ILIKE '%' || ? || '%' OR fabric ILIKE '%' || ? || '%'
               OR colour ILIKE '%' || ? || '%' OR coalesce(trend_term, '') ILIKE '%' || ? || '%')
        ORDER BY id
        """,
        [category, min_warmth, max_warmth, min_formality, max_formality, season, occasion, vibe,
         text, text, text, text],
    )


def closet_item(item_id: str) -> dict | None:
    rows = _rows("SELECT c.*, v.verdict AS trend_verdict FROM closet c "
                 "LEFT JOIN verdicts v ON v.term = c.trend_term WHERE id = lower(?)", [item_id])
    return rows[0] if rows else None


def closet_summary() -> dict:
    """Counts by category and the occasions the closet covers, so gaps are visible."""
    cats = _rows("SELECT category, count(*) AS n FROM closet GROUP BY 1 ORDER BY 2 DESC")
    occ = _rows("SELECT unnest(string_split(occasions, '|')) AS occasion, count(*) AS n "
                "FROM closet GROUP BY 1 ORDER BY 2 DESC")
    return {"items": sum(c["n"] for c in cats), "by_category": cats, "occasions": occ}


# --- Vibes ---


def vibe(name: str) -> dict | None:
    """One aesthetic by name or alias, e.g. 'punk', 'riot grrrl', 'quiet luxury'."""
    q = name.strip().lower()
    for row in _rows("SELECT * FROM vibes"):
        if q == row["vibe"] or q in row["aliases"].split("|"):
            return row
    for row in _rows("SELECT * FROM vibes"):  # looser: 'punk themed' -> punk
        if row["vibe"] in q or any(a in q for a in row["aliases"].split("|")):
            return row
    return None


def vibe_names() -> list[str]:
    return [r["vibe"] for r in _rows("SELECT vibe FROM vibes ORDER BY vibe")]


# --- Dress codes ---


def dress_codes() -> list[dict]:
    return _rows("SELECT * FROM dress_codes ORDER BY formality DESC")


# --- Weather ---

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CLIMATE_YEARS = 5


def _geocode(place: str) -> dict | None:
    r = httpx.get(GEOCODE_URL, params={"name": place.split(",")[0].strip(), "count": 1}, timeout=TIMEOUT)
    r.raise_for_status()
    hits = r.json().get("results")
    if not hits:
        return None
    h = hits[0]
    return {"name": f"{h['name']}, {h.get('admin1', '')} {h.get('country', '')}".replace("  ", " ").strip(", "),
            "lat": h["latitude"], "lon": h["longitude"]}


def weather(place: str, when: str) -> dict:
    """when is an ISO date (YYYY-MM-DD). Within 15 days: the live forecast for that day.
    Further out: the average for that month over the last five years. Fahrenheit is added to
    every answer, since the page and the receipts show °F first."""
    w = _weather(place, when)
    for k in ("high", "low"):
        if w.get(f"{k}_c") is not None:
            w[f"{k}_f"] = round(w[f"{k}_c"] * 9 / 5 + 32)
    return w


def _weather(place: str, when: str) -> dict:
    try:
        day = date.fromisoformat(when)
    except ValueError:
        return {"error": f"Could not read {when!r} as a date. Use YYYY-MM-DD."}
    try:
        loc = _geocode(place)
        if loc is None:
            return {"error": f"Open-Meteo has no place called {place!r}."}
        if 0 <= (day - date.today()).days <= 15:
            r = httpx.get(FORECAST_URL, timeout=TIMEOUT, params={
                "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
                "start_date": day.isoformat(), "end_date": day.isoformat()})
            r.raise_for_status()
            d = r.json()["daily"]
            return {"place": loc["name"], "date": day.isoformat(), "kind": "forecast",
                    "high_c": d["temperature_2m_max"][0], "low_c": d["temperature_2m_min"][0],
                    "rain_chance_pct": d["precipitation_probability_max"][0],
                    "wind_max_kmh": d["wind_speed_10m_max"][0],
                    "source": "Open-Meteo forecast API"}
        return _normals(loc, day.month)
    except Exception as e:  # network trouble: fall back to the committed table
        row = _fallback().get((place.lower().split(",")[0].strip(), day.month))
        if row is None:
            return {"error": f"Weather service unavailable ({type(e).__name__}) and no fallback for {place}."}
        return {"place": row["city"].title(), "month": MONTHS[day.month - 1], "kind": "normals",
                "high_c": float(row["high_c"]), "low_c": float(row["low_c"]),
                "rain_days": int(row["rain_days"]), "source": "committed fallback table (offline)"}


def _normals(loc: dict, month: int) -> dict:
    year = date.today().year
    r = httpx.get(ARCHIVE_URL, timeout=TIMEOUT, params={
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "UTC",
        "start_date": f"{year - CLIMATE_YEARS}-01-01", "end_date": f"{year - 1}-12-31",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum"})
    r.raise_for_status()
    d = r.json()["daily"]
    keep = [i for i, t in enumerate(d["time"]) if int(t[5:7]) == month]
    highs = [d["temperature_2m_max"][i] for i in keep if d["temperature_2m_max"][i] is not None]
    lows = [d["temperature_2m_min"][i] for i in keep if d["temperature_2m_min"][i] is not None]
    wet = [i for i in keep if (d["precipitation_sum"][i] or 0) >= 1.0]
    return {"place": loc["name"], "month": MONTHS[month - 1], "kind": "normals",
            "high_c": round(sum(highs) / len(highs), 1), "low_c": round(sum(lows) / len(lows), 1),
            "rain_days": round(len(wet) / CLIMATE_YEARS),
            "source": f"Open-Meteo historical archive, {year - CLIMATE_YEARS}-{year - 1} average (not a forecast)"}


@functools.cache
def _fallback() -> dict:
    with open(DATA / "climate_fallback.csv", newline="", encoding="utf-8") as f:
        return {(r["city"].lower(), int(r["month"])): r for r in csv.DictReader(f)}


if __name__ == "__main__":
    print(verdict("velvet dress"))
    print(rising("dress")[:3])
    print(closet_summary())
    print(closet(occasion="wedding guest", season="summer")[:3])
    print(weather("New York", date.today().isoformat()))
    print(weather("Charleston", "2027-04-12"))
