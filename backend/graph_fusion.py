import json
import logging
import re
import time
from itertools import combinations
from pathlib import Path

import numpy as np
from groq import BadRequestError, RateLimitError
from sklearn.metrics.pairwise import cosine_similarity
from app.config import OUTPUT_DIR

AUTO_MERGE_SIM = 0.95
LLM_LOW_SIM = 0.55
NEGATION_PREFIXES = ("un", "non", "in", "im", "dis", "ir", "a", "anti", "de", "mis")

_VERDICT_PAIR_BATCH = 4
_VERDICT_MAX_RETRIES = 4
_NEIGHBOURS_PER_NODE = 5
_EVIDENCE_CHUNKS_PER_NODE = 1
_EVIDENCE_SNIPPET_CHARS = 200

_STEP3_PROMPT_VERSION = "step3-evidence-v3"
_STEP5_PROMPT_VERSION = "step5-evidence-v3"
_CLUSTER_PROMPT_VERSION = "cluster-verify-v1"

VERDICT_CACHE_FILE = OUTPUT_DIR / "verdict_cache.json"

_BRIDGE_MIN_SHARED_NEIGHBOURS = 2
_HUB_DEGREE_PERCENTILE = 90.0
_HUB_MIN_ABSOLUTE_DEGREE = 8
logger = logging.getLogger(__name__)


_STRIP_CHARS = re.compile(r"[,;:\"'`''""]")
_WS = re.compile(r"\s+")


def normalize_concept(name):
    if not name:
        return ""
    text = str(name).lower()
    text = text.replace("-", " ")
    text = _STRIP_CHARS.sub("", text)
    return _WS.sub(" ", text).strip()


def _is_negation_variant(a, b):
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b or a == b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < 3:
        return False
    return any(longer == pre + shorter for pre in NEGATION_PREFIXES)


_HYPHEN_VARIANTS = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"}
_VAGUE_RELATIONS = {"is related to", "related to", "related", "relates to",
                    "is associated with", "associated with", "connected to",
                    "is connected to", "similar to", "is similar to"}


def _clean_relation(rel):
    rel = str(rel or "")
    for bad, good in _HYPHEN_VARIANTS.items():
        rel = rel.replace(bad, good)
    return _WS.sub(" ", rel).strip()


def _is_vague_relation(rel):
    return _clean_relation(rel).lower() in _VAGUE_RELATIONS


_HIER_BROADER_FIRST = {"generalizes", "generalises", "includes", "include",
                       "subsumes", "encompasses", "comprises", "contains",
                       "underlies", "underlie"}
_HIER_NARROWER_FIRST = {"is a", "is an", "is a type of", "type of", "is type of",
                        "is a kind of", "kind of", "is a form of", "form of",
                        "specializes", "specialises", "is a subtype of",
                        "subtype of", "is a special case of", "special case of",
                        "instance of", "is an instance of"}


def _is_hierarchical(rel):
    r = _clean_relation(rel).lower()
    if r in _HIER_BROADER_FIRST:
        return "broader_first"
    if r in _HIER_NARROWER_FIRST:
        return "narrower_first"
    if (r.startswith("is a ") or r.startswith("is an ") or r.startswith("type of")
            or "kind of" in r or "subtype" in r or "special case" in r
            or "instance of" in r or r.startswith("specialis") or r.startswith("specializ")):
        return "narrower_first"
    if (r.startswith("generali") or r.startswith("subsum") or r.startswith("underl")
            or r.startswith("encompass") or r in ("includes", "include", "contains", "comprises")):
        return "broader_first"
    return None


_PARTICIPLE_ACTIVE = {
    "defined": "defines", "satisfied": "satisfies", "used": "uses",
    "implemented": "implements", "supported": "supports", "described": "describes",
    "computed": "computes", "created": "creates", "caused": "causes",
    "required": "requires", "followed": "follows", "made": "makes",
    "built": "builds", "formed": "forms", "stored": "stores",
    "produced": "produces", "generated": "generates", "replaced": "replaces",
    "sorted": "sorts", "balanced": "balances", "ordered": "orders",
    "maintained": "maintains", "performed": "performs", "represented": "represents",
    "bounded": "bounds", "solved": "solves", "handled": "handles",
    "processed": "processes", "managed": "manages", "derived": "derives",
    "measured": "measures", "augmented": "augments", "preceded": "precedes",
    "achieved": "achieves", "given": "gives", "shown": "shows",
}
_PASSIVE_RE = re.compile(r"^(?:is|are|was|were|be|been|being)\s+(.+?)\s+by$")


