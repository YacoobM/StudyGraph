import asyncio
import json
import time
from collections import Counter, defaultdict

from groq import RateLimitError, BadRequestError

# This file contains functions to dynamically generate evolution data from
# the live StudyGraphEngine instance. The static mock data has been removed.
#
# Everything here is generated lazily, on demand, in two cost tiers rather
# than eagerly at upload/fuse time — a concept nobody ever looks at costs
# nothing:
#   - get_concept_description: one cheap LLM call (earliest-week summary
#     only), for a plain node click in either graph view.
#   - get_concept_detail: every week's summary plus every week-to-week
#     comparison, for the explicit "show concept evolution" action.
# Both cache forever once generated, so re-viewing the same concept (or
# viewing it in the other tier afterward) is always a pure cache read.

# --- Shared color palette ---
# Single source of truth for week coloring, used by the main graph
# (/api/graph), its legend, and the per-concept timeline subgraph below, so
# they always agree on what each color means.
WEEK_COLORS = ["#ffadad", "#ffd6a5", "#fdffb6", "#caffbf", "#9bf6ff", "#a0c4ff", "#bdb2ff", "#ffc6ff"]
MULTI_WEEK_COLOR = "#fcf6bd"  # Concepts spanning multiple weeks
DEFAULT_COLOR = "#d1d5db"     # Concepts with no week data

LLM_MODEL = "llama-3.3-70b-versatile"


def get_week_color_map(engine):
    """Week name -> color, numerically sorted (e.g. "Week 1", "Week 2", ...)."""
    sorted_weeks = sorted(
        engine.uploads_by_week.keys(),
        key=lambda w: int(w.split(' ')[1]) if ' ' in w else 0,
    )
    return {week: WEEK_COLORS[i % len(WEEK_COLORS)] for i, week in enumerate(sorted_weeks)}


# --- API Functions ---

def get_graph_data(engine, week=None):
    """
    Dynamically generates the main evolution graph from the engine's concepts.
    Colored the same way as /api/graph (see get_week_color_map) — a concept in
    one week gets that week's color, a concept spanning multiple weeks gets
    MULTI_WEEK_COLOR — so the Evolution page's graph and its legend agree with
    the rest of the app instead of rendering every node in one flat color.
    """
    if not engine or not engine.concepts:
        return {"nodes": [], "edges": [], "legend": {}}

    nodes = []
    edges = []
    concept_details = defaultdict(lambda: {"weeks": set(), "count": 0})

    # Aggregate concepts by name to find their weeks and importance
    for concept in engine.concepts:
        name = engine.canonical_nodes.get(concept["name"], concept["name"])
        concept_details[name]["weeks"].add(concept["week"])
        concept_details[name]["count"] += 1

    week_color_map = get_week_color_map(engine)

    # Create nodes for the evolution graph
    for name, data in concept_details.items():
        weeks = data["weeks"]
        if len(weeks) > 1:
            color = MULTI_WEEK_COLOR
        elif len(weeks) == 1:
            color = week_color_map.get(next(iter(weeks)), DEFAULT_COLOR)
        else:
            color = DEFAULT_COLOR
        nodes.append({
            "id": name,
            "concept_name": name,
            "importance": data["count"], # Importance is based on frequency
            "group": "concept",
            "color": color,
        })

    # Create edges between concepts that appear in the same week
    concept_names = list(concept_details.keys())
    for i in range(len(concept_names)):
        for j in range(i + 1, len(concept_names)):
            c1_weeks = concept_details[concept_names[i]]["weeks"]
            c2_weeks = concept_details[concept_names[j]]["weeks"]
            if c1_weeks.intersection(c2_weeks):
                edges.append({
                    "from_node": concept_names[i],
                    "to_node": concept_names[j],
                    "label": "related"
                })

    legend = {
        "weeks": [{"week": w, "color": c} for w, c in week_color_map.items()],
        "multi_week": {"label": "Spans multiple weeks", "color": MULTI_WEEK_COLOR},
        "no_week_data": {"label": "No week data", "color": DEFAULT_COLOR},
    }

    return {"nodes": nodes, "edges": edges, "legend": legend}


