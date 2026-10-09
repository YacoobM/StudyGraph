import asyncio
import json
import time
from collections import Counter, defaultdict

from app.config import EXTRACTION_MODEL, NON_RAG_REASONING_EFFORT
from services.llm_gateway import estimate_tokens, graph_limiter
from services.llm_provider import BAD_REQUEST_ERRORS, RATE_LIMIT_ERRORS

from app.logging_config import get_logger

from utils.retry import retry_async

WEEK_COLORS = ["#ffadad", "#ffd6a5", "#fdffb6", "#caffbf", "#9bf6ff", "#a0c4ff", "#bdb2ff", "#ffc6ff"]
MULTI_WEEK_COLOR = "#fcf6bd"
DEFAULT_COLOR = "#d1d5db"

LLM_MODEL = EXTRACTION_MODEL
logger = get_logger(__name__)


def get_week_color_map(engine):
    sorted_weeks = sorted(
        engine.uploads_by_week.keys(),
        key=lambda w: int(w.split(' ')[1]) if ' ' in w else 0,
    )
    return {week: WEEK_COLORS[i % len(WEEK_COLORS)] for i, week in enumerate(sorted_weeks)}


def get_graph_data(engine, week=None):
    if not engine or not engine.concepts:
        return {"nodes": [], "edges": [], "legend": {}}

    nodes = []
    edges = []
    concept_details = defaultdict(lambda: {"weeks": set(), "count": 0})

    for concept in engine.concepts:
        name = engine.canonical_nodes.get(concept["name"], concept["name"])
        concept_details[name]["weeks"].add(concept["week"])
        concept_details[name]["count"] += 1

    week_color_map = get_week_color_map(engine)

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
            "importance": data["count"],
            "group": "concept",
            "color": color,
        })

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


MAX_RETRIES = 5
BATCH_SIZE = 3

LLM_CONCURRENCY = asyncio.Semaphore(2)


@retry_async(
    on=RATE_LIMIT_ERRORS + BAD_REQUEST_ERRORS,
    max_attempts=5,
    backoff=2.0,
)
async def _call_with_retry(client, system_prompt, user_prompt, json_mode=False, max_tokens=None):
    """Call LLM with automatic retry on rate limits and bad requests."""
    kwargs = dict(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=LLM_MODEL,
        reasoning_effort=NON_RAG_REASONING_EFFORT,
    )
    if max_tokens is not None:
        kwargs["max_completion_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    
    estimated_input_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)
    estimated_output_tokens = max_tokens or 1
    
    await graph_limiter.acquire(estimated_input_tokens, estimated_output_tokens)
    async with LLM_CONCURRENCY:
        response = await asyncio.to_thread(lambda: client.chat.completions.create(**kwargs))
    return response.choices[0].message.content