def _passive_to_active(rel):
    r = _clean_relation(rel).lower()
    core = None
    m = _PASSIVE_RE.match(r)
    if m:
        core = m.group(1).strip()
    elif r.endswith(" by") and len(r.split()) <= 3:
        core = r[:-3].strip()
    if not core:
        return None
    toks = core.split()
    active = _PARTICIPLE_ACTIVE.get(toks[-1])
    if active is None:
        return None
    return " ".join(toks[:-1] + [active]).strip()


def _orient_relation(a, b, relation, direction, broader):
    relation = _clean_relation(relation)
    active = _passive_to_active(relation)
    swap_passive = active is not None
    if swap_passive:
        relation = active
    frm, to = (b, a) if str(direction) == "B_to_A" else (a, b)
    if swap_passive:
        frm, to = to, frm
    hier = _is_hierarchical(relation)
    if hier and str(broader).strip().upper() in ("A", "B"):
        broad = a if broader.strip().upper() == "A" else b
        narrow = b if broader.strip().upper() == "A" else a
        frm, to = (broad, narrow) if hier == "broader_first" else (narrow, broad)
    return frm, to, relation


def _load_verdict_cache():
    try:
        with open(VERDICT_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_verdict_cache(cache):
    try:
        VERDICT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(VERDICT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _pair_key(version, a, b):
    x, y = sorted((a, b))
    return f"{version}|qwen/qwen3.6-27b|{x}||{y}"


def _to_cache_record(a, b, r):
    direction = str(r.get("direction", "")).strip()
    canonical = str(r.get("canonical", "")).strip().upper()
    broader = str(r.get("broader", "")).strip().upper()
    if sorted((a, b))[0] != a:
        direction = {"A_to_B": "B_to_A", "B_to_A": "A_to_B"}.get(direction, direction)
        canonical = {"A": "B", "B": "A"}.get(canonical, canonical)
        broader = {"A": "B", "B": "A"}.get(broader, broader)
    return {"verdict": str(r.get("verdict", "unrelated")).lower().strip(),
            "relation": str(r.get("relation", "")).strip(),
            "direction": direction, "canonical": canonical, "broader": broader,
            "explanation": str(r.get("explanation", ""))[:200]}


def _from_cache_record(a, b, rec):
    direction = rec.get("direction", "")
    canonical = rec.get("canonical", "")
    broader = rec.get("broader", "")
    if sorted((a, b))[0] != a:
        direction = {"A_to_B": "B_to_A", "B_to_A": "A_to_B"}.get(direction, direction)
        canonical = {"A": "B", "B": "A"}.get(canonical, canonical)
        broader = {"A": "B", "B": "A"}.get(broader, broader)
    return {**rec, "direction": direction, "canonical": canonical, "broader": broader}


class _MalformedVerdict(Exception):
    pass


def _is_json_validate_failed(e):
    return "json_validate_failed" in str(e)


def _verdict_attempt(client, system, payload, log, tag):
    for attempt in range(_VERDICT_MAX_RETRIES + 1):
        reason, retry_after = None, None
        try:
            completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": "PAIRS:\n" + json.dumps(payload, ensure_ascii=False)},
                ],
                model="qwen/qwen3.6-27b",
                reasoning_effort="none",
                response_format={"type": "json_object"},
            )
            data = json.loads(completion.choices[0].message.content)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise _MalformedVerdict("no 'results' list")
            results = data["results"]
            if payload and not results:
                raise _MalformedVerdict("empty 'results' for non-empty batch")
            return results
        except RateLimitError as e:
            reason = "rate limited"
            try:
                retry_after = float(e.response.headers.get("retry-after"))
            except (AttributeError, TypeError, ValueError):
                pass
        except (_MalformedVerdict, json.JSONDecodeError) as e:
            reason = f"malformed ({type(e).__name__})"
        except BadRequestError as e:
            if not _is_json_validate_failed(e):
                log(f"{tag} rejected — defaults to unrelated")
                return None
            reason = "json_validate_failed"
        except Exception as e:
            reason = f"{type(e).__name__}"

        if attempt == _VERDICT_MAX_RETRIES:
            log(f"{tag} failed after {_VERDICT_MAX_RETRIES} retries ({reason})")
            return None
        delay = retry_after if retry_after is not None else min(60, 5 * (3 ** attempt))
        log(f"{tag} {reason} (attempt {attempt + 1}/{_VERDICT_MAX_RETRIES}) — waiting {delay:.1f}s")
        time.sleep(delay)
    return None


