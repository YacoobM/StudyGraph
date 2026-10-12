"""Day assignment for revision notes split into a study plan.

Kept free of I/O so it can be tested on its own.
"""
from __future__ import annotations

from typing import Optional

# A section is only split when each part keeps at least this many concepts.
MIN_CONCEPTS_PER_PART = 2


def split_across_days(sizes: list[int], days: Optional[int]) -> list[Optional[int]]:
    """Day number per section (None each without a split), balanced on concept counts with
    sections kept whole; every day gets at least one section."""
    count = len(sizes)
    if not days or count == 0:
        return [None] * count

    days = max(1, min(days, count))
    target = sum(sizes) / days

    out, day, acc = [], 1, 0
    for i, size in enumerate(sizes):
        if day < days:
            # Staying would leave a later day with no section at all.
            starves_a_later_day = (count - i - 1) < (days - day)
            # Otherwise move on once this day has taken its share, counting a
            # section as belonging to the day its bulk falls in.
            day_is_full = acc and acc + size / 2 > target
            if starves_a_later_day or day_is_full:
                day += 1
                acc = 0
        out.append(day)
        acc += size
    return out


def split_sections_for_days(sections: list[dict], days: Optional[int]) -> list[dict]:
    """Splits the largest sections until there is one per requested day, where possible.

    One section per document is too coarse for a plan: a single document asked over
    three days would otherwise give one section and two empty days. Parts keep the
    concept order and the source document, so they still read in sequence.
    """
    out = [dict(section) for section in sections]
    if not days:
        return out
    while len(out) < days:
        index = max(range(len(out)), key=lambda i: len(out[i]["concepts"]), default=None)
        if index is None or len(out[index]["concepts"]) < 2 * MIN_CONCEPTS_PER_PART:
            break
        concepts = out[index]["concepts"]
        half = len(concepts) // 2
        first = {**out[index], "concepts": concepts[:half]}
        second = {**out[index], "concepts": concepts[half:]}
        out[index:index + 1] = [first, second]
    return out


def assign_days(sections: list[dict], days: Optional[int]) -> tuple[list[dict], Optional[int]]:
    """Sets `day` on every section and returns the number of days actually used.

    With fewer sections than days the plan is shortened rather than leaving empty days.
    """
    day_of = split_across_days([len(s.get("concepts") or []) for s in sections], days)
    assigned = [{**section, "day": day} for section, day in zip(sections, day_of)]
    used = max((d for d in day_of if d), default=None)
    return assigned, used


def _valid_day(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def repair_note_days(note: dict) -> dict:
    """Fills in missing or invalid days on a stored plan note.

    Notes planned before days were assigned have null days. The plan is rebuilt from the
    stored section order so every section shows a real day and `days` matches it.
    """
    sections = note.get("sections") or []
    if note.get("kind") != "plan" or not sections:
        return note
    days = [section.get("day") for section in sections]
    if all(_valid_day(d) for d in days) and days == sorted(days) and set(days) == set(range(1, days[-1] + 1)):
        note["days"] = days[-1]
        return note
    requested = note.get("days") if _valid_day(note.get("days")) else len(sections)
    fixed, used = assign_days(sections, requested)
    note["sections"] = fixed
    note["days"] = used
    return note
