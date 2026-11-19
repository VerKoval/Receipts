"""Receipts: a closet stylist that has to show its evidence.

You say the occasion, the place and the date. A Lead reads the dress code and what is rising on
Google right now, then fans out in one turn:
  Closet Researcher  what in this closet fits the occasion, season and formality? (synthetic closet)
  Trend Analysts     is this piece's trend RISING, STEADY or FADING? (5 years of Google Trends, scored offline)
  Weather Scout      what will it actually be like there? (Open-Meteo, live)
Each runs in a fresh context and reports findings only. If the findings leave a gap, the Lead
sends one more round. Then a Writer, which never saw a tool call, prints three outfits as receipts.

The verdict words are computed by build/build.py. The model looks them up; it never decides them.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import uvicorn
from agents import Agent, ModelSettings, RunContextWrapper, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import datastore as ds
from context_panel import AgentContexts, PanelHooks, add_context_panel

set_tracing_disabled(True)

# Gemini on Vertex through LiteLLM. On Cloud Run the service account authenticates; no API key.
MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
RESEARCHER_MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused")
# reasoning_effort turns on Gemini's thinking; LiteLLM hands back the thought summary, which the
# page shows in each agent's column. "low" keeps it fast.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2,
                                     "reasoning_effort": "low"})

MAX_RESEARCHERS = 7  # across all rounds; keeps every answer well inside the grader's 5 minutes


@dataclass
class Run:
    """One request's state, shared by every agent and tool. The model never sees it."""

    hooks: PanelHooks
    researchers: int = 0
    findings: list[str] = field(default_factory=list)  # all the Writer gets to see
    evidence: dict = field(default_factory=lambda: {"trends": {}, "weather": None, "dress_code": None})


# ---------------------------------------------------------------- tools: Google Trends


@function_tool
def find_trend_terms(query: str) -> str:
    """Find tracked trend terms that match a word. Use this first: only these exact terms have
    five years of search data.

    Args:
        query: a garment or trend word, e.g. 'velvet' or 'jacket'.
    """
    hits = ds.find_terms(query)
    if not hits:
        return (f"No tracked term matches {query!r}. Tracked terms: "
                + ", ".join(t["term"] for t in ds.tracked_terms()) + ". Say it is not tracked rather than guessing.")
    return json.dumps(hits)


@function_tool
def trend_verdict(ctx: RunContextWrapper[Run], term: str) -> str:
    """The scored verdict for one tracked term, with the numbers behind it.

    Args:
        term: an exact term from find_trend_terms.
    """
    v = ds.verdict(term)
    if v is None:
        return f"{term!r} is not tracked. Use find_trend_terms."
    ctx.context.evidence["trends"][v["term"]] = {"verdict": v["verdict"], "why": v["why"], "series": ds.series(v["term"])}
    return json.dumps(v, default=str)


@function_tool
def rising_queries(query: str = "", category: str = "", kind: str = "rising") -> str:
    """Google Trends related-queries snapshots: what people searched more (rising) or most (top)
    in a time window. Good for spotting what is hot this month.

    Args:
        query: optional text filter, e.g. 'dress' or 'skirt'. Empty for everything.
        category: optional, one of 'womenswear', 'menswear', 'apparel', 'fashion & style'.
        kind: 'rising' (fastest growing) or 'top' (most searched).
    """
    rows = ds.rising(query, category, kind)
    return json.dumps(rows) if rows else f"No {kind} queries match {query!r} in {category or 'any category'}."


# ---------------------------------------------------------------- tools: the closet


@function_tool
def closet_overview() -> str:
    """The whole closet in one list (it is only 60 items): id, item, category, formality, warmth,
    seasons, occasions and trend verdict. Call this first."""
    cols = ["id", "item", "category", "formality", "warmth", "seasons", "occasions", "trend_term", "trend_verdict"]
    return json.dumps([{k: r[k] for k in cols if r.get(k) is not None} for r in ds.closet()])