def _verdict_call(client, system, payload, log, tag):
    results = _verdict_attempt(client, system, payload, log, tag)
    if results is not None:
        return results
    if len(payload) <= 1:
        log(f"{tag} defaults to unrelated (not cached)")
        return []

    log(f"{tag} splitting batch of {len(payload)} into singles")
    salvaged = []
    for item in payload:
        one = _verdict_attempt(client, system, [item], log, f"{tag} [id={item.get('id')}]")
        if one:
            salvaged.extend(one)
    log(f"{tag} recovered {len(salvaged)}/{len(payload)} from singles")
    return salvaged


def _results_by_id(results):
    by_id = {}
    for r in results:
        if isinstance(r, dict) and "id" in r:
            try:
                by_id[int(r["id"])] = r
            except (TypeError, ValueError):
                continue
    return by_id


class _DSU:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self):
        g = {}
        for x in self.parent:
            g.setdefault(self.find(x), []).append(x)
        return g


class _EvidencePool:
    def __init__(self, engine, log, name_vecs):
        self._log = log
        self._missing_logged = False
        self.missing = 0
        self._name_vecs = name_vecs
        by_doc = {}
        for chunks in engine.chunks_by_week.values():
            for ch in chunks:
                doc = ch.get("doc_name")
                text = ch.get("text")
                if not doc or not text or ch.get("embedding") is None:
                    continue
                by_doc.setdefault(doc, []).append(ch)
        self._by_doc = {
            doc: (chunks, np.array([c["embedding"] for c in chunks]))
            for doc, chunks in by_doc.items()
        }

    def snippets(self, sources, query_name):
        query_vec = self._name_vecs.get(query_name)
        if query_vec is None:
            return []
        chunks, vectors = [], []
        for doc in sorted(sources):
            entry = self._by_doc.get(doc)
            if entry is None:
                continue
            chunks.extend(entry[0])
            vectors.append(entry[1])
        if not chunks:
            self.missing += 1
            if not self._missing_logged:
                self._missing_logged = True
                self._log("[fusion] no embedded chunks for node sources")
            return []
        sims = cosine_similarity(query_vec.reshape(1, -1), np.vstack(vectors))[0]
        order = sorted(range(len(chunks)), key=lambda i: (-sims[i], i))
        return [chunks[i]["text"][:_EVIDENCE_SNIPPET_CHARS]
                for i in order[:_EVIDENCE_CHUNKS_PER_NODE]]


def _collect_nodes(engine):
    nodes = {}

    def touch(raw_name, source, is_mention):
        norm = normalize_concept(raw_name)
        if not norm:
            return
        rec = nodes.get(norm)
        if rec is None:
            rec = {"variants": set(), "sources": set(), "mentions": 0}
            nodes[norm] = rec
        rec["variants"].add(raw_name)
        if source:
            rec["sources"].add(source)
        if is_mention:
            rec["mentions"] += 1

    for c in engine.concepts:
        touch(c["name"], c.get("source"), True)
    for e in engine.edges:
        touch(e["from"], e.get("source"), False)
        touch(e["to"], e.get("source"), False)
    return nodes


