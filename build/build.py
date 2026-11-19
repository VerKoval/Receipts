"""Offline build: turn the raw downloads in build/raw/ into the parquet tables the agent reads.

    uv run --extra build build/build.py

Three outputs in data/:
  trends.parquet    monthly US search interest per term (Google Trends "interest over time")
  verdicts.parquet  one scored verdict per term: RISING, STEADY, FADING or TOO_QUIET
  rising.parquet    Google Trends "related queries" snapshots: what was top / rising, and when

The verdicts are decided here, by arithmetic, never by the model. The agents look them up.
"""

import re
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
RAW = HERE / "raw"
DATA = HERE.parent / "data"


# --- 1. Google Trends: interest over time --------------------------------------------------


def load_timeseries() -> pd.DataFrame:
    """Each CSV is one Google Trends compare (up to 8 terms), scaled 0-100 *within that file*.
    So values are comparable inside a batch, not across batches. We keep the batch id."""
    frames = []
    for path in sorted((RAW / "trends_timeseries").glob("*.csv")):
        wide = pd.read_csv(path)
        long = wide.melt(id_vars="Time", var_name="term", value_name="interest")
        long["batch"] = path.stem
        frames.append(long)
    df = pd.concat(frames)
    df["month"] = pd.to_datetime(df["Time"])
    df["term"] = df["term"].str.strip().str.lower()
    df["interest"] = pd.to_numeric(df["interest"].astype(str).str.replace("<1", "0"), errors="coerce")
    # A term downloaded twice: keep the batch where it has the best resolution (highest peak).
    best = df.groupby(["term", "batch"])["interest"].max().reset_index()
    best = best.sort_values("interest").drop_duplicates("term", keep="last")
    df = df.merge(best[["term", "batch"]], on=["term", "batch"])
    return df[["term", "month", "interest", "batch"]].sort_values(["term", "month"])


def score(df: pd.DataFrame) -> pd.DataFrame:
    """One verdict per term, comparing whole years so the seasons cancel out.

    recent = the last 12 complete months, prior = the 12 before that, base = the first 12.
    Medians, not means, so one freak month cannot carry a verdict.
    """
    # The current month is partial in a Google Trends download; drop it.
    last_full = df["month"].max() - pd.offsets.MonthBegin(1)
    df = df[df["month"] <= last_full]

    # A month where most terms in the same download spike together is a data artefact, not a
    # trend (one batch has every term jumping in April 2026). Find those months per batch.
    artefacts = {}
    for batch, g in df.groupby("batch"):
        wide = g.pivot(index="month", columns="term", values="interest")
        if wide.shape[1] < 3:
            continue
        # Compare each month with the same calendar month in the other years, so ordinary
        # seasonal peaks (coats in December) do not count as spikes.
        same_month = wide.groupby(wide.index.month).transform("median").clip(lower=1)
        ratio = wide / same_month
        spiking = (ratio > 2.5).sum(axis=1) / wide.shape[1]
        artefacts[batch] = [m for m, share in spiking.items() if share >= 0.5]

    # The jump hits terms in other downloads too (velvet dress, cropped jacket), so treat a
    # flagged month as a Google-side anomaly and leave it out for every term.
    anomalies = sorted({m for months in artefacts.values() for m in months})

    rows = []
    for term, g in df.groupby("term"):
        s = g.set_index("month")["interest"]
        batch = g["batch"].iloc[0]
        # Artefact months stay in the chart but are left out of the arithmetic.
        s = s.drop([m for m in anomalies if m in s.index]).asfreq("MS").interpolate()
        recent, prior, base = s.iloc[-12:], s.iloc[-24:-12], s.iloc[:12]
        r, p, b = recent.median(), prior.median(), base.median()
        change_1y = (r - p) / p if p > 0 else None
        change_since_2021 = (r - b) / b if b > 0 else None

        # The calendar month this term usually peaks in, averaged over every year.
        by_month = s.groupby(s.index.month).mean()
        peak_month = int(by_month.idxmax())
        seasonal_swing = round(by_month.max() / max(by_month.min(), 0.5), 1)

        if s.median() < 2 and s.max() < 10:
            verdict, why = "TOO_QUIET", "search interest too low to score"
        elif change_1y is None:
            verdict, why = "TOO_QUIET", "no searches in the prior year to compare against"
        elif change_1y >= 0.2:
            verdict, why = "RISING", f"last 12 months up {change_1y:+.0%} on the year before"
        elif change_1y <= -0.2:
            verdict, why = "FADING", f"last 12 months down {change_1y:+.0%} on the year before"
        else:
            verdict, why = "STEADY", f"last 12 months within 20% of the year before ({change_1y:+.0%})"

        flagged = [m.strftime("%b %Y") for m in anomalies]
        rows.append({
            "term": term,
            "verdict": verdict,
            "why": why,
            "recent_median": float(r),
            "prior_median": float(p),
            "base_2021_median": float(b),
            "change_1y": None if change_1y is None else round(change_1y, 3),
            "change_since_2021": None if change_since_2021 is None else round(change_since_2021, 3),
            "peak_month": peak_month,
            "seasonal_swing": seasonal_swing,
            "peak_value_month": s.idxmax().strftime("%b %Y"),
            "artefact_months": ", ".join(flagged),
            "batch": batch,
            "window": f"{s.index.min():%b %Y} to {s.index.max():%b %Y}",
        })
    return pd.DataFrame(rows)