@function_tool
def search_closet(category: str = "", occasion: str = "", season: str = "", min_formality: int = 0,
                  max_formality: int = 5, min_warmth: int = 0, max_warmth: int = 5, text: str = "") -> str:
    """Closet items matching every filter you give. Formality and warmth run 0-5. Each item has an
    id (c01...), and a trend_term + trend_verdict when it maps to a tracked Google Trends term.

    Args:
        category: top, bottom, dress, outerwear, shoes or accessory. Empty for all.
        occasion: e.g. 'wedding guest', 'business casual', 'college'. Empty for any.
        season: spring, summer, fall or winter. Empty for any.
        min_formality: lowest formality (0 gym, 1 casual, 3 business casual, 5 black tie).
        max_formality: highest formality.
        min_warmth: lowest warmth (0-5).
        max_warmth: highest warmth (0-5).
        text: optional word matched against item name, fabric, colour and trend, e.g. 'velvet' or 'linen'.
    """
    rows = ds.closet(category, max_warmth, min_warmth, min_formality, max_formality, season, occasion, text)
    return json.dumps(rows) if rows else "Nothing in the closet matches. Say so: that is a gap, not an error."


# ---------------------------------------------------------------- tools: weather, dress codes


@function_tool
def weather(ctx: RunContextWrapper[Run], place: str, when: str) -> str:
    """Weather for a place on a date: the live forecast if it is within 15 days, otherwise the
    five-year average for that month.

    Args:
        place: a city, e.g. 'Austin' or 'Lisbon, Portugal'.
        when: the date as YYYY-MM-DD.
    """
    w = ds.weather(place, when)
    if "error" not in w:
        ctx.context.evidence["weather"] = w
    return json.dumps(w)


@function_tool
def dress_codes(ctx: RunContextWrapper[Run], code: str = "") -> str:
    """The dress-code table: formality 1-5, what to reach for, what to avoid, and the source.

    Args:
        code: optional, e.g. 'cocktail'. Empty returns all codes.
    """
    rows = ds.dress_codes()
    if code:
        match = [r for r in rows if code.lower() in r["code"].lower()]
        if match:
            ctx.context.evidence["dress_code"] = match[0]
            # The Writer only reads findings, so the dress code goes there too.
            ctx.context.findings.append("## Dress code: " + match[0]["code"] + "\n\n" + json.dumps(match[0]))
            return json.dumps(match)
    return json.dumps(rows)


# ---------------------------------------------------------------- researchers

trend_analyst = Agent(
    name="Trend Analyst",
    instructions="""\
You check whether one trend is actually happening, using only Google Trends data.

1. find_trend_terms to get the exact tracked term. Never pass your own wording to trend_verdict.
2. trend_verdict on the best match. Optionally rising_queries with a related word for this month's signal.
3. Report at most 5 bullets: the verdict word first, exactly as given, then the numbers
   (change vs last year, peak month, data window). Mention any anomaly months the data flags.

Verdicts: RISING (12-month median up 20%+ on the year before), STEADY, FADING (down 20%+),
TOO_QUIET (too little search to judge). If the term is not tracked, say NOT TRACKED and stop.
Never substitute a trend you happen to know about.""",
    model=RESEARCHER_MODEL, model_settings=SETTINGS,
    tools=[find_trend_terms, trend_verdict, rising_queries],
)

closet_researcher = Agent(
    name="Closet Researcher",
    instructions="""\
You find what in the user's closet works for one occasion. The closet is the only source of
clothes: never invent an item.

1. closet_overview: it lists every item. Read all of it. Use the formality the dress code
   implies (casual 1-2, business casual 3, cocktail 4, black tie 5), the season and the warmth.
   search_closet is there if you want to filter, but the list is the truth. If the request names
   a garment, fabric or trend (e.g. velvet), find every item that matches by name or fabric.
2. Report candidates grouped by category: id, item, formality, warmth, and trend_verdict if it has
   one. At most 4 per category, best first.
3. End with GAPS: what the occasion needs that the closet does not have (e.g. no floor-length
   dress for black tie). Write "GAPS: none" if there are none.""",
    model=RESEARCHER_MODEL, model_settings=SETTINGS,
    tools=[closet_overview, search_closet],
)

weather_scout = Agent(
    name="Weather Scout",
    instructions="""\
You find out what the weather will be so the outfit survives the day.

1. Call weather with the place and date.
2. Report at most 4 bullets: high and low in °C and °F, rain, and what that means for fabric,
   layers and shoes. Copy the tool's "source" field word for word as your last bullet, so the
   reader knows if it is a live forecast, a multi-year average or the offline fallback. If the
   place was not found, say that.""",
    model=RESEARCHER_MODEL, model_settings=SETTINGS,
    tools=[weather],
)