def merge_graph(engine, client, log=logger.info):
    nodes = _collect_nodes(engine)
    if not nodes:
        log("Fusion: no concepts")
        return {}, []

    unique_names = sorted(nodes.keys())
    log(f"Fusion step 1: {len(engine.concepts)} mentions -> {len(unique_names)} unique")

    dsu = _DSU(unique_names)
    llm_candidates = []
    auto_merged = blocked = 0
    if len(unique_names) >= 2:
        name_embeddings = engine.model.encode(unique_names)
        sim_matrix = cosine_similarity(name_embeddings)
        for i in range(len(unique_names)):
            for j in range(i + 1, len(unique_names)):
                sim = float(sim_matrix[i, j])
                if sim < LLM_LOW_SIM:
                    continue
                a, b = unique_names[i], unique_names[j]
                if _is_negation_variant(a, b):
                    blocked += 1
                    continue
                if sim >= AUTO_MERGE_SIM:
                    dsu.union(a, b)
                    auto_merged += 1
                else:
                    llm_candidates.append((a, b, sim))
    llm_candidates = [(a, b, s) for (a, b, s) in llm_candidates if dsu.find(a) != dsu.find(b)]
    log(f"Fusion step 2: {auto_merged} auto-merged, {blocked} blocked, {len(llm_candidates)} for LLM")

    interim_cmap = _build_canonical_map(dsu, nodes)

    adjacency = {}
    for e in engine.edges:
        f = interim_cmap.get(normalize_concept(e["from"]), normalize_concept(e["from"]))
        t = interim_cmap.get(normalize_concept(e["to"]), normalize_concept(e["to"]))
        if not f or not t or f == t:
            continue
        adjacency.setdefault(f, []).append((t, e["relation"]))
        adjacency.setdefault(t, []).append((f, e["relation"]))

    canonical_votes = {}
    step3_new_edges = []
    if llm_candidates:
        ask_names = sorted({n for (a, b, _s) in llm_candidates for n in (a, b)})
        name_vecs = {nm: np.asarray(vec) for nm, vec in zip(ask_names, engine.model.encode(ask_names))}
        
        merges, step3_new_edges, canonical_votes = _llm_evaluate_pairs(
            llm_candidates, interim_cmap, adjacency, nodes, name_vecs, engine, client, log)
        for (a, b) in merges:
            dsu.union(a, b)

    split_out = _verify_clusters(dsu, nodes, engine, client, log)

    final_cmap = _build_canonical_map(dsu, nodes, canonical_votes, split_out)

    final_nodes = {}
    for norm, rec in nodes.items():
        canon = final_cmap.get(norm, norm)
        fn = final_nodes.get(canon)
        if fn is None:
            fn = {"sources": set(), "mentions": 0}
            final_nodes[canon] = fn
        fn["sources"] |= rec["sources"]
        fn["mentions"] += rec["mentions"]

    def _final_canon(name):
        norm = normalize_concept(name)
        return final_cmap.get(norm, norm)

    edge_groups = {}
    skipped = 0

    def _add_edge(frm, to, relation, discovered_by, doc=None, summary="", context=""):
        nonlocal skipped
        relation = _clean_relation(relation)
        f, t = _final_canon(frm), _final_canon(to)
        if not f or not t or f == t:
            skipped += 1
            return
        if f not in final_nodes or t not in final_nodes:
            skipped += 1
            return
        key = (f, t, relation)
        rec = edge_groups.get(key)
        if rec is None:
            rec = {"from": f, "to": t, "relation": relation, "sources": set(),
                   "evidence": 0, "discovered_by": discovered_by,
                   "summary": summary or "", "context": context or ""}
            edge_groups[key] = rec
        else:
            if not rec.get("summary") and summary:
                rec["summary"] = summary
            if not rec.get("context") and context:
                rec["context"] = context
        rec["evidence"] += 1
        if doc:
            rec["sources"].add(doc)
        if discovered_by == "extraction":
            rec["discovered_by"] = "extraction"

    for e in engine.edges:
        _add_edge(e["from"], e["to"], e["relation"], "extraction",
                  doc=e.get("source"), summary=e.get("summary", ""),
                  context=e.get("context", ""))
    for e in step3_new_edges:
        _add_edge(e["from"], e["to"], e["relation"], "llm_eval",
                  summary=e.get("summary", ""), context=e.get("context", ""))

    input_edges = len(engine.edges) + len(step3_new_edges)
    exact_count = len(edge_groups)

    pair_best = {}
    for rec in edge_groups.values():
        pkey = (rec["from"], rec["to"])
        cur = pair_best.get(pkey)
        if cur is None or _edge_rank(rec) > _edge_rank(cur):
            pair_best[pkey] = rec
    pair_count = len(pair_best)

    edge_list = _reciprocal_dedup(list(pair_best.values()))

    for rec in edge_list:
        if not rec["sources"]:
            rec["sources"] = set(final_nodes[rec["from"]]["sources"]) | \
                             set(final_nodes[rec["to"]]["sources"])

    log(f"Fusion step 4: {input_edges} -> {len(edge_list)} edges "
        f"(exact -{(input_edges - skipped) - exact_count}, "
        f"pair -{exact_count - pair_count}, reciprocal -{pair_count - len(edge_list)})")

    for fe in _bridge_fusion(final_nodes, edge_list, engine, client, log):
        key = (fe["from"], fe["to"], fe["relation"])
        if key in edge_groups:
            continue
        src = set(final_nodes[fe["from"]]["sources"]) | set(final_nodes[fe["to"]]["sources"])
        rec = {"from": fe["from"], "to": fe["to"], "relation": fe["relation"],
               "sources": src, "evidence": 1, "discovered_by": "bridge_fusion",
               "summary": fe.get("summary", ""), "context": fe.get("context", "")}
        edge_groups[key] = rec
        edge_list.append(rec)

    canonical_nodes = {}
    for norm, rec in nodes.items():
        canon = final_cmap.get(norm, norm)
        for variant in rec["variants"]:
            if variant != canon:
                canonical_nodes[variant] = canon

    fused_edges = []
    for rec in sorted(edge_list, key=lambda r: (r["from"], r["to"], r["relation"])):
        edge = {
            "from": rec["from"],
            "to": rec["to"],
            "relation": rec["relation"],
            "summary": rec.get("summary", ""),
            "context": rec.get("context", ""),
            "strength": "medium",
            "source": sorted(rec["sources"])[0] if rec["sources"] else "unknown",
            "discovered_by": rec["discovered_by"],
            "conflict_resolved": rec["evidence"] > 1,
        }
        if rec["evidence"] > 1:
            edge["original_count"] = rec["evidence"]
        fused_edges.append(edge)

    by_discovery = {}
    for rec in fused_edges:
        by_discovery[rec["discovered_by"]] = by_discovery.get(rec["discovered_by"], 0) + 1
    log(f"Fusion done: {len(final_nodes)} nodes, {len(fused_edges)} edges {by_discovery}")
    return canonical_nodes, fused_edges