# --- 2. Google Trends: related queries (top / rising) --------------------------------------

# Files are named <category>_<window>_<top|rising>.csv. The exact date window each was downloaded
# for lives here (Google's own file names had it), because the agent cites it.
WINDOWS = {
    "apparel_past_12_months": ("apparel, all (past 12 months)", "2025-11-18", "2026-11-18"),
    "fashion_past_5_years": ("fashion & style (past 5 years)", "2021-11-18", "2026-11-18"),
    "womenswear_past_30_days": ("womenswear (past 30 days)", "2026-10-18", "2026-11-18"),
    "menswear_past_30_days": ("menswear (past 30 days)", "2026-10-18", "2026-11-18"),
}

# Related queries pull in whatever else people typed. These are not clothes.
NOISE = re.compile(
    r"wordle|fifa|world cup|google|ai mode|weather|university|rankings|career|online learning|"
    r"travel destinations|health & fitness|tiles nyt|spirit halloween|wayfair|meaning$|"
    r"jaylen|elly de la cruz|tommy detamore|ladd mcconkey|percy hynes|raquel leviss|tommy paul|"
    r"charlbi dean|jenna ortega|gisele|tasha k|leni klum|jack harlow|bernard arnault|shamar|"
    r"brice turang|james pearce|heidi klum|zoe kravitz|adriana lima|cindy crawford|shalom harlow|"
    r"papillon|10 good men|aniara|one pace|fast pace|^pace$|^gia$|i am gia|^line$|^model$|"
    r"60 days|hims|mobus|^product$|^tommy$|cosmopolitan|mannequin|dg market|^mode$"
)


def load_related() -> pd.DataFrame:
    frames = []
    for path in sorted((RAW / "trends_related").glob("*.csv")):
        name, kind = path.stem.rsplit("_", 1)  # e.g. womenswear_past_30_days, rising
        category, start, end = WINDOWS[name]
        df = pd.read_csv(path)
        df.columns = ["query", "interest", "increase"]
        df["kind"] = kind
        df["window_start"] = pd.to_datetime(start).date()
        df["window_end"] = pd.to_datetime(end).date()
        df["category"] = category
        df["source_file"] = path.name
        frames.append(df)
    df = pd.concat(frames)
    df["query"] = df["query"].str.strip().str.lower()
    df["increase_pct"] = pd.to_numeric(
        df["increase"].astype(str).str.replace(r"[%,+]", "", regex=True).replace("Breakout", "5000"),
        errors="coerce",
    )
    df["noise"] = df["query"].str.contains(NOISE)
    return df


if __name__ == "__main__":
    ts = load_timeseries()
    verdicts = score(ts)
    related = load_related()

    ts.to_parquet(DATA / "trends.parquet", index=False)
    verdicts.to_parquet(DATA / "verdicts.parquet", index=False)
    related.to_parquet(DATA / "rising.parquet", index=False)

    pd.set_option("display.width", 200)
    print(verdicts[["term", "verdict", "why", "peak_month", "artefact_months"]].to_string(index=False))
    print(f"\n{len(related)} related queries, {related['noise'].sum()} flagged as noise")