async def _send(ctx: RunContextWrapper[Run], agent: Agent, label: str, prompt: str) -> str:
    """Run one researcher in a fresh context and keep only its findings."""
    if ctx.context.researchers >= MAX_RESEARCHERS:
        return "No researchers left. Work with what you have and reply DONE."
    ctx.context.researchers += 1
    clone = agent.clone(name=f"{agent.name} {ctx.context.researchers}")
    result = await Runner.run(clone, prompt, context=ctx.context, hooks=ctx.context.hooks, max_turns=8)
    ctx.context.findings.append(f"## {label}\n\n{result.final_output}")
    return str(result.final_output)


@function_tool
async def check_trend(ctx: RunContextWrapper[Run], term: str) -> str:
    """Send a Trend Analyst to check one trend. Call several in the same turn: they run in parallel.

    Args:
        term: one tracked trend term, e.g. 'velvet dress'.
    """
    return await _send(ctx, trend_analyst, f"Trend check: {term}", f"Trend to check: {term}")


@function_tool
async def check_closet(ctx: RunContextWrapper[Run], occasion: str, dress_code: str, season: str) -> str:
    """Send a Closet Researcher to find candidate pieces in the closet.

    Args:
        occasion: e.g. 'summer wedding guest'.
        dress_code: e.g. 'cocktail' or 'business casual'.
        season: spring, summer, fall or winter.
    """
    return await _send(ctx, closet_researcher, f"Closet: {occasion}",
                       f"Occasion: {occasion}\nDress code: {dress_code}\nSeason: {season}")


@function_tool
async def check_weather(ctx: RunContextWrapper[Run], place: str, when: str) -> str:
    """Send a Weather Scout.

    Args:
        place: a city.
        when: the date as YYYY-MM-DD.
    """
    return await _send(ctx, weather_scout, f"Weather: {place} on {when}", f"Place: {place}\nDate: {when}")


@function_tool
def tracked_trends() -> str:
    """Every trend term with five years of Google Trends data, and its verdict."""
    return json.dumps(ds.tracked_terms())


# ---------------------------------------------------------------- the Lead


def lead_instructions(ctx, agent) -> str:
    return f"""\
Today is {date.today():%A %d %B %Y}. You lead a styling team that answers "what should I wear?"
from the user's own closet, with evidence. You only help with clothes and outfits.

Decide first whether you can act.
- Not about what to wear or fashion trends: reply NEEDS: I only help with what to wear. What's the occasion?
- Too vague to act on (no occasion at all, e.g. "what should I wear?"): reply with ONE line,
  NEEDS: <your single most important question>, and nothing else.
- Do not ask about place or date when a season is named ("summer wedding") or the occasion is
  ongoing ("college", "business casual"): use that season, or the current one, and skip the
  weather check when there is no city.
- A question about a trend alone ("is velvet in?") needs no place or date, but still call
  check_closet (occasion: "everyday, wearing <the trend>") so the answer uses pieces the user owns.

Otherwise:
1. Call dress_codes with the one code that fits the occasion (always pass a code; a summer
   wedding with no stated code is cocktail, college is casual), tracked_trends, and rising_queries
   (category womenswear or menswear if the request implies one) to see what is hot right now.
2. In ONE turn, call in parallel: check_closet; check_weather if there is a city (date as
   YYYY-MM-DD; for a month or season alone use the 15th of the next such month); and check_trend
   for 2-3 tracked terms that fit the occasion and that closet pieces map to (dresses, jeans,
   jackets, boots, sneakers...).
3. Read the findings. If something important is missing (e.g. a trend you want to recommend is
   unverified), send at most one more round.
4. Reply DONE and nothing else. A writer writes the answer.

Only check trends a closet piece for THIS occasion maps to (e.g. no boots for a summer wedding).
If one of them turns out FADING, that is a useful receipt too."""


lead = Agent(
    name="Lead",
    instructions=lead_instructions,
    model=MODEL, model_settings=SETTINGS,
    tools=[dress_codes, tracked_trends, rising_queries, check_closet, check_trend, check_weather],
)


# ---------------------------------------------------------------- the Writer


class Item(BaseModel):
    closet_id: str = Field(description="the closet id, e.g. 'c24'")
    piece: str = Field(description="the closet item's name, exactly as the findings give it")
    stamp: str = Field(description="the evidence backing it: a trend verdict (RISING, STEADY, FADING, TOO_QUIET), "
                                   "WEATHER, DRESS CODE, or TASTE if no finding supports it")
    receipt: str = Field(description="one short line of evidence with a number and its source "
                                     "(Google Trends / Open-Meteo / dress-code guide), or 'my taste, not the data'")