def _build_canonical_map(dsu, nodes, llm_votes=None, split_out=None):
    votes = llm_votes or {}
    split = split_out or set()
    cmap = {}
    for members in dsu.groups().values():
        active_split = split & set(members)
        kept = [m for m in members if m not in active_split]
        if not kept:
            active_split, kept = set(), members
        best = max(kept, key=lambda m: (votes.get(m, 0), len(m), nodes[m]["mentions"], m))
        for m in members:
            cmap[m] = m if m in active_split else best
    return cmap


def _edge_rank(rec):
    return (rec["evidence"],
            1 if rec["discovered_by"] == "extraction" else 0,
            -len(rec["relation"]),
            rec["relation"])


def _reciprocal_dedup(records):
    by_ordered = {(r["from"], r["to"]): r for r in records}
    kept, seen = [], set()
    for rec in records:
        key = (rec["from"], rec["to"])
        if key in seen:
            continue
        rev = (rec["to"], rec["from"])
        other = by_ordered.get(rev)
        if (other is not None and rev not in seen
                and _clean_relation(other["relation"]).lower() == _clean_relation(rec["relation"]).lower()):
            winner = min((rec, other), key=lambda r: (
                -r["evidence"], 0 if r["discovered_by"] == "extraction" else 1,
                r["from"], r["to"]))
            kept.append(winner)
            seen.add(key)
            seen.add(rev)
        else:
            kept.append(rec)
            seen.add(key)
    return kept


def _neighbours(name, interim_cmap, adjacency, k):
    canon = interim_cmap.get(name, name)
    out, seen = [], set()
    for nb, rel in adjacency.get(canon, []):
        key = (nb, rel)
        if nb == canon or key in seen:
            continue
        seen.add(key)
        out.append(f"{rel} -> {nb}")
        if len(out) >= k:
            break
    return out or ["(no neighbours)"]


