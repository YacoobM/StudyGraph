"""Known-answer tests for the concept extraction evaluation (eval/concept_eval.py)."""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from eval import concept_eval as ce  # noqa: E402

EXAMPLES = BACKEND / "eval" / "examples"


def doc(concepts, relationships=None, name="a.pdf"):
    return {"document": name, "concepts": concepts, "relationships": relationships or []}


def score(reference, extracted, fuzzy=None):
    return ce.evaluate_document(doc(reference), extracted, [], fuzzy)


def test_normalize_matches_graph_fusion():
    fusion = pytest.importorskip("services.graph_fusion")
    for name in ["Entity-Relationship  Model", "Big O; notation", "  `SQL` JOINs ", "", "a:b,c"]:
        assert ce.normalize(name) == fusion.normalize_concept(name)


def test_plural_folding_rules():
    assert ce.fold_plural("primary keys") == "primary key"
    assert ce.fold_plural("functional dependencies") == "functional dependency"
    assert ce.fold_plural("classes") == "class"
    assert ce.fold_plural("analysis") == "analysis"
    assert ce.fold_plural("bus") == "bus"
    assert ce.fold_plural("process") == "process"


def test_scores_never_invent_values_on_empty_lists():
    assert ce.scores(0, 0, 0) == {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None, "f1": None}
    nothing_extracted = ce.scores(0, 0, 4)
    assert nothing_extracted["precision"] is None
    assert nothing_extracted["recall"] == 0.0
    assert nothing_extracted["f1"] is None


def test_scores_all_wrong_gives_zero_f1():
    assert ce.scores(0, 3, 2)["f1"] == 0.0


def test_known_counts():
    result = score(["A", "B", "C", "D"], ["a", "b", "x"])
    assert (result["tp"], result["fp"], result["fn"]) == (2, 1, 2)
    assert result["precision"] == pytest.approx(2 / 3)
    assert result["recall"] == pytest.approx(0.5)
    assert result["f1"] == pytest.approx(4 / 7)
    assert result["false_negatives"] == ["C", "D"]
    assert result["false_positives"] == ["x"]


def test_matching_is_one_to_one():
    result = score(["Primary key"], ["Primary key", "primary keys"])
    assert (result["tp"], result["fp"], result["fn"]) == (1, 1, 0)
    assert result["matches"] == [{"reference": "Primary key", "extracted": "Primary key", "match": "exact"}]


def test_exact_match_wins_over_plural():
    result = score(["node", "nodes"], ["nodes"])
    assert result["matches"][0] == {"reference": "nodes", "extracted": "nodes", "match": "exact"}
    assert result["false_negatives"] == ["node"]


def test_duplicate_extracted_names_count_once():
    result = score(["Foreign key"], ["Foreign key", "foreign-key", "FOREIGN KEY"])
    assert (result["tp"], result["fp"], result["fn"]) == (1, 0, 0)
    assert result["duplicates_ignored"]["extracted"] == ["foreign-key", "FOREIGN KEY"]


def test_synonyms_match():
    result = score([{"name": "Third normal form", "synonyms": ["3NF"]}], ["3nf"])
    assert result["matches"] == [{"reference": "Third normal form", "extracted": "3nf", "match": "synonym"}]


def test_fuzzy_is_off_unless_asked():
    off = score(["Normalisation"], ["Normalization"])
    assert (off["tp"], off["fp"], off["fn"]) == (0, 1, 1)
    on = score(["Normalisation"], ["Normalization"], fuzzy=0.9)
    assert on["matches"][0]["match"] == "fuzzy"
    assert on["tp"] == 1


def test_snapshot_is_filtered_by_document():
    snapshot = {
        "concepts": [{"id": "1", "name": "Index"}, {"id": "2", "name": "B tree"}, {"id": "3", "name": "Hashing"}],
        "relationships": [{"source_concept_id": "1", "target_concept_id": "2"}],
        "concept_provenance": [
            {"concept_id": "1", "document_filename": "week5.pdf"},
            {"concept_id": "2", "document_filename": "Week5.PDF"},
            {"concept_id": "3", "document_filename": "week6.pdf"},
        ],
    }
    names, pairs = ce.extracted_for(snapshot, "week5.pdf")
    assert names == ["Index", "B tree"]
    assert pairs == [("index", "b tree")]
    assert ce.extracted_for(snapshot, "missing.pdf") == (None, [])


def test_unknown_document_stops_the_run(tmp_path):
    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps({"document": "missing.pdf", "concepts": ["A"]}))
    with pytest.raises(SystemExit):
        ce.run(ref, {"documents": [{"document": "other.pdf", "concepts": ["A"]}]}, "test")


def test_summary_micro_and_macro():
    first = score(["A", "B"], ["a", "b"])        # P 1.0, R 1.0
    second = score(["C", "D"], ["c", "x", "y"])  # P 1/3, R 0.5
    summary = ce.summarise([first, second])
    assert summary["micro"]["tp"] == 3 and summary["micro"]["fp"] == 2 and summary["micro"]["fn"] == 1
    assert summary["micro"]["precision"] == pytest.approx(3 / 5)
    assert summary["macro"]["precision"] == pytest.approx((1 + 1 / 3) / 2)
    assert summary["macro"]["recall"] == pytest.approx(0.75)


def test_example_files_end_to_end():
    data = json.loads((EXAMPLES / "concepts_extracted.example.json").read_text())
    results, summary, _ = ce.run(EXAMPLES / "concepts_reference.example.json", data, "example")
    r = results[0]
    assert (r["tp"], r["fp"], r["fn"]) == (5, 2, 3)
    assert r["precision"] == pytest.approx(5 / 7)
    assert r["recall"] == pytest.approx(5 / 8)
    assert r["f1"] == pytest.approx(2 / 3)
    kinds = {m["extracted"]: m["match"] for m in r["matches"]}
    assert kinds == {
        "normalisation": "exact",
        "Second Normal Form": "exact",
        "Functional Dependencies": "plural",
        "Primary Keys": "plural",
        "1NF": "synonym",
    }
    rel = r["relationships"]
    assert (rel["tp"], rel["fp"], rel["fn"]) == (1, 1, 2)


def test_cli_writes_reports(tmp_path, capsys):
    code = ce.main([
        "--reference", str(EXAMPLES / "concepts_reference.example.json"),
        "--extracted", str(EXAMPLES / "concepts_extracted.example.json"),
        "--out", str(tmp_path),
    ])
    assert code == 0
    files = sorted(p.suffix for p in tmp_path.iterdir())
    assert files == [".csv", ".json", ".md"]
    report = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert report["summary"]["micro"]["tp"] == 5
    assert "| week3-normalisation.pdf | 8 | 7 | 5 | 2 | 3 | 0.714 | 0.625 | 0.667 |" in capsys.readouterr().out
