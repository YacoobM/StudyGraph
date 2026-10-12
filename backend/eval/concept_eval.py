"""Score StudyGraph concept extraction against a hand-checked reference list.

Precision = TP / (TP + FP), recall = TP / (TP + FN), F1 = 2PR / (P + R).
Matching is one to one: a reference concept can be matched by at most one
extracted concept, and the other way round.

Run from the backend folder:
    python -m eval.concept_eval --reference eval/examples/concepts_reference.example.json \
        --extracted eval/examples/concepts_extracted.example.json
See eval/README.md for the full workflow.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# Same rules as services.graph_fusion.normalize_concept. Copied so this tool
# runs without the ML dependencies. test_concept_eval checks they agree.
_STRIP_CHARS = re.compile(r"[,;:\"'`''""]")
_WS = re.compile(r"\s+")
_KEEP_S = ("ss", "us", "is", "as", "os")


def normalize(name) -> str:
    if not name:
        return ""
    text = str(name).lower()
    text = text.replace("-", " ")
    text = _STRIP_CHARS.sub("", text)
    return _WS.sub(" ", text).strip()


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(_KEEP_S):
        return word[:-1]
    return word


def fold_plural(key: str) -> str:
    """Singular form of the last word, so "foreign keys" matches "foreign key"."""
    words = key.split(" ")
    if not words or not words[-1]:
        return key
    words[-1] = _singular(words[-1])
    return " ".join(words)


def safe_ratio(top: int, bottom: int):
    """None when the ratio is undefined, so an empty list never becomes a fake score."""
    return None if bottom == 0 else top / bottom


def scores(tp: int, fp: int, fn: int) -> dict:
    precision = safe_ratio(tp, tp + fp)
    recall = safe_ratio(tp, tp + fn)
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


# ---------- reading inputs ----------

def _name_of(item):
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("name") or item.get("concept") or item.get("normalized_name") or ""
    return ""


def _reference_concepts(raw: list) -> tuple[list[dict], list[str]]:
    """Reference rows as {name, keys}, plus names dropped as duplicates."""
    rows, seen, dupes = [], set(), []
    for item in raw:
        name = _name_of(item).strip()
        key = normalize(name)
        if not key:
            continue
        if key in seen:
            dupes.append(name)
            continue
        seen.add(key)
        synonyms = item.get("synonyms", []) if isinstance(item, dict) else []
        alias_keys = [normalize(s) for s in synonyms if normalize(s)]
        rows.append({"name": name, "key": key, "synonyms": alias_keys})
    return rows, dupes


def _extracted_concepts(names: list[str]) -> tuple[list[dict], list[str]]:
    rows, seen, dupes = [], set(), []
    for name in names:
        name = str(name).strip()
        key = normalize(name)
        if not key:
            continue
        if key in seen:
            dupes.append(name)
            continue
        seen.add(key)
        rows.append({"name": name, "key": key})
    return rows, dupes


def _relationships(raw: list) -> list[tuple[str, str]]:
    pairs = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        a = normalize(row.get("from") or row.get("source") or "")
        b = normalize(row.get("to") or row.get("target") or "")
        if a and b and a != b:
            pairs.append((a, b))
    return pairs


def load_reference(path: Path) -> list[dict]:
    """Reference file: one document {"document", "concepts"} or {"documents": [...]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    docs = data.get("documents") if isinstance(data, dict) and "documents" in data else [data]
    out = []
    for doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get("concepts"), list):
            raise ValueError(f"{path}: every document needs a concepts list.")
        out.append({
            "document": str(doc.get("document") or "").strip(),
            "concepts": doc["concepts"],
            "relationships": doc.get("relationships") or [],
        })
    return out


def _snapshot_names(snapshot: dict, document: str) -> list[str] | None:
    """Concept names from a graph version. With a document name, only concepts
    whose provenance points at that file. None when the file is not in the graph."""
    concepts = snapshot.get("concepts") or []
    if not document:
        return [_name_of(c) for c in concepts]
    provenance = snapshot.get("concept_provenance") or []
    wanted = document.lower()
    ids = {str(p.get("concept_id")) for p in provenance
           if str(p.get("document_filename") or "").lower() == wanted}
    if not ids:
        return None
    return [_name_of(c) for c in concepts if str(c.get("id")) in ids]


def _snapshot_relationships(snapshot: dict, names: list[str]) -> list[tuple[str, str]]:
    keep = {normalize(n) for n in names}
    by_id = {str(c.get("id")): normalize(_name_of(c)) for c in snapshot.get("concepts") or []}
    pairs = []
    for row in snapshot.get("relationships") or []:
        a = by_id.get(str(row.get("source_concept_id")))
        b = by_id.get(str(row.get("target_concept_id")))
        if a and b and a != b and a in keep and b in keep:
            pairs.append((a, b))
    return pairs