_STEP3_SYSTEM = (
    "Resolve whether two concept names are SAME, DIFFERENT BUT RELATED, or UNRELATED. "
    "Judge PRIMARILY from source text evidence; use neighbourhood as secondary context. "
    "Never judge from name similarity alone. Answer 'related' ONLY for meaningful academic relationships.\n"
    "RELATION LABEL RULES (when 'related'):\n"
    "- 1-3 words, directed verb phrase in ACTIVE voice (e.g. 'is a type of', 'generalises'). "
    "NOT passive voice.\n"
    "- Must NOT contain either name, NOT a full sentence, NOT vague ('is related to', 'similar to').\n"
    "- If HIERARCHICAL (is a/type of/generalises/includes/underlies), return 'broader': 'A'|'B' "
    "naming the more general concept.\n"
    'Return: {"id": int, "verdict": "same"|"related"|"unrelated", '
    '"canonical": "A"|"B" (if same), "relation": phrase (if related), '
    '"direction": "A_to_B"|"B_to_A" (if related), "broader": "A"|"B" (if related + hierarchical)}. '
    'Only JSON: {"results": [...]}'
)


def _llm_evaluate_pairs(candidates, interim_cmap, adjacency, nodes, name_vecs, engine, client, log):
    cache = _load_verdict_cache()
    resolved, to_ask = {}, []
    for (a, b, sim) in candidates:
        rec = cache.get(_pair_key(_STEP3_PROMPT_VERSION, a, b))
        if isinstance(rec, dict):
            resolved[(a, b)] = _from_cache_record(a, b, rec)
        else:
            to_ask.append((a, b, sim))
    log(f"Fusion step 3: {len(resolved)} cache hits, {len(to_ask)} LLM calls")

    cache_dirty = False
    for start in range(0, len(to_ask), _VERDICT_PAIR_BATCH):
        sub = to_ask[start:start + _VERDICT_PAIR_BATCH]
        payload = []
        for idx, (a, b, sim) in enumerate(sub):
            item = {"id": idx, "a": a, "b": b, "similarity": round(sim, 3),
                    "a_sources": sorted(nodes[a]["sources"]),
                    "b_sources": sorted(nodes[b]["sources"]),
                    "a_neighbours": _neighbours(a, interim_cmap, adjacency, _NEIGHBOURS_PER_NODE),
                    "b_neighbours": _neighbours(b, interim_cmap, adjacency, _NEIGHBOURS_PER_NODE)}
            a_ev = evidence.snippets(nodes[a]["sources"], b) if 'evidence' in locals() else []
            b_ev = evidence.snippets(nodes[b]["sources"], a) if 'evidence' in locals() else []
            if a_ev:
                item["a_evidence"] = a_ev
            if b_ev:
                item["b_evidence"] = b_ev
            payload.append(item)

        evidence = _EvidencePool(engine, log, name_vecs)
        payload = []
        for idx, (a, b, sim) in enumerate(sub):
            item = {"id": idx, "a": a, "b": b, "similarity": round(sim, 3),
                    "a_sources": sorted(nodes[a]["sources"]),
                    "b_sources": sorted(nodes[b]["sources"]),
                    "a_neighbours": _neighbours(a, interim_cmap, adjacency, _NEIGHBOURS_PER_NODE),
                    "b_neighbours": _neighbours(b, interim_cmap, adjacency, _NEIGHBOURS_PER_NODE)}
            a_ev = evidence.snippets(nodes[a]["sources"], b)
            b_ev = evidence.snippets(nodes[b]["sources"], a)
            if a_ev:
                item["a_evidence"] = a_ev
            if b_ev:
                item["b_evidence"] = b_ev
            payload.append(item)

        by_id = _results_by_id(_verdict_call(client, _STEP3_SYSTEM, payload, log, "Fusion step 3:"))
        for idx, (a, b, sim) in enumerate(sub):
            r = by_id.get(idx)
            if r is None:
                resolved[(a, b)] = {"verdict": "unrelated"}
                continue
            rec = _to_cache_record(a, b, r)
            cache[_pair_key(_STEP3_PROMPT_VERSION, a, b)] = rec
            cache_dirty = True
            resolved[(a, b)] = _from_cache_record(a, b, rec)
    if cache_dirty:
        _save_verdict_cache(cache)

    merges, new_edges, canonical_votes = [], [], {}
    n_same = n_related = n_unrelated = 0
    for (a, b, sim) in candidates:
        r = resolved.get((a, b), {})
        verdict = r.get("verdict", "unrelated")
        if verdict == "same":
            merges.append((a, b))
            canonical = str(r.get("canonical", "")).strip().upper()
            if canonical in ("A", "B"):
                chosen = a if canonical == "A" else b
                canonical_votes[chosen] = canonical_votes.get(chosen, 0) + 1
            n_same += 1
        elif verdict == "related":
            frm, to, relation = _orient_relation(
                a, b, r.get("relation") or "", r.get("direction") or "A_to_B",
                r.get("broader") or "")
            rl = relation.lower()
            if not relation or _is_vague_relation(relation) or a in rl or b in rl:
                n_unrelated += 1
                continue
            new_edges.append({"from": frm, "to": to, "relation": relation})
            n_related += 1
        else:
            n_unrelated += 1

    log(f"Fusion step 3: {len(candidates)} -> same {n_same} / related {n_related} / unrelated {n_unrelated}")
    return merges, new_edges, canonical_votes


