# Receipts

**What to wear from your closet, and the proof it works.**

Ask "what should I wear to a summer wedding?", "to class this week?" or "for a business casual
internship?". The agent searches a closet, checks the dress code, the weather and five years of
Google search interest, then prints **three outfits as receipts**. Every piece carries a stamp
saying what backs it: a trend verdict, the weather, the dress code, or `TASTE` when the data
didn't back it. It also lists the pieces to skip (their trend is fading) and what the closet is
missing.

**Who it's for:** anyone standing in front of a full closet with nothing to wear, who would
rather re-wear what they own than buy something because a video said it's "in".

Deployed: see `deploy_url` in `submission.json`. Path: **Deep Research**.

## How it works

Five agent shapes, one job each:

| Agent | Job | Data |
|---|---|---|
| **Lead** | Asks one `NEEDS:` question if the request is too vague. Otherwise reads the dress code and this month's rising searches, then fans out **in one turn** so the researchers run in parallel. Sends one more round if the findings leave a gap. Capped at 7 researchers. | dress codes, rising queries, tracked terms |
| **Closet Researcher** (fresh context) | Finds candidate pieces by occasion, formality, warmth and season, and reports **GAPS** | `data/closet.csv` |
| **Trend Analyst** (one per trend, fresh context) | Looks up the scored verdict and the numbers behind it. Says `NOT TRACKED` rather than guessing. | `data/verdicts.parquet`, `data/rising.parquet` |
| **Weather Scout** (fresh context) | Live forecast if the date is within 15 days, otherwise the five-year average for that month. Says which one it used. | Open-Meteo |
| **Writer** | Never sees a tool call, only findings. Returns structured JSON: 3 outfits of closet ids, each with a stamp and a sourced receipt, plus `busted`, `gaps` and `cant_tell`. | findings only |

**The verdicts are computed offline, never by the model.** `build/build.py` scores every
Google Trends term by comparing the median of the last 12 complete months with the 12 before
(whole years, so seasons cancel out):

| Verdict | Rule |
|---|---|
| `RISING` | last 12 months up 20%+ on the year before |
| `STEADY` | within ±20% |
| `FADING` | down 20%+ |
| `TOO_QUIET` | too little search to score |

The build also flags **anomaly months**. In one download every term jumped about 9× in April 2026
and fell back by June, and the same jump shows up in other downloads. That is a Google-side
artefact, not a trend, so April–May 2026 are left out of the arithmetic for every term (they stay
on the chart). Without this, `mini dress` and `velvet dress` would look like much bigger risers
than they are.

## Data

| Source | What | How to load |
|---|---|---|
| Google Trends, *interest over time* | Monthly US search interest, Nov 2021 – Nov 2026, 24 terms in 4 downloads (`build/raw/trends_timeseries/`) | Downloaded by hand from trends.google.com (Explore → US → past 5 years → ↓ CSV). Google Trends has no free API. |
| Google Trends, *related queries* | Top and rising searches for 4 windows/categories (womenswear and menswear for the past 30 days, apparel for the past 12 months, fashion for the past 5 years) (`build/raw/trends_related/`) | Same page, "Related queries" ↓. Noise (sports, Wordle, celebrities) is filtered in the build. |
| Synthetic closet | 60 items with category, colour, fabric, warmth 0–5, formality 0–5, seasons, occasions, and a `trend_term` linking it to a Google Trends term (`data/closet.csv`) | Written for this project. **Synthetic, and labelled as such on the page.** |
| Dress codes | 11 codes with formality, what to reach for and what to avoid; every row cites the guide it came from (`data/dress_codes.csv`) | Hand-built |
| Weather | Forecast and multi-year normals | Open-Meteo, live, no key. `data/climate_fallback.csv` if it's down. |

Rebuild the parquet: `uv run --extra build build/build.py`

### What the data can't answer (and the agent says so)
- Any trend outside the 24 tracked terms (`NOT TRACKED`). It won't substitute one it "knows".
- Terms too quiet to score (`old money`, `short skirt`, `ankle boot`, `vest` are `TOO_QUIET`).
- Anything the closet doesn't contain. Those go in "your closet is missing", never into an invented garment.
- Google Trends measures searching, not buying or wearing.
- Weather more than 15 days out is a five-year average, not a forecast.

## Run it

```bash
uv run app.py      # http://localhost:8000
```

The model is Gemini on Vertex AI, so there is no API key: run `gcloud auth application-default login`
locally. On Cloud Run the service account authenticates.

`POST /chat {"message": "..."}` returns `{"response", "tool_calls", "report", "evidence"}`. Every
sub-agent's tool calls are in `tool_calls` with an `agent` field.

## Three sample queries

> TODO before submitting: run each one and paste the real outputs below.

**1. "What should I wear to a summer wedding in Charleston?"**
Expected: three outfits built from closet dresses (`c21` sage slip dress, `c22` floral midi, `c23` LBD),
with `c40` block-heel sandals. Weather is the June five-year average from Open-Meteo (high
29.4 °C, low 23.1 °C, about 16 days with rain, so breathable fabric and no suede). The dress code is cocktail or semi-formal. `mini dress` is `RISING` (+36%).
Source: `data/closet.csv`, `data/verdicts.parquet`, Open-Meteo archive.

| Agent | Same model, no tools, no system prompt |
|---|---|
| _paste_ | _paste_ |

**2. "Business casual first day at an internship in Seattle on Dec 1"**
Expected: trousers (`c14`/`c15`), `c04` oxford or `c09` cardigan, `c39` loafers, `c30` trench
(`trench coat` is `STEADY`, +16%) or `c60` wool coat. `c42` combat boots are flagged
(`doc martens` is `FADING`, −33%).
Source: `data/closet.csv`, `data/verdicts.parquet`, `data/dress_codes.csv`, Open-Meteo forecast.

| Agent | Same model, no tools, no system prompt |
|---|---|
| _paste_ | _paste_ |

**3. "Is velvet actually in this year?"**
Expected: `velvet dress` is `RISING`. The 12-month median is 24.3 vs 13.0 the year before (+87%),
it peaks every November, and April–May 2026 are flagged as anomalies. Suggests the closet's `c24`
burgundy velvet midi.
Source: `data/verdicts.parquet` (`build/raw/trends_timeseries/time_series_US_20211118-2116_*.csv`).

| Agent | Same model, no tools, no system prompt |
|---|---|
| _paste_ | _paste_ |

## Deploy

Cloud Run, continuous deploy from GitHub (Cloud Run → Create service → "Continuously deploy from
a repository" → this repo, Dockerfile build). Set the request timeout to 600 s. The service
account needs the **Vertex AI User** role. All data is committed (under 1 MB), so the container
needs nothing at startup.
