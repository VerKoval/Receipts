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
| Google Trends, *interest over time* | Monthly US search interest, Nov 2021 – Nov 2026, 31 terms in 5 downloads (`build/raw/trends_timeseries/`) | Downloaded by hand from trends.google.com (Explore → US → past 5 years → ↓ CSV). Google Trends has no free API. |
| Google Trends, *related queries* | Top and rising searches for 4 windows/categories (womenswear and menswear for the past 30 days, apparel for the past 12 months, fashion for the past 5 years) (`build/raw/trends_related/`) | Same page, "Related queries" ↓. Noise (sports, Wordle, celebrities) is filtered in the build. |
| Synthetic closet | 71 items with category, colour, fabric, warmth 0–5, formality 0–5, seasons, occasions, vibe tags (punk, goth, preppy…), and a `trend_term` linking it to a Google Trends term (`data/closet.csv`) | Written for this project. **Synthetic, and labelled as such on the page.** |
| Dress codes | 18 codes, from black tie to nightclub, rave / warehouse, concert, themed party and night out. Each has formality, what to reach for and what to avoid. The classic codes cite etiquette guides, nightclub cites Wikipedia, and the other event codes are marked as hand-written project notes (`data/dress_codes.csv`) | Hand-built |
| Etiquette rules | 10 rules: no white at a wedding, black tie means long, club door codes, dressing up for upscale settings (boarding schools, country clubs), funerals, religious venues, interviews, no stilettos on grass, rave shoes, Indian weddings. Each lists trigger words and the colours or pieces it rules out (`data/etiquette.csv`) | Hand-built. **Enforced in code:** after the Writer answers, any piece that breaks an active rule is taken out of the outfit and listed under "Skip these". |
| Vibes | 16 aesthetics (punk, grunge, goth, rave, glam, y2k, streetwear, preppy, old money, boho, coquette, clean girl, dark academia, cottagecore, athleisure, normcore), each with aliases, key pieces, colours, details, what to avoid and a Wikipedia source (`data/vibes.csv`) | Hand-built. A themed request ("punk-themed rave") is looked up here, and all three outfits have to fit it. |
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

The model is Gemini on Vertex AI, so there is no API key. Locally, run `gcloud auth application-default login`
(the app defaults to the `receipts-closet` project; set `VERTEXAI_PROJECT` to use another). On Cloud Run the service account authenticates.

**Watch every agent think:** the button under the order form opens one live column per agent
(Lead, each researcher, Writer), side by side. Each column shows its task, Gemini's thought
summaries, every tool call with its arguments, what came back, and its report, as it happens.

The Writer can only use real closet pieces. Any `closet_id` it returns that isn't in
`data/closet.csv` is dropped in code before the answer reaches the page.

`POST /chat {"message": "..."}` returns `{"response", "tool_calls", "report", "evidence"}`. Every
sub-agent's tool calls are in `tool_calls` with an `agent` field.

## Three sample queries

Real outputs from this code run locally on Nov 18, 2026 (Gemini 3.5 Flash on Vertex AI), next to the same model with no tools and no system prompt. The no-tools model gives long, confident, generic advice. It can't know the closet, cites no numbers or sources, and has no idea whether anything is actually rising.

### 1. "What should I wear to a summer wedding in Charleston?"

**Expected:** Three outfits from closet ids only: `c21` sage slip dress or `c23` little black dress with `c40` block-heel sandals. Dress code cocktail, cited to withjoy.com. June five-year average from Open-Meteo: high 29.4 °C, low 23.1 °C, about 16 days with rain. `mini dress` is `RISING` (+36%).

**Source:** `data/closet.csv`, `data/dress_codes.csv`, `data/verdicts.parquet`, Open-Meteo archive API

<details><summary><b>Agent</b> (36s, 5 researchers, 16 tool results)</summary>

**A curated wedding-guest guide for hot, damp Charleston weather with classic and trending options**

- Weather: High of 29.4—C (84.9—F) and low of 23.1—C (73.6—F) in June, based on Open-Meteo historical archive
- Dress code: Cocktail: midi dress, sharp jumpsuit, suit and tie, heels or dressy flats according to https://withjoy.com/blog/wedding-guest-dress-code-guide/