_STEP3B_SYSTEM = (
    "Several concept NAMES merged into ONE node. Using source text, decide which "
    "names truly refer to the SAME concept and which are DIFFERENT (pulled in by mistake). "
    "Return names that do NOT belong in 'split_out'; if all are the same, return [].\n"
    'Return: {"id": int, "split_out": [name, ...]}. '
    'Only JSON: {"results": [...]}'
)


def _verify_clusters(dsu, nodes, engine, client, log):
    clusters = [sorted(m) for m in dsu.groups().values() if len(m) >= 3]
    log(f"Fusion step 3b: {len(clusters)} cluster(s) to re-verify")
    if not clusters:
        return set()

    def _ckey(members):
        return _pair_key(_CLUSTER_PROMPT_VERSION, "||".join(members), "")

    cache = _load_verdict_cache()
    resolved, to_ask = {}, []
    for ci, members in enumerate(clusters):
        rec = cache.get(_ckey(members))
        if isinstance(rec, dict):
            resolved[ci] = rec
        else:
            to_ask.append((ci, members))

    if to_ask:
        cluster_names = sorted({m for (_ci, members) in to_ask for m in members})
        name_vecs = {nm: np.asarray(vec) for nm, vec in zip(cluster_names, engine.model.encode(cluster_names))}
        evidence = _EvidencePool(engine, log, name_vecs)

        payload = []
        for (ci, members) in to_ask:
            entry = {"id": ci, "names": []}
            for m in members:
                name_item = {"name": m}
                snippets = evidence.snippets(nodes[m]["sources"], m)
                if snippets:
                    name_item["evidence"] = snippets
                entry["names"].append(name_item)
            payload.append(entry)

        by_id = _results_by_id(_verdict_call(client, _STEP3B_SYSTEM, payload, log, "Fusion step 3b:"))
        for (ci, members) in to_ask:
            r = by_id.get(ci) or {}
            rec = {"split_out": [m for m in (r.get("split_out") or []) if m in members]}
            cache[_ckey(members)] = rec
            resolved[ci] = rec
        _save_verdict_cache(cache)

    split_out = set()
    for ci, members in enumerate(clusters):
        so = [m for m in ((resolved.get(ci) or {}).get("split_out") or []) if m in members]
        if len(so) >= len(members):
            so = so[:len(members) - 1]
        split_out.update(so)
    log(f"Fusion step 3b: split out {len(split_out)} name(s)")
    return split_out


_STEP5_SYSTEM = (
    "Two concepts from different documents share neighbours. Using source text, decide if "
    "a meaningful academic relationship connects them. Label must be grounded in text — "
    "if text doesn't support a specific relationship, answer 'unrelated'. Shared graph "
    "structure alone is NOT sufficient.\n"
    "RELATION LABEL RULES: 1-3 words, ACTIVE voice verb phrase; NOT passive; "
    "NOT containing either name; NOT vague ('related to', 'similar to'). If HIERARCHICAL, "
    'return "broader": "A"|"B".\n'
    'Return: {"id": int, "verdict": "related"|"unrelated", '
    '"relation": phrase (if related), "direction": "A_to_B"|"B_to_A" (if related), '
    '"broader": "A"|"B" (if hierarchical)}. '
    'Only JSON: {"results": [...]}'
)