class Outfit(BaseModel):
    name: str = Field(description="short, e.g. 'The Safe Bet', 'On Trend', 'The Wildcard'")
    vibe: str = Field(description="three or four words")
    items: list[Item]
    why: str = Field(description="one or two sentences tying it to the occasion and weather")


class Report(BaseModel):
    headline: str = Field(description="one line summary of the answer")
    weather_line: str = Field(description="the weather in one line with numbers and source, or 'not checked'")
    dress_code_line: str = Field(description="the dress code in one line, or 'n/a'")
    outfits: list[Outfit] = Field(description="exactly three outfits, unless the request was only about a trend")
    busted: list[str] = Field(description="closet pieces whose trend is FADING, with the number and why to skip them now")
    gaps: list[str] = Field(description="what the occasion needs that the closet lacks; empty if nothing")
    cant_tell: str = Field(description="what the data could not answer, plainly")


writer = Agent(
    name="Writer",
    instructions="""\
You write the final answer from the research findings only. You never saw the tools.

Give three outfits built ONLY from closet items in the findings, with their ids: a safe one, an
on-trend one, and a wildcard. Each outfit is a full look of 3-5 pieces (shoes included), and the
three must differ from each other. Every item carries a stamp and a receipt. Rules:
- Copy verdict words exactly as the findings give them. Never upgrade TOO_QUIET, TOO_FEW or
  NOT TRACKED into a recommendation.
- A piece whose trend is FADING goes in `busted`, never in the on-trend outfit.
- Never invent a garment. If the closet lacks something the occasion needs, put it in `gaps`,
  but only when the findings show the closet was searched for it and nothing came back.
- An item with no supporting finding gets stamp TASTE and receipt "my taste, not the data".
- Weather and dress code can back items too (stamp WEATHER or DRESS CODE). Name the weather source
  exactly as the findings give it.
- Plain words. No "elevate", "effortless", "timeless", "statement piece".
- If the request was only about a trend, return outfits that show how to wear it (or none if it is busted).""",
    model=MODEL, model_settings=SETTINGS,
    output_type=Report,
)


def to_markdown(r: Report) -> str:
    lines = [f"**{r.headline}**", "", f"- Weather: {r.weather_line}", f"- Dress code: {r.dress_code_line}", ""]
    for o in r.outfits:
        lines += [f"### {o.name} — {o.vibe}"]
        lines += [f"- **{i.piece}** ({i.closet_id}) `{i.stamp}` {i.receipt}" for i in o.items]
        lines += [f"\n{o.why}", ""]
    if r.busted:
        lines += ["### Skip these"] + [f"- {b}" for b in r.busted] + [""]
    if r.gaps:
        lines += ["### Your closet is missing"] + [f"- {g}" for g in r.gaps] + [""]
    lines += [f"_What the data can't tell you:_ {r.cant_tell}"]
    return "\n".join(lines)


async def style(request: str, hooks: PanelHooks) -> tuple[str, Report | None, Run]:
    run = Run(hooks=hooks)
    plan = await Runner.run(lead, request, context=run, hooks=hooks, max_turns=12)
    said = str(plan.final_output).strip()
    if said.startswith("NEEDS:"):
        return said, None, run
    findings = "\n\n---\n\n".join(run.findings) or "No findings: the researchers returned nothing."
    result = await Runner.run(writer, f"Request: {request}\n\nFindings:\n\n{findings}", hooks=hooks)
    report: Report = result.final_output
    # The Writer may only use real closet pieces. Drop any id that isn't in the closet and use the
    # closet's own name for the rest, so a hallucinated garment can never reach the page.
    for outfit in report.outfits:
        kept = []
        for item in outfit.items:
            real = ds.closet_item(item.closet_id)
            if real:
                item.piece = real["item"]
                kept.append(item)
        outfit.items = kept
    report.outfits = [o for o in report.outfits if o.items]
    return to_markdown(report), report, run


# ---------------------------------------------------------------- web app

HERE = Path(__file__).parent
sessions: dict[str, AgentContexts] = {}
app = FastAPI()
add_context_panel(app, sessions)


def _short(value, limit: int = 700) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + " ..."