### The Safe Bet — classic cocktail romance
- **sage green slip dress** (c21) `DRESS CODE` midi dress meets cocktail code according to https://withjoy.com/blog/wedding-guest-dress-code-guide/
- **nude block-heel sandals** (c40) `WEATHER` airy sandals suit high of 29.4C according to Open-Meteo
- **beaded clutch** (c53) `TASTE` my taste, not the data

This classic sage green slip dress meets the cocktail requirement perfectly, while the breathable fabric and airy sandals keep you comfortable in the warm Charleston heat.

### On Trend — modern trending evening
- **little black dress** (c23) `RISING` mini dress up +36% over last year according to Google Trends
- **black pointed-toe pumps** (c41) `TASTE` my taste, not the data
- **gold hoop earrings** (c50) `TASTE` my taste, not the data

This trending little black dress gives a sharp cocktail silhouette, paired with pumps for an elegant, elevated appearance.

### The Wildcard — polished structured separates
- **white silk-look blouse** (c05) `TASTE` my taste, not the data
- **black satin midi skirt** (c17) `DRESS CODE` dressy separates fit cocktail format according to https://withjoy.com/blog/wedding-guest-dress-code-guide/
- **black pointed-toe pumps** (c41) `TASTE` my taste, not the data
- **pearl hair clip** (c58) `TASTE` my taste, not the data

Choosing dressy satin separates offers a modern twist on standard guest attire, keeping you breezy and elegant through the reception.

_What the data can't tell you:_ The data cannot confirm the exact local rain forecast for the specific calendar day, only historical averages.

</details>

<details><summary><b>Same model, no tools, no system prompt</b></summary>

Attending a summer wedding in Charleston, South Carolina, is beautiful, but it requires a strategic approach to fashion. Charleston in the summer is **hot, humid, and coastal**, with a distinct "Southern chic" aesthetic. 

To look stylish while surviving the Lowcountry heat, here is your ultimate guide on what to wear.

---

### 1. The Golden Rules of Charleston Summer Style
*   **Fabric is everything:** Avoid polyester, heavy satin, and thick silks. They do not breathe and will show sweat instantly. Opt for **linen, cotton, organza, chiffon, georgette, or seersucker**.
*   **Embrace the Lowcountry Palette:** Think "Southern garden party." Pastel shades (mint, peach, dusty blue, lavender), bright citrus tones, and floral prints are highly encouraged. Avoid heavy blacks or dark jewel tones.
*   **Prepare for the Terrain:** Charleston is famous for its historic cobblestones, uneven brick sidewalks, and grassy plantation lawns. **Leave the stilettos at home.**
*   **The AC/Humidity Contrast:** You will sweat outside, but the indoor air conditioning will be freezing. Bring a light layer.

---

### For Women

#### **Cocktail / Semi-Formal (Most Common)**
*   **The Look:** A midi or maxi-length dress in a lightweight, flowy fabric. 
*   **Style Ideas:** A floral georgette wrap dress, a tiered cotton-voile midi dress, or a pastel organza dress with puff sleeves. 
*   **Footwear:** Block heels, elegant wedges, or metallic dressy flats. These will keep you from sinking into the grass at historic venues like Lowndes Grove or Middleton Place.
*   **Accessory:** A chic woven clutch (like wicker or raffia) or a colorful statement earring. Bring a light pashmina or linen blazer for the AC.

#### **Formal / Black-Tie Optional**
*   **The Look:** A floor-length gown in a breathable fabric like silk chiffon or high-end georgette.
*   **Style Ideas:** A bright, solid-colored gown (think marigold, Kelly green, or coral) or a sophisticated, watercolor-esque floral print. 
*   **Footwear:** Dressy block-heel sandals.

#### **Casual / Beach (e.g., Kiawah Island or Folly Beach)**
*   **The Look:** A high-end sundress or a chic two-piece linen set.
*   **Style Ideas:** A linen halter dress or a cotton eyelet midi dress.
*   **Footwear:** Fancy flat sandals or espadrilles.

---

### For Men

#### **Cocktail / Semi-Formal**
*   **The Look:** A lightweight suit in a light color. 
*   **Style Ideas:** A linen-blend suit, a lightweight cotton-stretch suit, or a modern seersucker suit (very Charleston) in tan, light gray, soft blue, or stone. 
*   **Shirt:** A crisp, white, 100% cotton or linen-blend button-down.
*   **Tie:** Optional, but if you wear one, go for a knit tie or a linen tie in a pastel shade.
*   **Footwear:** Suede loafers, leather driving shoes, or boat shoes (for very casual/beach weddings) worn without socks (or with no-show socks).