def _bridge_fusion(final_nodes, edge_list, engine, client, log):
    neigh = {n: set() for n in final_nodes}
    for rec in edge_list:
        neigh.setdefault(rec["from"], set()).add(rec["to"])
        neigh.setdefault(rec["to"], set()).add(rec["from"])

    degrees = {n: len(s) for n, s in neigh.items()}
    deg_values = np.array(list(degrees.values()) or [0.0], dtype="float64")
    p90 = float(np.percentile(deg_values, _HUB_DEGREE_PERCENTILE))
    hubs = {n for n, d in degrees.items() if d > p90 and d >= _HUB_MIN_ABSOLUTE_DEGREE}

    connected = set()
    for rec in edge_list:
        connected.add((rec["from"], rec["to"]))
        connected.add((rec["to"], rec["from"]))

    shared = {}
    for node, nbset in neigh.items():
        if node in hubs:
            continue
        for a, b in combinations(sorted(nbset), 2):
            shared[(a, b)] = shared.get((a, b), 0) + 1

    candidates = []
    for (a, b), cnt in shared.items():
        if cnt < _BRIDGE_MIN_SHARED_NEIGHBOURS or (a, b) in connected:
            continue
        if set(final_nodes[a]["sources"]) == set(final_nodes[b]["sources"]):
            continue
        common = (neigh[a] & neigh[b]) - hubs
        if len(common) < _BRIDGE_MIN_SHARED_NEIGHBOURS:
            continue
        candidates.append((a, b, sorted(common)))

    log(f"Fusion step 5: {len(hubs)} hub(s), {len(candidates)} candidate(s)")
    if not candidates:
        return []

    cache = _load_verdict_cache()
    resolved, to_ask = {}, []
    for (a, b, common) in candidates:
        rec = cache.get(_pair_key(_STEP5_PROMPT_VERSION, a, b))
        if isinstance(rec, dict):
            resolved[(a, b)] = _from_cache_record(a, b, rec)
        else:
            to_ask.append((a, b, common))
    log(f"Fusion step 5: {len(resolved)} cache hits, {len(to_ask)} LLM calls")

    ask_names = sorted({n for (a, b, _c) in to_ask for n in (a, b)})
    name_vecs = {nm: np.asarray(vec) for nm, vec in zip(ask_names, engine.model.encode(ask_names))} if ask_names else {}
    evidence = _EvidencePool(engine, log, name_vecs)

    cache_dirty = False
    for start in range(0, len(to_ask), _VERDICT_PAIR_BATCH):
        sub = to_ask[start:start + _VERDICT_PAIR_BATCH]
        payload = []
        for idx, (a, b, common) in enumerate(sub):
            item = {"id": idx, "a": a, "b": b,
                    "a_sources": sorted(final_nodes[a]["sources"]),
                    "b_sources": sorted(final_nodes[b]["sources"]),
                    "shared_neighbours": common}
            a_ev = evidence.snippets(final_nodes[a]["sources"], b)
            b_ev = evidence.snippets(final_nodes[b]["sources"], a)
            if a_ev:
                item["a_evidence"] = a_ev
            if b_ev:
                item["b_evidence"] = b_ev
            payload.append(item)

        by_id = _results_by_id(_verdict_call(client, _STEP5_SYSTEM, payload, log, "Fusion step 5:"))
        for idx, (a, b, common) in enumerate(sub):
            r = by_id.get(idx)
            if r is None:
                resolved[(a, b)] = {"verdict": "unrelated"}
                continue
            rec = _to_cache_record(a, b, r)
            cache[_pair_key(_STEP5_PROMPT_VERSION, a, b)] = rec
            cache_dirty = True
            resolved[(a, b)] = _from_cache_record(a, b, rec)
    if cache_dirty:
        _save_verdict_cache(cache)

    fusion_edges = []
    for (a, b, common) in candidates:
        r = resolved.get((a, b), {})
        if r.get("verdict") != "related":
            continue
        frm, to, relation = _orient_relation(
            a, b, r.get("relation") or "", r.get("direction") or "A_to_B",
            r.get("broader") or "")
        rl = relation.lower()
        if relation and not _is_vague_relation(relation) and a not in rl and b not in rl:
            fusion_edges.append({"from": frm, "to": to, "relation": relation})
    log(f"Fusion step 5: {len(candidates)} -> {len(fusion_edges)} fusion edge(s)")
    return fusion_edges