class CollectingHooks(PanelHooks):
    """PanelHooks plus two things: a record of every tool call for /chat, and a "step" event for
    every move an agent makes (its task, its thoughts, each call, each result, its report), so
    the page can draw one live column per agent."""

    def __init__(self, contexts, emit, calls: list[dict]):
        super().__init__(contexts, emit)
        self.calls = calls
        self.briefed: set[str] = set()

    def step(self, agent: str, kind: str, text: str, tool: str = "") -> None:
        self.emit({"type": "step", "agent": agent, "kind": kind, "tool": tool, "text": text})

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        await super().on_llm_start(context, agent, system_prompt, input_items)
        if agent.name not in self.briefed:  # the first time this agent runs: show its task
            self.briefed.add(agent.name)
            asks = [i for i in input_items if isinstance(i, dict) and i.get("role") == "user"]
            self.step(agent.name, "task", _short(asks[-1]["content"] if asks else "", 400))

    async def on_llm_end(self, context, agent, response):
        await super().on_llm_end(context, agent, response)
        for item in response.output:
            kind = getattr(item, "type", None)
            if kind == "reasoning":
                summary = " ".join(getattr(s, "text", "") for s in (getattr(item, "summary", None) or []))
                content = " ".join(getattr(c, "text", "") for c in (getattr(item, "content", None) or []))
                if (summary or content).strip():
                    self.step(agent.name, "thought", _short(summary or content, 1200))
            elif kind == "function_call":
                self.step(agent.name, "call", _short(item.arguments, 300), tool=item.name)
            elif kind == "message":
                text = " ".join(getattr(c, "text", "") for c in item.content if hasattr(c, "text"))
                if text.strip():
                    self.step(agent.name, "say", _short(text, 1500))

    async def on_tool_start(self, context, agent, tool):
        await super().on_tool_start(context, agent, tool)
        self.calls.append({"agent": agent.name, "tool": tool.name, "status": "started",
                           "arguments": getattr(context, "tool_arguments", None)})

    async def on_tool_end(self, context, agent, tool, result):
        await super().on_tool_end(context, agent, tool, result)
        self.calls.append({"agent": agent.name, "tool": tool.name, "result": str(result)[:600]})
        self.step(agent.name, "result", _short(str(result), 600), tool=tool.name)

    async def on_agent_end(self, context, agent, output):
        await super().on_agent_end(context, agent, output)
        self.step(agent.name, "done", "")


@app.get("/")
def index():
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/closet")
def closet():
    """The whole closet, for the rail on the page."""
    return ds.closet()


@app.get("/trends")
def trends_board():
    """Every tracked term with its verdict and series, for the ticker on the page."""
    return [{**t, "series": [p["interest"] for p in ds.series(t["term"])]} for t in ds.tracked_terms()]


@app.get("/healthz")
def healthz():
    return {"ok": True, "tracked_terms": len(ds.tracked_terms())}


class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
async def chat(body: ChatRequest):
    """One request, one JSON answer. Sub-agent tool calls are in tool_calls with their agent."""
    calls: list[dict] = []
    hooks = CollectingHooks(AgentContexts(), lambda e: None, calls)
    answer, report, run = await style(body.message, hooks)
    return {"response": answer, "tool_calls": calls,
            "report": report.model_dump() if report else None, "evidence": run.evidence}


@app.get("/run")
async def run_stream(request: str):
    """The same work streamed to the page as Server-Sent Events."""
    run_id = str(uuid.uuid4())
    sessions[run_id] = AgentContexts()
    queue: asyncio.Queue = asyncio.Queue()
    calls: list[dict] = []
    hooks = CollectingHooks(sessions[run_id], queue.put_nowait, calls)

    async def work():
        try:
            answer, report, state = await style(request, hooks)
            if report is None:
                queue.put_nowait({"type": "question", "text": answer.removeprefix("NEEDS:").strip()})
            else:
                queue.put_nowait({"type": "done", "markdown": answer, "report": report.model_dump(),
                                  "evidence": state.evidence, "tool_calls": calls})
        except Exception as e:
            queue.put_nowait({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"})

    task = asyncio.create_task(work())

    async def events():
        try:
            yield f"data: {json.dumps({'type': 'start', 'run_id': run_id})}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("done", "error", "question"):
                    break
        finally:
            task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream")


if __name__ == "__main__":
    import os

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
