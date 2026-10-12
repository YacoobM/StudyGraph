"""Regression tests for day assignment in revision-note plans (the DAY null bug)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.note_days import (  # noqa: E402
    assign_days,
    repair_note_days,
    split_across_days,
    split_sections_for_days,
)


def section(n, doc="a.pdf"):
    return {"title": "", "concepts": [f"{doc}-{i}" for i in range(n)], "document": doc}


def test_no_split_gives_no_days():
    assert split_across_days([3, 4], None) == [None, None]
    sections, used = assign_days([section(3)], None)
    assert used is None and sections[0]["day"] is None


def test_days_start_at_one_and_cover_every_day():
    for days in (1, 2, 3, 5):
        out = split_across_days([4] * 6, days)
        assert out[0] == 1
        assert sorted(set(out)) == list(range(1, days + 1))
        assert out == sorted(out)


def test_fewer_sections_than_days_shortens_the_plan():
    sections, used = assign_days([section(2), section(2)], 3)
    assert used == 2
    assert [s["day"] for s in sections] == [1, 2]


def test_single_document_is_split_to_fill_the_days():
    parts = split_sections_for_days([section(12)], 3)
    assert len(parts) == 3
    assert sum(len(p["concepts"]) for p in parts) == 12
    # Concept order is kept across the parts.
    assert [c for p in parts for c in p["concepts"]] == section(12)["concepts"]
    sections, used = assign_days(parts, 3)
    assert used == 3 and [s["day"] for s in sections] == [1, 2, 3]


def test_small_sections_are_not_split():
    parts = split_sections_for_days([section(3)], 3)
    assert len(parts) == 1
    _, used = assign_days(parts, 3)
    assert used == 1


def test_repair_fills_null_days_on_stored_notes():
    note = {"kind": "plan", "days": 3, "sections": [
        {"day": None, "concepts": ["a", "b"]},
        {"day": None, "concepts": ["c"]},
        {"day": None, "concepts": ["d", "e"]},
    ]}
    repair_note_days(note)
    assert [s["day"] for s in note["sections"]] == [1, 2, 3]
    assert note["days"] == 3


def test_repair_caps_days_to_sections():
    note = {"kind": "plan", "days": 3, "sections": [{"day": None, "concepts": ["a"]}]}
    repair_note_days(note)
    assert note["sections"][0]["day"] == 1 and note["days"] == 1


def test_repair_leaves_valid_plans_and_full_notes_alone():
    plan = {"kind": "plan", "days": 2, "sections": [{"day": 1, "concepts": []}, {"day": 2, "concepts": []}]}
    assert repair_note_days(plan)["sections"][1]["day"] == 2
    full = {"kind": "full", "days": None, "sections": [{"day": None, "concepts": ["a"]}]}
    assert repair_note_days(full)["sections"][0]["day"] is None


def test_planner_assigns_days_end_to_end(monkeypatch):
    from repositories import supabase_notes

    async def no_titles(sections, cache_key):
        return None

    monkeypatch.setattr(supabase_notes, "request_titles", no_titles)
    concepts = [{"name": f"C{i}", "source": "lecture.pdf", "page": i} for i in range(9)]
    sections = asyncio.run(supabase_notes._plan_supabase_sections(concepts, "k", 3))
    assigned, used = assign_days(sections, 3)
    assert used == 3
    assert [s["day"] for s in assigned] == [1, 2, 3]
    assert all(s["title"].startswith("lecture (part") for s in assigned)