#### **Formal / Black-Tie Optional**
*   **The Look:** If it�s Black-Tie, you must wear a tuxedo. If it is "Optional," a very dark navy or charcoal lightweight wool suit is acceptable.
*   **Pro-Tip:** Ask for an unlined or half-lined jacket to allow for maximum airflow.
*   **The Southern Twist:** A white or cream dinner jacket with black tuxedo trousers is a highly acceptable, incredibly dapper option for Southern formal weddings.

#### **Casual / Beach**
*   **The Look:** Tailored chinos (in khaki, stone, or even Nantucket red) with a neatly pressed linen button-down (sleeves rolled up to the elbow). No tie required. A navy blazer is optional but recommended.

---

### Charleston "Survival" Accessories
Do not underestimate the Southern humidity. Add these items to your packing list:
1.  **A Hand Fan:** A stylish paper, bamboo, or lace hand fan is a lifesaver during outdoor ceremonies and looks great in photos.
2.  **Anti-Frizz Hair Products / Updos:** The humidity is intense. Women may want to plan for a chic low-bun, ponytail, or braid to keep hair off the neck.
3.  **Sunglasses:** Essential for outdoor daytime ceremonies. Opt for a classic shape (like tortoiseshell or wire frames) rather than sporty ones.
4.  **Mini Umbrella:** Summer in Charleston brings quick, sudden afternoon thunderstorms. Keep a small umbrella in your bag.
5.  **Setting Spray & Blotting Papers:** To keep your makeup from melting off before the reception starts.

</details>

### 2. "Business casual first day at an internship in Seattle on Dec 1"

**Expected:** Formality-3 pieces: `c04` oxford, `c14`/`c15` trousers, `c39` loafers, and `c60` wool coat for the cold. The live Open-Meteo forecast for Dec 1 (high about 8 °C, 42% rain). `cropped jacket` is `RISING` (+39%), backing `c34` tweed jacket.

**Source:** `data/closet.csv`, `data/dress_codes.csv`, `data/verdicts.parquet`, Open-Meteo forecast API

<details><summary><b>Agent</b> (23s, 5 researchers, 16 tool results)</summary>

**A warm and professional start for your Seattle internship**

- Weather: High of 7.6—C (45.7—F), low of 4.8—C (40.6—F) with a 42% chance of rain (Open-Meteo)
- Dress code: Business casual first day at an internship

### The Safe Bet — polished and warm
- **light blue oxford button-down** (c04) `TASTE` my taste, not the data
- **grey pleated trousers** (c15) `TASTE` my taste, not the data
- **black wool coat** (c60) `WEATHER` wool is ideal for chilly 7.6—C weather according to Open-Meteo
- **black loafers** (c39) `TASTE` my taste, not the data

This outfit combines tailored elements to ensure a highly professional first impression while keeping you warm in the chilly Seattle winter weather.

### On Trend — modern and textured
- **white silk-look blouse** (c05) `TASTE` my taste, not the data
- **black wide-leg trousers** (c14) `TASTE` my taste, not the data
- **cropped tweed jacket** (c34) `RISING` up +39% on the year before (Google Trends)
- **black loafers** (c39) `TASTE` my taste, not the data

The cropped jacket is highly on-trend and pairs perfectly with wide-leg trousers for a sharp, modern business casual look.

### The Wildcard — sleek and cosy
- **long-sleeve black knit dress** (c27) `TASTE` my taste, not the data
- **sheer black tights** (c54) `RISING` rising trend in recent data (Google Trends)
- **black oversized blazer** (c29) `TASTE` my taste, not the data
- **tan ankle boots** (c43) `WEATHER` sturdy boots are ideal for the 42% rain chance (Open-Meteo)

This option plays with proportions by layering an oversized blazer over a fitted knit dress, kept warm and practical with trending tights and rain-ready boots.

_What the data can't tell you:_ There is no trend data available for loafers, ankle boots, wool coats, or knit wear.

</details>

<details><summary><b>Same model, no tools, no system prompt</b></summary>