MAX_RETRIES = 4
BATCH_SIZE = 6  # concepts per LLM call. Was 3, tuned for llama-3.1-8b-instant's
                # 6,000 TPM cap — a 15-concept batch there was ~10,500 tokens,
                # rejected outright (413) no matter how many times it's
                # retried, since retrying an oversized request just fails
                # identically. LLM_MODEL is now llama-3.3-70b-versatile,
                # confirmed on console.groq.com/settings/limits at 12,000 TPM
                # (double llama-3.1-8b-instant's, not lower as once assumed —
                # see TPM_BUDGET below), so batches can run bigger: a 6-concept
                # batch still stays comfortably under budget, and halving the
                # number of calls matters more now since 70b-versatile's daily
                # request cap (1,000 RPD) is far tighter than 8b-instant's
                # (14,400 RPD).

# Batches within a single summarize_*_batch() call used to all fire via
# asyncio.gather with no cap — a course with many concepts could dispatch
# a dozen simultaneous requests the instant the previous phase finished,
# which is enough to blow through a per-minute limit in under a second
# even if the account's steady-state throughput would have been fine.
# This throttles how many of THIS module's requests are in flight at once.
GROQ_CONCURRENCY = asyncio.Semaphore(2)

# Confirmed on console.groq.com/settings/limits: llama-3.3-70b-versatile is
# 12,000 TPM (llama-3.1-8b-instant, despite being the smaller model, is only
# 6,000 TPM on this account — the opposite of what was originally assumed
# here). 10,500 keeps a -1,500 margin below the real 12,000 cap, since
# _estimate_tokens is a rough ~4-chars/token heuristic, not an exact
# tokenizer count.
TPM_BUDGET = 10500


class _TokenPacer:
    """
    Fixed-window tokens-per-minute limiter. Every Groq call reserves its
    estimated token cost here BEFORE sending — if that would exceed the
    budget for the current ~60s window, it waits for the window to roll
    over rather than firing and getting rejected with a 413.
    """
    def __init__(self, tpm_budget):
        self.tpm_budget = tpm_budget
        self._window_start = time.monotonic()
        self._used = 0
        self._lock = asyncio.Lock()

    async def acquire(self, estimated_tokens):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._window_start
            if elapsed >= 60:
                self._window_start = now
                self._used = 0
                elapsed = 0
            if self._used + estimated_tokens > self.tpm_budget:
                wait = 60 - elapsed
                if wait > 0:
                    print(f"Pacing for TPM budget — waiting {wait:.1f}s before next Groq call.")
                    await asyncio.sleep(wait)
                self._window_start = time.monotonic()
                self._used = 0
            self._used += estimated_tokens


_pacer = _TokenPacer(TPM_BUDGET)