def extracted_for(data, document: str) -> tuple[list[str] | None, list[tuple[str, str]]]:
    """Accepts a list of names, {"concepts": [...]}, {"documents": [...]} or a graph
    version export with concepts, relationships and concept_provenance."""
    if isinstance(data, list):
        return [_name_of(x) for x in data], []
    if not isinstance(data, dict):
        raise ValueError("Extracted concepts must be a JSON list or object.")
    if "documents" in data:
        for doc in data["documents"]:
            if not document or str(doc.get("document") or "").lower() == document.lower():
                return [_name_of(x) for x in doc.get("concepts") or []], _relationships(doc.get("relationships"))
        return None, []
    if "snapshot" in data:
        data = {**data["snapshot"], **{k: v for k, v in data.items() if k != "snapshot"}}
    if data.get("concept_provenance") or (data.get("concepts") and isinstance(data["concepts"][0], dict) and "id" in data["concepts"][0]):
        names = _snapshot_names(data, document)
        return names, (_snapshot_relationships(data, names) if names else [])
    return [_name_of(x) for x in data.get("concepts") or []], _relationships(data.get("relationships"))


def load_from_supabase(graph_id: str, version_id: str | None) -> dict:
    """Read a sealed graph version with the user's own token (row level security applies)."""
    token = os.environ.get("STUDYGRAPH_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set STUDYGRAPH_ACCESS_TOKEN to a signed-in user's access token.")
    from repositories.supabase_graphs import list_graph_versions, load_graph_version
    from repositories.users import SupabaseIdentity

    identity = SupabaseIdentity(auth_user_id="", email="", access_token=token)
    if not version_id:
        versions = list_graph_versions(identity, graph_id)
        if not versions:
            raise SystemExit(f"Graph {graph_id} has no sealed versions.")
        version_id = str(versions[0]["id"])
    loaded = load_graph_version(identity, graph_id, version_id)
    return {
        "graph_id": graph_id,
        "version_id": version_id,
        "concepts": loaded["concepts"],
        "relationships": loaded["relationships"],
        "concept_provenance": loaded["concept_provenance"],
    }


# ---------- matching ----------

def match(reference: list[dict], extracted: list[dict], fuzzy: float | None = None) -> tuple[list[dict], list[str], list[str]]:
    """One to one matching in passes: exact, plural, synonym, then optional fuzzy.
    Earlier passes win, and within a pass the input order decides."""
    free_ref = list(range(len(reference)))
    free_ext = list(range(len(extracted)))
    pairs = []

    def run(kind, test):
        for e in list(free_ext):
            for r in free_ref:
                if test(reference[r], extracted[e]):
                    pairs.append({"reference": reference[r]["name"], "extracted": extracted[e]["name"], "match": kind})
                    free_ref.remove(r)
                    free_ext.remove(e)
                    break

    run("exact", lambda r, e: r["key"] == e["key"])
    run("plural", lambda r, e: fold_plural(r["key"]) == fold_plural(e["key"]))
    run("synonym", lambda r, e: fold_plural(e["key"]) in {fold_plural(s) for s in r["synonyms"]})
    if fuzzy:
        run("fuzzy", lambda r, e: SequenceMatcher(None, r["key"], e["key"]).ratio() >= fuzzy)

    return pairs, [reference[i]["name"] for i in free_ref], [extracted[i]["name"] for i in free_ext]


def evaluate_document(ref_doc: dict, names: list[str], ext_pairs, fuzzy=None) -> dict:
    reference, ref_dupes = _reference_concepts(ref_doc["concepts"])
    extracted, ext_dupes = _extracted_concepts(names)
    pairs, missed, extra = match(reference, extracted, fuzzy)
    result = {
        "document": ref_doc["document"] or "(all)",
        "reference_count": len(reference),
        "extracted_count": len(extracted),
        **scores(len(pairs), len(extra), len(missed)),
        "matches": pairs,
        "false_negatives": missed,
        "false_positives": extra,
        "duplicates_ignored": {"reference": ref_dupes, "extracted": ext_dupes},
    }
    ref_pairs = _relationships(ref_doc.get("relationships"))
    if ref_pairs:
        result["relationships"] = evaluate_relationships(ref_pairs, ext_pairs, pairs)
    return result


def evaluate_relationships(ref_pairs, ext_pairs, concept_pairs) -> dict:
    """Relationships compared as unordered concept pairs. Extracted endpoints are
    renamed to the reference concept they matched. The relation type is ignored."""
    to_ref = {normalize(p["extracted"]): normalize(p["reference"]) for p in concept_pairs}
    ref_set = {frozenset(p) for p in ref_pairs}
    ext_set = {frozenset((to_ref.get(a, a), to_ref.get(b, b))) for a, b in ext_pairs}
    ext_set = {p for p in ext_set if len(p) == 2}
    tp = len(ref_set & ext_set)
    out = scores(tp, len(ext_set) - tp, len(ref_set) - tp)
    out["missing"] = sorted(" <> ".join(sorted(p)) for p in ref_set - ext_set)
    return out