Congratulations on your internship! Deciding what to wear on Day 1 can be nerve-wracking, but Seattle has a very distinct style culture that makes this easier. 

"Seattle Business Casual" is generally more relaxed than East Coast business casual, but for your **first day**, you should always aim for the smarter end of the spectrum. On December 1, you will also need to account for the weather: it will likely be cold (mid-40s), damp, and overcast. 

Here is your complete guide to nailing the look, keeping comfort, weather, and PNW style culture in mind.

---

### Option 1: Masculine-Leaning / Classic Menswear

This look is professional, warm, and highly adaptable. Layering is key because Seattle offices can fluctuate wildly in temperature.

*   **The Top:** A crisp, button-down collar shirt (white, light blue, or a subtle pattern) layered under a crewneck or V-neck sweater (merino wool or cotton-cashmere in grey, navy, or forest green). 
*   **The Bottoms:** Tailored chinos (navy, charcoal, or olive) or performance tech-pants (like Lululemon Commission pants). *Avoid blue jeans on Day 1, even if others are wearing them.*
*   **The Shoes:** Leather Chelsea boots, Chukka boots, or leather brogues. **Crucial step:** Make sure they are waterproofed/treated. 
*   **The Outerwear:** A tailored wool overcoat (navygrey, or camel) OR a sleek, dark-colored technical rain jacket (like Arc�teryx, Patagonia, or REI). Both are highly acceptable in Seattle.

### Option 2: Feminine-Leaning / Classic Womenswear

This outfit balances professionalism with the cozy, layered vibe appropriate for a Pacific Northwest winter.

*   **The Top:** A high-quality knit turtleneck, a silk/satin blouse, or a structured knit top. 
*   **The Layer:** A tailored blazer (oversized wool blazers are very on-trend and professional) or a structured cardigan.
*   **The Bottoms:** Wide-leg trousers or ankle-length straight-leg pants in wool, crepe, or heavy cotton. If you wear a skirt or dress, pair it with sheer or opaque black tights to stay warm.
*   **The Shoes:** Leather loafers, low-block heel ankle boots, or smart flats. (Make sure they have decent traction for wet sidewalks).
*   **The Outerwear:** A mid-to-long wool wrap coat or a stylish, water-resistant trench coat.

---

### The "Seattle Rules" of Dressing (Especially for December)

1.  **The Shoe Reality:** You will likely be walking from a light rail station, bus stop, or parking garage in the drizzle. Do not wear suede or super-high heels on Day 1. Wet, cold feet will ruin your first day.
2.  **No Umbrellas (Mostly):** Locals famously rarely use umbrellas unless it's a downpour. Opt for a jacket with a hood, or wear a sleek wool beanie/hat that you can take off and shake out when you get inside.
3.  **The "One Notch Down" Rule:** If you arrive and realize you are the most dressed-up person in the office, don't sweat it. In Seattle, it is *always* better to be slightly overdressed on Day 1 than to show up in a hoodie and sneakers. You can dress down on Day 2 once you see what your team wears.
4.  **The Bag:** Bring a sleek backpack (very common in Seattle tech/corporate offices) or a structured leather tote that can fit a laptop and has a zipper to protect your things from the rain.

### Pro-Tip for Day 1
Keep your hair and makeup/grooming relatively simple. The December humidity/mist in Seattle will ruin any high-maintenance hairstyles during your commute. Opt for a sleek low bun, a neat ponytail, or a natural look that holds up well in damp weather. 

Good luck! You're going to look great.

</details>

### 3. "Is velvet actually in this year?"

**Expected:** `velvet dress` is `RISING`: 12-month median 24.3 vs 13.0 the year before (+87%), peaks every November, April–May 2026 left out as anomalies. Outfits are built around `c24` burgundy velvet midi dress.

**Source:** `data/verdicts.parquet` (from `build/raw/trends_timeseries/velvet_dress.csv`), `data/closet.csv`

<details><summary><b>Agent</b> (34s, 3 researchers, 11 tool results)</summary>

**Velvet is rising sharply for fall, and your burgundy dress is ready for smart casual wear**

- Weather: not checked
- Dress code: smart casual (formality level 2-3)

### The Safe Bet — polished classic prep
- **burgundy velvet midi dress** (c24) `RISING` up +87% on Google Trends
- **camel trench coat** (c30) `STEADY` stable interest on Google Trends
- **black loafers** (c39) `TASTE` my taste, not the data