def _batches(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def _call_and_parse_json(client, system_prompt, user_prompt, max_tokens):
    for attempt in range(5):
        raw = await _call_with_retry(client, system_prompt, user_prompt, json_mode=False, max_tokens=max_tokens)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
            result = {}
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        key = item.get("key")
                        text = item.get("text")
                        if key and text:
                            result[str(key)] = str(text)
            return result
        except (json.JSONDecodeError, TypeError, AttributeError):
            if attempt == 4:
                logger.error("evolution_json_parse_exhausted retries=5")
                return {}
            logger.warning("evolution_json_parse_failed attempt=%d/5", attempt + 1)
            await asyncio.sleep(1)
    return {}


SUMMARY_TOKENS_PER_ITEM = 250
COMPARISON_TOKENS_PER_ITEM = 180
BATCH_TOKEN_OVERHEAD = 150


def _invert_uploads_by_week(engine):
    return {doc: week for week, doc in engine.uploads_by_week.items()}


def _relations_for_concept_week(engine, raw_name, week, doc_to_week):
    out = []
    for e in engine.edges:
        if doc_to_week.get(e.get("source")) != week:
            continue
        if e["from"] == raw_name:
            out.append({"direction": "from", "other": e["to"], "relation": e["relation"],
                        "summary": e.get("summary", ""),
                        "chunk_ids": e.get("chunk_ids", [e.get("chunk_id", "")])})
        elif e["to"] == raw_name:
            out.append({"direction": "to", "other": e["from"], "relation": e["relation"],
                        "summary": e.get("summary", ""),
                        "chunk_ids": e.get("chunk_ids", [e.get("chunk_id", "")])})
    return out


def _format_relation_evidence(concept_name, relations, engine=None):
    if not relations:
        return ""
    lines = []
    for r in relations:
        if r["direction"] == "from":
            phrase = f"{concept_name} {r['relation']} {r['other']}"
        else:
            phrase = f"{r['other']} {r['relation']} {concept_name}"
        detail = r["summary"]
        evidence = ""
        if engine:
            resolved = []
            for chunk_id in r.get("chunk_ids", []):
                if chunk_id:
                    chunk = engine.resolve_chunk(chunk_id)
                    if chunk:
                        citation = engine.format_citation(chunk["pages"])
                        resolved.append(
                            f"[{citation}]\n{chunk['text'][:800]}"
                            if citation else chunk["text"][:800]
                        )
            if resolved:
                evidence = "\n".join(resolved)
        lines.append(f"- {phrase}" + (f": {detail}" if detail else "") + evidence)
    return "\n".join(lines)


def _fallback_text_from_relations(relations, chunks, format_citation, docs_label):
    if relations:
        pieces = [r["summary"] for r in relations if r.get("summary")]
        if pieces:
            return " ".join(dict.fromkeys(pieces))
    if chunks:
        excerpt = chunks[0]["text"].strip()
        snippet = excerpt[:500].rsplit(". ", 1)[0] + "." if len(excerpt) > 500 else excerpt
        citation = format_citation(chunks[0]["pages"])
        citation_str = f" ({citation})" if citation else ""
        return f"{snippet}{citation_str}"
    return f"Appeared in: {docs_label or 'unknown'}"


async def summarize_weeks_batch(client, requests, engine):
    if not requests:
        return {}

    results = {}
    system_prompt = (
        "For each concept, synthesize its relationships into 2-4 sentences describing "
        "how it was presented that week: role, scope, connections. Do not invent detail. "
        "If given a raw excerpt with [page] citation, explain the concept and end with a citation. "
        'Respond: {"items": [{"key": "<KEY>", "text": "<summary>"}, ...]}'
    )

    batches = list(_batches(requests, BATCH_SIZE))
    total = len(batches)

    for i, batch in enumerate(batches, start=1):
        batch_started = time.perf_counter()
        logger.info("evolution_summary_batch_started batch=%d/%d concepts=%d", i, total, len(batch))
        sections = []
        for req in batch:
            if req.get("evidence"):
                material = req["evidence"]
            else:
                blocks = []
                for c in req.get("chunks", []):
                    citation = engine.format_citation(c["pages"])
                    header = f"[{citation}]" if citation else "[unknown page]"
                    blocks.append(f"{header}\n{c['text'][:600]}")
                material = "\n\n".join(blocks) or "(no evidence)"
            sections.append(
                f'KEY: {req["key"]}\nCONCEPT: {req["concept_name"]}\n{req["week"]} EVIDENCE:\n{material}'
            )
        user_prompt = "\n\n---\n\n".join(sections)

        max_tokens = BATCH_TOKEN_OVERHEAD + SUMMARY_TOKENS_PER_ITEM * len(batch)
        data = await _call_and_parse_json(client, system_prompt, user_prompt, max_tokens=max_tokens)
        for req in batch:
            text = data.get(req["key"])
            if isinstance(text, str) and text.strip():
                results[req["key"]] = text.strip()
        logger.info(
            "evolution_summary_batch_complete batch=%d/%d concepts=%d duration_ms=%.1f results=%d",
            i,
            total,
            len(batch),
            (time.perf_counter() - batch_started) * 1000,
            len(data),
        )

    return results


async def generate_concept_definitions_batch(client, requests):
    """Generate first-class concept definitions from grounded document evidence."""
    if not requests:
        return {}

    results = {}
    system_prompt = (
        "For each concept, write a concise 2-4 sentence definition using only the supplied document evidence. "
        "Explain what the concept means and its important connections. Do not invent detail. "
        'Respond: {"items": [{"key": "<KEY>", "text": "<definition>"}, ...]}'
    )
    batches = list(_batches(requests, BATCH_SIZE))
    for index, batch in enumerate(batches, start=1):
        logger.info(
            "concept_definition_batch_started batch=%d/%d concepts=%d",
            index, len(batches), len(batch),
        )
        sections = [
            f'KEY: {request["key"]}\nCONCEPT: {request["concept_name"]}\nEVIDENCE:\n{request.get("evidence", "")}'
            for request in batch
        ]
        data = await _call_and_parse_json(
            client,
            system_prompt,
            "\n\n---\n\n".join(sections),
            max_tokens=BATCH_TOKEN_OVERHEAD + SUMMARY_TOKENS_PER_ITEM * len(batch),
        )
        for request in batch:
            text = data.get(request["key"])
            if isinstance(text, str) and text.strip():
                results[request["key"]] = text.strip()
        logger.info(
            "concept_definition_batch_complete batch=%d/%d concepts=%d results=%d",
            index, len(batches), len(batch), len(data),
        )
    return results


async def summarize_comparisons_batch(client, requests):
    if not requests:
        return {}

    results = {}
    system_prompt = (
        "Contrast how a concept was presented in two weeks of material. Name the CONCRETE DIFFERENCE — "
        "what relationship, connection, or detail is new, dropped, or expanded. Do not write vague continuity. "
        "Write 1-3 sentences, grounded only in evidence. If no difference, name what stayed the same. "
        'Respond: {"items": [{"key": "<KEY>", "text": "<comparison>"}, ...]}'
    )

    batches = list(_batches(requests, BATCH_SIZE))
    total = len(batches)

    for i, batch in enumerate(batches, start=1):
        batch_started = time.perf_counter()
        logger.info("evolution_comparison_batch_started batch=%d/%d concepts=%d", i, total, len(batch))
        sections = []
        for req in batch:
            if not req["from_text"] or not req["to_text"]:
                continue
            sections.append(
                f'KEY: {req["key"]}\nCONCEPT: {req["concept_name"]}\n'
                f'{req["from_week"]} EVIDENCE:\n{req["from_text"][:600]}\n\n'
                f'{req["to_week"]} EVIDENCE:\n{req["to_text"][:600]}'
            )
        if not sections:
            logger.info("evolution_comparison_batch_skipped batch=%d/%d", i, total)
            continue
        user_prompt = "\n\n---\n\n".join(sections)

        max_tokens = BATCH_TOKEN_OVERHEAD + COMPARISON_TOKENS_PER_ITEM * len(sections)
        data = await _call_and_parse_json(client, system_prompt, user_prompt, max_tokens=max_tokens)
        for req in batch:
            text = data.get(req["key"])
            if isinstance(text, str) and text.strip():
                results[req["key"]] = text.strip()
        logger.info(
            "evolution_comparison_batch_complete batch=%d/%d concepts=%d duration_ms=%.1f results=%d",
            i,
            total,
            len(batch),
            (time.perf_counter() - batch_started) * 1000,
            len(data),
        )

    return results


async def precompute_week_summaries(engine, client, raw_week_pairs, summary_pairs=None):
    if not raw_week_pairs:
        return

    doc_to_week = _invert_uploads_by_week(engine)
    target_pairs = summary_pairs if summary_pairs is not None else raw_week_pairs

    for raw, week in target_pairs:
        if (raw, week) not in engine.concept_week_chunks:
            relations = _relations_for_concept_week(engine, raw, week, doc_to_week)
            if relations:
                engine.concept_week_chunks[(raw, week)] = {"relations": relations, "chunks": []}
            else:
                query = f"Definition of {raw}"
                chunks = engine.retrieve_chunks_for_week(query, week, top_k=3)
                engine.concept_week_chunks[(raw, week)] = {"relations": [], "chunks": chunks}

    summary_requests = [
        {
            "key": f"{raw}|{week}",
            "concept_name": raw,
            "week": week,
            "evidence": _format_relation_evidence(
                raw,
                engine.concept_week_chunks.get((raw, week), {}).get("relations", []),
                engine,
            ),
            "chunks": engine.concept_week_chunks.get((raw, week), {}).get("chunks", []),
        }
        for raw, week in target_pairs
        if (raw, week) not in engine.concept_week_summaries
    ]
    if not summary_requests:
        return

    summaries = await summarize_weeks_batch(client, summary_requests, engine)
    for req in summary_requests:
        key = (req["concept_name"], req["week"])
        text = summaries.get(req["key"])
        if text:
            engine.degraded_summary_keys.discard(key)
        else:
            entry = engine.concept_week_chunks.get(key, {})
            docs = sorted({
                c["source"] for c in engine.concepts
                if c["name"] == req["concept_name"] and c["week"] == req["week"]
            })
            text = _fallback_text_from_relations(
                entry.get("relations", []),
                entry.get("chunks", []),
                engine.format_citation,
                ", ".join(docs),
            )
            engine.degraded_summary_keys.add(key)
        engine.concept_week_summaries[key] = text


def _dedupe_summary_targets(raw_week_pairs, engine):
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
    system_prompt = (
        "Given week-to-week comparisons for a concept, write a 1-3 sentence summary "
        "of its overall arc from first to last week, grounded only in what the comparisons say."
    )
    usable = [c for c in consecutive_comparisons if not c["comparison"].startswith("Comparison unavailable")]
    if not usable:
        return None
    steps = "\n".join(f"{c['from_week']} -> {c['to_week']}: {c['comparison']}" for c in usable)
    user_prompt = f"CONCEPT: {concept_name}\n\n{steps}"
    max_tokens = BATCH_TOKEN_OVERHEAD + COMPARISON_TOKENS_PER_ITEM
    text = await _call_with_retry(client, system_prompt, user_prompt, max_tokens=max_tokens)
    return text.strip() if text else None


async def get_concept_description(engine, concept_name, client):
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

    used_key = next((key for key in raw_pairs if key in engine.concept_week_summaries), None)
    summary = (
        engine.concept_week_summaries[used_key] if used_key
        else f"Appeared in: {', '.join(set(weeks_data_raw[first_week]))}"
    )

    return {
        "concept_name": concept_name,
        "week": first_week,
        "summary": summary,
        "is_fallback": bool(used_key and used_key in engine.degraded_summary_keys),
        "appears_in_weeks": len(weeks_data_raw),
    }


async def get_concept_detail(engine, concept_name, client):
    if not engine:
        return None

    occurrences = [c for c in engine.concepts if engine.canonical_nodes.get(c["name"], c["name"]) == concept_name]
    if not occurrences:
        return None

    raw_week_pairs = {(occ["name"], occ["week"]) for occ in occurrences}

    weeks_data_raw = defaultdict(list)
    for occ in occurrences:
        weeks_data_raw[occ["week"]].append(occ["source"])
    sorted_weeks = sorted(weeks_data_raw.keys(), key=lambda w: int(w.split(" ")[1]) if ' ' in w else 0)

    await precompute_week_summaries(engine, client, raw_week_pairs, summary_pairs=_dedupe_summary_targets(raw_week_pairs, engine))

    def excerpt_for(week):
        pieces = []
        for raw, w in raw_week_pairs:
            if w != week:
                continue
            entry = engine.concept_week_chunks.get((raw, week), {})
            ev = _format_relation_evidence(raw, entry.get("relations", []))
            if ev:
                pieces.append(ev)
            else:
                pieces.extend(c["text"] for c in entry.get("chunks", []))
        return "\n".join(pieces)

    def fallback_week_summary(week):
        docs = ", ".join(set(weeks_data_raw[week]))
        return f"Appeared in: {docs}"

    weeks_data = []
    for week in sorted_weeks:
        used_key = next(
            ((raw, w) for raw, w in raw_week_pairs if w == week and (raw, w) in engine.concept_week_summaries),
            None,
        )
        summary = engine.concept_week_summaries[used_key] if used_key else None
        weeks_data.append({
            "week": week,
            "summary": summary or fallback_week_summary(week),
            "is_fallback": bool(used_key and used_key in engine.degraded_summary_keys),
        })

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
                    f"Comparison unavailable — {fw} vs. {tw} diff could not be computed."
                )
                consecutive_comparisons.append({"from_week": fw, "to_week": tw, "comparison": text})
        engine.concept_consecutive_comparisons[concept_name] = consecutive_comparisons

    first_week, last_week = sorted_weeks[0], sorted_weeks[-1]
    comparison = engine.concept_overall_comparison.get(concept_name)
    if comparison is None:
        if first_week == last_week:
            comparison = f"Appears only in {first_week}"
        elif len(sorted_weeks) == 2:
            comparison = consecutive_comparisons[0]["comparison"] if consecutive_comparisons else (
                f"First in {first_week}, last in {last_week}"
            )
        else:
            comparison = await synthesize_overall_evolution(client, concept_name, consecutive_comparisons) or (
                f"First in {first_week}, last in {last_week}"
            )
        engine.concept_overall_comparison[concept_name] = comparison

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