def summarise(results: list[dict]) -> dict:
    """Micro average sums the counts. Macro average is the mean of defined per document scores."""
    tp = sum(r["tp"] for r in results)
    fp = sum(r["fp"] for r in results)
    fn = sum(r["fn"] for r in results)
    macro = {}
    for key in ("precision", "recall", "f1"):
        values = [r[key] for r in results if r[key] is not None]
        macro[key] = sum(values) / len(values) if values else None
    return {"documents": len(results), "micro": scores(tp, fp, fn), "macro": macro}


# ---------- output ----------

def fmt(value) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def markdown(results: list[dict], summary: dict, settings: dict) -> str:
    lines = [
        "# Concept extraction evaluation",
        "",
        f"Run at {settings['run_at']}. Extracted from: {settings['source']}. "
        f"Fuzzy matching: {settings['fuzzy'] or 'off'}.",
        "",
        "| Document | Ref | Extracted | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r['document']} | {r['reference_count']} | {r['extracted_count']} | {r['tp']} | {r['fp']} | {r['fn']} "
            f"| {fmt(r['precision'])} | {fmt(r['recall'])} | {fmt(r['f1'])} |"
        )
    m = summary["micro"]
    lines.append(
        f"| **Micro average** | | | {m['tp']} | {m['fp']} | {m['fn']} | {fmt(m['precision'])} | {fmt(m['recall'])} | {fmt(m['f1'])} |"
    )
    a = summary["macro"]
    lines.append(f"| **Macro average** | | | | | | {fmt(a['precision'])} | {fmt(a['recall'])} | {fmt(a['f1'])} |")
    for r in results:
        lines += ["", f"## {r['document']}", ""]
        non_exact = [p for p in r["matches"] if p["match"] != "exact"]
        if non_exact:
            lines.append("Matched by plural, synonym or fuzzy rule:")
            lines += [f"- {p['extracted']} -> {p['reference']} ({p['match']})" for p in non_exact]
            lines.append("")
        lines.append(f"Missed (false negatives): {', '.join(r['false_negatives']) or 'none'}")
        lines.append("")
        lines.append(f"Not in the reference (false positives): {', '.join(r['false_positives']) or 'none'}")
        if "relationships" in r:
            rel = r["relationships"]
            lines += ["", f"Relationships: TP {rel['tp']}, FP {rel['fp']}, FN {rel['fn']}, "
                          f"precision {fmt(rel['precision'])}, recall {fmt(rel['recall'])}, F1 {fmt(rel['f1'])}"]
    return "\n".join(lines) + "\n"


def write_reports(out_dir: Path, results, summary, settings) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = settings["run_at"].replace(":", "").replace("-", "")
    base = out_dir / f"concept_eval_{stamp}"
    paths = [base.with_suffix(".json"), base.with_suffix(".csv"), base.with_suffix(".md")]
    paths[0].write_text(json.dumps({"settings": settings, "summary": summary, "documents": results}, indent=2), encoding="utf-8")
    with paths[1].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["document", "reference", "extracted", "tp", "fp", "fn", "precision", "recall", "f1"])
        for r in results:
            writer.writerow([r["document"], r["reference_count"], r["extracted_count"], r["tp"], r["fp"], r["fn"],
                             fmt(r["precision"]), fmt(r["recall"]), fmt(r["f1"])])
    paths[2].write_text(markdown(results, summary, settings), encoding="utf-8")
    return paths


def run(reference_path, extracted_data, source_label, fuzzy=None) -> tuple[list[dict], dict, dict]:
    results = []
    for ref_doc in load_reference(reference_path):
        names, ext_pairs = extracted_for(extracted_data, ref_doc["document"])
        if names is None:
            raise SystemExit(f"No extracted concepts found for document '{ref_doc['document']}'. "
                             "Check the file name matches the uploaded document.")
        results.append(evaluate_document(ref_doc, names, ext_pairs, fuzzy))
    settings = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "reference": str(reference_path),
        "source": source_label,
        "fuzzy": fuzzy,
    }
    return results, summarise(results), settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reference", required=True, type=Path, help="Hand-checked reference JSON.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--extracted", type=Path, help="JSON with StudyGraph's extracted concepts.")
    source.add_argument("--graph-id", help="Read the concepts from this Supabase graph instead.")
    parser.add_argument("--version-id", help="Graph version to score. Defaults to the latest sealed one.")
    parser.add_argument("--fuzzy", type=float, help="Also match names this similar (0 to 1). Off by default.")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "reports", help="Folder for the reports.")
    parser.add_argument("--no-files", action="store_true", help="Print the table only.")
    args = parser.parse_args(argv)

    if args.fuzzy is not None and not 0 < args.fuzzy <= 1:
        parser.error("--fuzzy must be between 0 and 1.")
    if args.extracted:
        data = json.loads(args.extracted.read_text(encoding="utf-8"))
        label = str(args.extracted)
    else:
        data = load_from_supabase(args.graph_id, args.version_id)
        label = f"graph {data['graph_id']} version {data['version_id']}"

    results, summary, settings = run(args.reference, data, label, args.fuzzy)
    print(markdown(results, summary, settings))
    if not args.no_files:
        for path in write_reports(args.out, results, summary, settings):
            print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