This balances the dressy burgundy velvet with a steady camel trench coat and classic loafers for an approachable smart casual outfit.

### On Trend — textured modern contrast
- **burgundy velvet midi dress** (c24) `RISING` up +87% on Google Trends
- **cropped tweed jacket** (c34) `RISING` up +39% on Google Trends
- **sheer black tights** (c54) `RISING` rising interest on Google Trends
- **black loafers** (c39) `TASTE` my taste, not the data

This look combines the rising velvet dress and cropped tweed jacket to create a highly fashionable, textured look.

### The Wildcard — edgy fall romance
- **burgundy velvet midi dress** (c24) `RISING` up +87% on Google Trends
- **black faux-leather jacket** (c31) `STEADY` stable interest on Google Trends
- **black loafers** (c39) `TASTE` my taste, not the data
- **small black shoulder bag** (c52) `TASTE` my taste, not the data

The contrast between the soft velvet dress and the tough leather jacket creates a cool, unexpected everyday styling.

### Your closet is missing
- velvet blazer
- velvet trousers
- velvet skirt

_What the data can't tell you:_ The data could not confirm whether velvet tops or shoes are trending, as search findings were restricted to velvet dresses.

</details>

<details><summary><b>Same model, no tools, no system prompt</b></summary>

**Yes, velvet is absolutely "in" for Fall/Winter 2024 and heading into 2025.** 

However, the *way* velvet is being worn this year has shifted. It is no longer just a holiday-only fabric or associated with cheap, shiny fast-fashion. This year, velvet is being treated as a **luxurious, everyday texture** aligned with the "quiet luxury" and "boho-chic" revivals.

Here is how velvet is trending right now and how to wear it so it feels fresh, not dated:

### 1. The Velvet Blazer & Pantsuit
Tailoring is huge right now, and velvet suits are the ultimate statement. 
* **The Look:** Think 1970s Mick Jagger or 1990s Tom Ford for Gucci. Instead of a stiff, corporate look, this year�s velvet suits are slightly oversized, slouchy, and incredibly chic.
* **How to wear it:** Wear a velvet blazer over a simple white t-shirt and jeans for a high-low look, or wear the full suit with nothing but a lace bralette underneath for evening.

### 2. The "It" Color: Burgundy & Oxblood
If you buy one velvet piece this year, make it burgundy, Merlot, or deep oxblood. This is the definitive color of the season (largely thanks to Gucci�s "Ancora" red). Rich jewel tones like emerald green, chocolate brown, and midnight blue are also incredibly popular.

### 3. "Liquid" and Draped Velvet
Forget the stiff, heavy velvet of the past. This year is all about fluid, silk-blend, or viscose velvet that drapes like water. 
* Long, slip-style maxi dresses and bias-cut skirts that catch the light when you move are very on-trend. 

### 4. Velvet Accessories (The Easy Way In)
If you aren't ready to commit to a full velvet garment, accessories are a massive trend right now:
* **Velvet Mary Jane flats:** A major footwear trend this year.
* **Hair bows:** Coquette-style velvet bows in the hair are still highly popular.
* **Handbags:** Soft, slouchy velvet bags or structured velvet clutches.

---

### What to Avoid (What makes velvet look "out"):
To keep your velvet looking modern rather than dusty or dated, avoid:
* **Cheap, highly synthetic "crushed" velvet:** This can look shiny and inexpensive. Opt for plush, matte, or devor� (burnout) velvet instead.
* **Overly tight bodycon dresses:** This style feels very 2010s. Instead, look for slip silhouettes, A-line skirts, or relaxed tailoring.
* **Too many holiday clich�s:** Wearing red velvet with sparkle can feel a bit "Mrs. Claus." To modernize it, pair velvet with contrasting textures like leather, denim, or chunky knits.

</details>

**A vague request** ("What should I wear?") stops at the Lead with one question and spends nothing on researchers: `NEEDS: What is the occasion or event you are dressing for?`

## Deploy

Cloud Run, continuous deploy from GitHub (Cloud Run → Create service → "Continuously deploy from
a repository" → this repo, Dockerfile build). Set the request timeout to 600 s. The service
account needs the **Vertex AI User** role. All data is committed (under 1 MB), so the container
needs nothing at startup.