def _estimate_tokens(text):
    # Rough ~4-chars-per-token heuristic — doesn't need to be exact, just
    # needs to keep us safely under budget rather than exactly at it.
    return max(1, len(text) // 4)


async def _call_with_retry(client, system_prompt, user_prompt, json_mode=False, temperature=0.2, max_tokens=None):
    """
    Calls Groq with real retry/backoff on rate limits — honors the
    Retry-After header when present, falls back to exponential backoff
    otherwise. Returns the response text, or None if every attempt fails
    (callers fall back to a plain template rather than crash the upload).

    A low temperature keeps summaries grounded in the excerpt rather than
    rambling. max_tokens is left uncapped by default (None) — callers that
    batch several concepts into one JSON response must pass an explicit cap
    sized for the whole batch, since a truncated response breaks JSON
    parsing for every item in it, not just one.
    """
    def call():
        kwargs = dict(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=LLM_MODEL,
            temperature=temperature,
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return client.chat.completions.create(**kwargs)

    estimated_tokens = _estimate_tokens(system_prompt) + _estimate_tokens(user_prompt) + (max_tokens or 0)

    for attempt in range(MAX_RETRIES + 1):
        try:
            await _pacer.acquire(estimated_tokens)
            async with GROQ_CONCURRENCY:
                response = await asyncio.to_thread(call)
            return response.choices[0].message.content
        except RateLimitError as e:
            if attempt == MAX_RETRIES:
                print(f"Groq rate limit exceeded after {MAX_RETRIES} retries — giving up on this batch.")
                return None
            retry_after = None
            try:
                retry_after = float(e.response.headers.get("retry-after"))
            except (AttributeError, TypeError, ValueError):
                pass
            # Without a Retry-After header, assume a per-minute quota and
            # ramp toward it (5s, 15s, 30s, 60s) rather than giving up after
            # a token-bucket-style short backoff — a real RPM limit needs
            # up to a full minute to reset, not ~14s.
            delay = retry_after if retry_after is not None else min(60, 5 * (3 ** attempt))
            print(f"Rate limited (attempt {attempt + 1}/{MAX_RETRIES}) — retrying in {delay:.1f}s.")
            await asyncio.sleep(delay)
        except BadRequestError as e:
            # Groq validates json_object-mode output server-side and returns
            # this (code "json_validate_failed") when the model's own
            # generation isn't valid JSON — not a quota issue, so no reason
            # to wait long; just try again, since sampling is nondeterministic.
            if attempt == MAX_RETRIES:
                print(f"Groq rejected malformed JSON generation after {MAX_RETRIES} retries — giving up on this batch: {e}")
                return None
            print(f"Groq rejected malformed JSON generation (attempt {attempt + 1}/{MAX_RETRIES}) — retrying.")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Groq call failed: {e}")
            return None
    return None


def _batches(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def _call_and_parse_json(client, system_prompt, user_prompt, temperature, max_tokens):
    """
    Calls Groq expecting {"items": [{"key": "...", "text": "..."}, ...]} and
    returns a plain dict of key -> text.

    An array of small, uniform objects (rather than one flat object keyed by
    arbitrary strings like "concept name|Week 1") is a far more natural
    pattern for a model to generate correctly — a smaller model asked for
    the latter has been observed inventing literal "KEY"/"summary" fields
    instead of actually using each item's real key, producing invalid JSON
    that Groq's own json_object-mode validation rejects outright.

    Also retries when the response comes back non-JSON or truncated mid-
    object — a batched call failing loses every item in that batch, not
    just one, and for a concept that never appears again there's no later
    upload that would naturally retry it. Returns {} if every attempt fails.
    """
    for attempt in range(MAX_RETRIES + 1):
        raw = await _call_with_retry(client, system_prompt, user_prompt, json_mode=True, temperature=temperature, max_tokens=max_tokens)
        if not raw:
            return {}  # _call_with_retry already exhausted its own retries
        try:
            parsed = json.loads(raw)
            items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
            return {
                item["key"]: item["text"]
                for item in items
                if isinstance(item, dict) and isinstance(item.get("key"), str) and isinstance(item.get("text"), str)
            }
        except (json.JSONDecodeError, TypeError, AttributeError):
            if attempt == MAX_RETRIES:
                print("Evolution batch: failed to parse JSON response after retries — giving up.")
                return {}
            print(f"Evolution batch: malformed JSON response (attempt {attempt + 1}/{MAX_RETRIES}) — retrying.")
    return {}


# Per-item token budgets, scaled by batch size to bound the whole JSON
# response — a truncated batch response fails to parse for every item in
# it, not just one, so these need headroom rather than a tight fit.
SUMMARY_TOKENS_PER_ITEM = 250
COMPARISON_TOKENS_PER_ITEM = 180
BATCH_TOKEN_OVERHEAD = 150


async def summarize_weeks_batch(client, requests, engine):
    """
    Batches many (concept, week) weekly-summary requests into a handful of
    LLM calls instead of one call per concept — the biggest lever against
    hitting Groq's per-minute request limit during a big upload.

    `requests`: list of {"key", "concept_name", "week", "chunks"}.
    Returns dict key -> summary text (missing/failed keys are simply absent,
    so the caller can fall back to a plain template for those).
    """
    if not requests:
        return {}

    results = {}
    system_prompt = (
        "You are a study assistant. For each concept below, given an excerpt of course "
        "material, explain how that concept was presented in it: its definition, scope, and "
        "any notable details. Write 2-4 sentences per concept, in your own words, grounded "
        "only in its excerpt — do not invent information that isn't there. End each summary "
        "with a citation in the form '(Covered in Slide N)' or '(Covered in Slide N-M)', using "
        "the page number(s) shown in that concept's [brackets]. "
        'Respond with JSON: {"items": [{"key": "<the exact KEY given>", "text": "<the summary>"}, ...]}, '
        "one entry per concept below, in the same order."
    )

    batches = list(_batches(requests, BATCH_SIZE))
    total = len(batches)

    async def run_batch(i, batch):
        print(f"Evolution summaries: batch {i}/{total} ({len(batch)} concepts)")
        sections = []
        for req in batch:
            blocks = []
            for c in req["chunks"]:
                citation = engine.format_citation(c["pages"])
                header = f"[{citation}]" if citation else "[unknown page]"
                blocks.append(f"{header}\n{c['text'][:600]}")
            context_text = "\n\n".join(blocks) or "(no excerpt available)"
            sections.append(
                f'KEY: {req["key"]}\nCONCEPT: {req["concept_name"]}\n{req["week"]} EXCERPT:\n{context_text}'
            )
        user_prompt = "\n\n---\n\n".join(sections)

        max_tokens = BATCH_TOKEN_OVERHEAD + SUMMARY_TOKENS_PER_ITEM * len(batch)
        data = await _call_and_parse_json(client, system_prompt, user_prompt, temperature=0.2, max_tokens=max_tokens)
        for req in batch:
            text = data.get(req["key"])
            if isinstance(text, str) and text.strip():
                results[req["key"]] = text.strip()
        print(f"Evolution summaries: batch {i}/{total} done")

    await asyncio.gather(*[run_batch(i, b) for i, b in enumerate(batches, start=1)])
    return results


async def summarize_comparisons_batch(client, requests):
    """
    Batches many week-to-week comparison requests into a handful of LLM
    calls. `requests`: list of {"key", "concept_name", "from_week",
    "from_text", "to_week", "to_text"}. Returns dict key -> comparison text.
    """
    if not requests:
        return {}

    results = {}
    system_prompt = (
        "You are a study assistant. For each concept below, contrast how it was presented in "
        "two different weeks of course material. Your answer must name the CONCRETE DIFFERENCE "
        "between the weeks — e.g. what specific detail, example, formula, scope, or nuance is "
        "new, dropped, or expanded in the later week compared to the earlier one. Do not write "
        "vague continuity statements like 'it continued to be discussed' or 'it was reinforced' "
        "without saying what actually changed. Write 1-3 sentences per concept, grounded only in "
        "the excerpts given — do not invent information that isn't there. If the excerpts truly "
        "show no difference in how the concept is treated, say so explicitly and name what stayed "
        "the same (e.g. 'both weeks give the same definition, with no new detail added'). "
        'Respond with JSON: {"items": [{"key": "<the exact KEY given>", "text": "<the comparison>"}, ...]}, '
        "one entry per concept below, in the same order."
    )

    batches = list(_batches(requests, BATCH_SIZE))
    total = len(batches)

    async def run_batch(i, batch):
        print(f"Evolution comparisons: batch {i}/{total} ({len(batch)} concepts)")
        sections = []
        for req in batch:
            if not req["from_text"] or not req["to_text"]:
                continue
            sections.append(
                f'KEY: {req["key"]}\nCONCEPT: {req["concept_name"]}\n'
                f'{req["from_week"]} EXCERPT:\n{req["from_text"][:600]}\n\n'
                f'{req["to_week"]} EXCERPT:\n{req["to_text"][:600]}'
            )
        if not sections:
            print(f"Evolution comparisons: batch {i}/{total} skipped (nothing to compare)")
            return
        user_prompt = "\n\n---\n\n".join(sections)

        max_tokens = BATCH_TOKEN_OVERHEAD + COMPARISON_TOKENS_PER_ITEM * len(sections)
        data = await _call_and_parse_json(client, system_prompt, user_prompt, temperature=0.25, max_tokens=max_tokens)
        for req in batch:
            text = data.get(req["key"])
            if isinstance(text, str) and text.strip():
                results[req["key"]] = text.strip()
        print(f"Evolution comparisons: batch {i}/{total} done")

    await asyncio.gather(*[run_batch(i, b) for i, b in enumerate(batches, start=1)])
    return results


async def precompute_week_summaries(engine, client, raw_week_pairs, summary_pairs=None):
    """
    Given a set of (raw_concept_name, week) pairs, retrieves/caches their
    source excerpts (no LLM) and generates any missing weekly summaries.

    Callers should pass as many pairs as they have available in one call —
    summarize_weeks_batch groups whatever it's given into BATCH_SIZE-sized
    LLM calls internally, so batching only actually happens if this is
    called with many concepts' pairs at once. Calling it once per concept
    (e.g. in a loop) defeats that: each call would only ever have that one
    concept's own 1-2 weeks to batch, turning what should be a handful of
    3-at-a-time LLM calls into one call per concept.

    `summary_pairs`, if given, restricts which pairs actually get a *new*
    summary generated — retrieval still runs for every pair in
    raw_week_pairs regardless (it's free/local). Defaults to raw_week_pairs
    itself. get_concept_detail passes a deduped subset here: several raw
    spellings that fuse into one canonical concept can share a week (e.g.
    "ML" and "Machine Learning" both in Week 1), but only one spelling's
    summary is ever displayed for that week, so requesting a fresh LLM
    summary for every spelling would waste tokens on text nothing shows.
    The per-document upload pipeline has no such grouping yet (fusion
    hasn't run), so it leaves this at the default — there, distinct raw
    pairs are genuinely distinct concepts, not spelling variants of one.
    """
    if not raw_week_pairs:
        return

    # Cheap, local-only: make sure a retrieved excerpt is cached for every
    # (raw name, week) pair (no LLM call).
    for raw, week in raw_week_pairs:
        if (raw, week) not in engine.concept_week_chunks:
            query = f"Detailed explanation and definition of {raw}, not just a passing mention"
            engine.concept_week_chunks[(raw, week)] = engine.retrieve_chunks_for_week(query, week, top_k=3)

    target_pairs = summary_pairs if summary_pairs is not None else raw_week_pairs
    summary_requests = [
        {"key": f"{raw}|{week}", "concept_name": raw, "week": week,
         "chunks": engine.concept_week_chunks.get((raw, week), [])}
        for raw, week in target_pairs
        if (raw, week) not in engine.concept_week_summaries
    ]
    if not summary_requests:
        return

    summaries = await summarize_weeks_batch(client, summary_requests, engine)
    for req in summary_requests:
        text = summaries.get(req["key"])
        if not text:
            # Cache a fallback too — otherwise a failed generation (e.g.
            # Groq already rate-limited from a big batch) stays permanently
            # "missing", and every later view of this concept re-fires a
            # live Groq call for it instead of reading from cache, which is
            # exactly the click-triggers-an-LLM-call behavior the caching
            # in get_concept_detail is meant to avoid.
            #
            # The fallback itself is grounded in the actual retrieved PDF
            # excerpt (already fetched above, no extra LLM call) rather than
            # a content-free "appeared in document X" note — a concept whose
            # summary generation failed should still show real material from
            # the source, not a stub with no explanation of the concept.
            chunks = req["chunks"]
            if chunks:
                excerpt = chunks[0]["text"].strip()
                snippet = excerpt[:500].rsplit(". ", 1)[0] + "." if len(excerpt) > 500 else excerpt
                citation = engine.format_citation(chunks[0]["pages"])
                citation_str = f" ({citation})" if citation else ""
                text = f"{snippet}{citation_str}"
            else:
                docs = sorted({
                    c["source"] for c in engine.concepts
                    if c["name"] == req["concept_name"] and c["week"] == req["week"]
                })
                text = f"Appeared in document(s): {', '.join(docs) if docs else 'unknown'} — no excerpt could be retrieved."
        engine.concept_week_summaries[(req["concept_name"], req["week"])] = text


def _dedupe_summary_targets(raw_week_pairs, engine):
    """
    raw_week_pairs is all-one-concept by construction in get_concept_detail
    (every entry is a raw spelling that fused into the same canonical
    concept), so unlike precompute_week_summaries' generic input, it's safe
    to collapse to one raw name per week here. Picks a spelling that
    already has a cached summary if one exists (costs nothing); otherwise
    the spelling with the most occurrences that week, ties broken
    alphabetically for determinism.
    """
    by_week = defaultdict(list)
    for raw, week in raw_week_pairs:
        by_week[week].append(raw)

    chosen = set()
    for week, raw_names in by_week.items():
        cached = next((raw for raw in raw_names if (raw, week) in engine.concept_week_summaries), None)
        if cached is not None:
            chosen.add((cached, week))
            continue
        counts = Counter(raw_names)
        best = sorted(set(raw_names), key=lambda r: (-counts[r], r))[0]
        chosen.add((best, week))
    return chosen


async def synthesize_overall_evolution(client, concept_name, consecutive_comparisons):
    """
    Synthesizes a concept's first-to-last "overall evolution" summary from
    the per-transition comparisons already generated for it, instead of
    re-sending raw first-week/last-week excerpts through another Groq call
    (as summarize_comparisons_batch would). Cheaper — the input is a
    handful of already-grounded sentences rather than ~1200 chars of raw
    excerpt — and arguably more accurate too, since it's built from
    verified per-transition diffs instead of asking the model to eyeball
    two distant excerpts directly. Only used for concepts spanning more
    than 2 weeks; the 2-week case reuses its single consecutive comparison
    directly (see get_concept_detail).
    """
    system_prompt = (
        "You are a study assistant. You are given a chronological sequence of "
        "week-to-week comparisons already written for one concept. Write a single "
        "1-3 sentence summary of its overall arc from the first week to the last, "
        "grounded only in what these comparisons say — do not invent any detail "
        "that isn't in them."
    )
    # Skip placeholder text from transitions whose own generation failed —
    # feeding "Comparison unavailable..." to the model as if it were real
    # signal produces a hallucination-adjacent "overall" grounded in nothing.
    usable = [c for c in consecutive_comparisons if not c["comparison"].startswith("Comparison unavailable")]
    if not usable:
        return None
    steps = "\n".join(f"{c['from_week']} -> {c['to_week']}: {c['comparison']}" for c in usable)
    user_prompt = f"CONCEPT: {concept_name}\n\n{steps}"
    max_tokens = BATCH_TOKEN_OVERHEAD + COMPARISON_TOKENS_PER_ITEM
    text = await _call_with_retry(client, system_prompt, user_prompt, temperature=0.25, max_tokens=max_tokens)
    return text.strip() if text else None


async def get_concept_description(engine, concept_name, client):
    """
    Cheap "what is this concept" tier: a single grounded summary from the
    concept's earliest-appearance week only — one LLM call regardless of
    how many weeks it actually spans. Meant to back a plain node click in
    the graph UI (general graph or Evolution graph alike). get_concept_detail
    is the separate, explicit "show concept evolution" action: it costs one
    call per week plus one per week-to-week transition, so it stays gated
    behind that explicit request instead of running on every click.
    """
    if not engine:
        return None

    occurrences = [c for c in engine.concepts if engine.canonical_nodes.get(c["name"], c["name"]) == concept_name]
    if not occurrences:
        return None

    weeks_data_raw = defaultdict(list)
    for occ in occurrences:
        weeks_data_raw[occ["week"]].append(occ["source"])
    first_week = min(weeks_data_raw.keys(), key=lambda w: int(w.split(" ")[1]) if ' ' in w else 0)

    raw_pairs = {(occ["name"], occ["week"]) for occ in occurrences if occ["week"] == first_week}
    await precompute_week_summaries(engine, client, raw_pairs, summary_pairs=_dedupe_summary_targets(raw_pairs, engine))

    summary = next(
        (engine.concept_week_summaries[(raw, w)] for raw, w in raw_pairs if (raw, w) in engine.concept_week_summaries),
        None,
    ) or f"Appeared in document(s): {', '.join(set(weeks_data_raw[first_week]))}"

    return {
        "concept_name": concept_name,
        "week": first_week,
        "summary": summary,
        "appears_in_weeks": len(weeks_data_raw),
    }


async def get_concept_detail(engine, concept_name, client):
    """
    Returns full evolution information for a single concept — every week's
    summary plus every week-to-week comparison — generating and caching
    whatever hasn't been generated yet. Called on demand only, from the
    explicit "show concept evolution" action in the UI (not on a plain node
    click; see get_concept_description for that cheaper tier, and not
    eagerly at upload/fuse time — a concept the user never asks to see the
    evolution of never costs anything beyond get_concept_description's one
    call). Summaries and comparisons are cached forever once generated, so
    calling this again for the same concept is free.
    """
    if not engine:
        return None

    occurrences = [c for c in engine.concepts if engine.canonical_nodes.get(c["name"], c["name"]) == concept_name]
    if not occurrences:
        return None

    # A canonical name can be the fusion of several raw spellings.
    raw_week_pairs = {(occ["name"], occ["week"]) for occ in occurrences}

    weeks_data_raw = defaultdict(list)
    for occ in occurrences:
        weeks_data_raw[occ["week"]].append(occ["source"])
    sorted_weeks = sorted(weeks_data_raw.keys(), key=lambda w: int(w.split(" ")[1]) if ' ' in w else 0)

    # Generate (and cache) a grounded definition for any week that doesn't
    # have one yet — this is the concept's core "what is it" explanation,
    # needed whether or not it turns out to span multiple weeks. Delegates
    # to precompute_week_summaries so a single concept viewed on-demand and
    # a whole document precomputed in bulk share the same batching path.
    # summary_pairs is deduped to one raw spelling per week (see
    # _dedupe_summary_targets) — retrieval still happens for every spelling,
    # but only one per week ever gets an LLM summary requested, since only
    # one is ever shown in weeks_data below.
    await precompute_week_summaries(engine, client, raw_week_pairs, summary_pairs=_dedupe_summary_targets(raw_week_pairs, engine))

    def excerpt_for(week):
        texts = []
        for raw, w in raw_week_pairs:
            if w == week:
                texts.extend(c["text"] for c in engine.concept_week_chunks.get((raw, week), []))
        return " ".join(texts)

    def fallback_week_summary(week):
        docs = ", ".join(set(weeks_data_raw[week]))
        return f"Appeared in document(s): {docs}"

    weeks_data = []
    for week in sorted_weeks:
        summary = next(
            (engine.concept_week_summaries[(raw, w)] for raw, w in raw_week_pairs
             if w == week and (raw, w) in engine.concept_week_summaries),
            None,
        )
        weeks_data.append({"week": week, "summary": summary or fallback_week_summary(week)})

    # Comparisons only make sense for concepts spanning more than one week,
    # and are cached under the canonical concept_name (not per raw spelling)
    # since they're computed fresh here from the full occurrence set.
    consecutive_comparisons = engine.concept_consecutive_comparisons.get(concept_name)
    if consecutive_comparisons is None:
        consecutive_comparisons = []
        if len(sorted_weeks) > 1:
            pairs = list(zip(sorted_weeks, sorted_weeks[1:]))
            comparison_requests = [
                {"key": f"{fw}|{tw}", "concept_name": concept_name,
                 "from_week": fw, "from_text": excerpt_for(fw), "to_week": tw, "to_text": excerpt_for(tw)}
                for fw, tw in pairs
            ]
            comparisons = await summarize_comparisons_batch(client, comparison_requests)
            for fw, tw in pairs:
                text = comparisons.get(f"{fw}|{tw}") or (
                    f"Comparison unavailable — the {fw} vs. {tw} diff for this concept couldn't "
                    f"be generated. It did appear in both weeks, but no concrete difference could "
                    f"be computed this time."
                )
                consecutive_comparisons.append({"from_week": fw, "to_week": tw, "comparison": text})
        engine.concept_consecutive_comparisons[concept_name] = consecutive_comparisons

    first_week, last_week = sorted_weeks[0], sorted_weeks[-1]
    comparison = engine.concept_overall_comparison.get(concept_name)
    if comparison is None:
        if first_week == last_week:
            comparison = f"This concept only appears in {first_week}, from '{weeks_data_raw[first_week][0]}'."
        elif len(sorted_weeks) == 2:
            # "first vs last" IS "week 1 vs week 2" here — reuse rather than
            # asking the same question twice.
            comparison = consecutive_comparisons[0]["comparison"] if consecutive_comparisons else (
                f"The concept first appeared in {first_week} and was last seen in {last_week}."
            )
        else:
            comparison = await synthesize_overall_evolution(client, concept_name, consecutive_comparisons) or (
                f"The concept first appeared in {first_week} (from '{weeks_data_raw[first_week][0]}') "
                f"and was last seen in {last_week} (from '{weeks_data_raw[last_week][0]}')."
            )
        engine.concept_overall_comparison[concept_name] = comparison

    # Find related concepts from the main graph
    all_related = set()
    working_edges = engine.fused_edges if engine.fused_edges else engine.edges
    for edge in working_edges:
        if edge["from"] == concept_name:
            all_related.add(edge["to"])
        if edge["to"] == concept_name:
            all_related.add(edge["from"])

    return {
        "concept_name": concept_name,
        "comparison": comparison,
        "consecutive_comparisons": consecutive_comparisons,
        "weeks_data": weeks_data,
        "related_concepts": list(all_related)
    }


def get_concept_subgraph(engine, concept_name):
    """
    Dynamically generates a D3-ready subgraph for a concept's weekly evolution,
    colored with the same week palette as the main graph.
    """
    if not engine:
        return None

    occurrences = [c for c in engine.concepts if engine.canonical_nodes.get(c["name"], c["name"]) == concept_name]
    if not occurrences:
        return None

    present_weeks = sorted(
        set(o["week"] for o in occurrences),
        key=lambda w: int(w.split(" ")[1]) if ' ' in w else 0,
    )
    week_color_map = get_week_color_map(engine)
    concept_color = (
        MULTI_WEEK_COLOR if len(present_weeks) > 1
        else week_color_map.get(present_weeks[0], DEFAULT_COLOR) if present_weeks
        else DEFAULT_COLOR
    )

    nodes = [{"id": concept_name, "label": concept_name, "group": "concept", "color": concept_color}]
    edges = []

    for week in present_weeks:
        week_id = f"{concept_name}_{week.replace(' ', '')}"
        nodes.append({"id": week_id, "label": week, "group": "week", "color": week_color_map.get(week, DEFAULT_COLOR)})
        edges.append({"source": concept_name, "target": week_id, "label": "appears in"})

    for i in range(len(present_weeks) - 1):
        from_week_id = f"{concept_name}_{present_weeks[i].replace(' ', '')}"
        to_week_id = f"{concept_name}_{present_weeks[i+1].replace(' ', '')}"
        edges.append({"source": from_week_id, "target": to_week_id, "label": "evolved to"})

    return {"nodes": nodes, "edges": edges}
