"""
Key-sentence extraction: the sentences a document's revision note must cover,
picked without an LLM.

Candidates are sentence-like units cut from the extracted text. They are ranked
by centrality — TextRank over sentence embeddings — with small boosts for
definitions, numbered steps and figures, then picked with a per-region quota so
a derivation on page 3 is not crowded out by the sentences everything else
resembles. Nothing here is subject-specific: every rule is about layout
(headings, bullets, captions, running heads, reference lists) or about the
shape of a sentence, not about what it says.

Embedding is done by whatever `encode` the caller hands in. notes_service
passes the engine's own SentenceTransformer, so no second model is loaded.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Callable, Optional

import numpy as np

# Bump when anything below changes what gets picked; cached picks are keyed on
# it together with the document text.
VERSION = "keysent-v2"

# About one pick per this many characters of text, within these bounds.
CHARS_PER_PICK = 400
MIN_PICKS = 5
MAX_PICKS = 20

# The document is cut into this many equal slices. Each slice with candidates
# gets a share of the picks in proportion to how many it holds, at least one,
# before the rest are filled by score.
REGIONS = 10

# A candidate this similar to one already picked says the same thing.
REDUNDANT = 0.80

BOOST_DEFINITION = 0.30
BOOST_STEP = 0.15
BOOST_NUMBER = 0.10
# A result or conclusion ("⇒ max h ≈ 1.440 lg n", "therefore", "as a result")
# is short and symbol-heavy, so centrality alone ranks it below the prose
# around it.
BOOST_CONCLUSION = 0.20

_BULLET = re.compile(r"^\s*(?:[•●▪◦‣∙\-–—*]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s+")
_TERMINAL = re.compile(r"[.!?;:]\s*$")
_CAPTION = re.compile(
    r"^\s*(?:fig(?:ure)?|table|diagram|chart|exhibit|plate)\.?\s*\d+[a-z]?\s*[:.—–-]", re.I)
# Relations make a line a statement even without a verb: "h < 2 lg Nh".
_RELATION = re.compile(r"[=<>≤≥≈≠⇒→⇔∝]")
# Formula lines continue the statement above them rather than breaking it, so
# a derivation laid out one term per line stays one unit.
_FORMULA = re.compile(r"[=<>≤≥≈≠⇒→⇔∝+×·÷√∑∫^/−]|[A-Za-zΘΩΟθωο]\(")
_AUX = frozenset(
    "is are was were be been being am has have had do does did "
    "can could will would shall should may might must".split())
_OVERVIEW = re.compile(
    r"^\s*(?:lecture\s+|course\s+|chapter\s+|session\s+|unit\s+)?"
    r"(?:overview|outline|agenda|contents|table of contents|"
    r"learning (?:objectives|outcomes)|objectives|topics(?: covered)?)\s*:?\s*$", re.I)
_REFERENCES = re.compile(
    r"^\s*(?:[ivxlc]+\.|\d+\.?)?\s*(?:references|bibliography|works cited|"
    r"literature cited|reference list|further reading)\s*:?\s*$", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_AFFILIATION = re.compile(
    r"\b(?:universit(?:y|ies|ät|é)|institut(?:e|o|ion)?|department|faculty|school of|"
    r"college|centre|center|laborator(?:y|ies)|division|inc\.|ltd|corporation|"
    r"hospital|foundation)\b", re.I)
_HONORIFIC = re.compile(r"^\s*(?:dr|prof|professor|mr|mrs|ms|mx)\.?\s+[A-Z]", re.I)
_DEFINITION = re.compile(
    r"\b(?:is|are) (?:a|an|the|defined as|called|known as)\b|\brefers? to\b|"
    r"\bmeans?\b|\bdefined as\b|\bdenotes?\b|^[^:]{2,40}:\s+\S", re.I)
_STEP = re.compile(
    r"^(?:step\s*\d+|\d+[.)]\s|first(?:ly)?\b|second(?:ly)?\b|then\b|next\b|finally\b)", re.I)
_CONCLUSION = re.compile(
    r"[⇒∴]|\b(?:therefore|thus|hence|consequently|as a result|it follows)\b", re.I)
_WORD = re.compile(r"[^\W\d_][\w'’-]*")

# Front matter sits in the opening stretch of a document; title and
# affiliation rules only apply there, the overview rule a little further.
_FRONT = 0.12
_OVERVIEW_ZONE = 0.25
# A reference heading is only believed in the second half.
_REFERENCES_FROM = 0.5


def _letters(s: str) -> float:
    compact = s.replace(" ", "")
    return sum(ch.isalpha() for ch in compact) / max(len(compact), 1)


def _is_label(line: str) -> bool:
    """A figure value or page number: very short, or mostly digits and
    symbols. A formula line is never one, however short: a derivation laid
    out one term per line has lines holding only "=" or ">"."""
    if _FORMULA.search(line):
        return False
    return len(line.replace(" ", "")) <= 3 or _letters(line) < 0.5


def _capitalised(line: str) -> float:
    words = _WORD.findall(line)
    return sum(1 for w in words if w[0].isupper()) / max(len(words), 1)


def _is_heading(line: str) -> bool:
    """A title-case line with no closing punctuation: a heading or a slide
    title, which breaks units and is not a key point itself. Numbered
    headings ("1. Introduction to the Classical Framework") count too. A line
    stating a relation is a formula, not a heading, however capitalised:
    "Nh > Fh (nth Fibonacci number)"."""
    words = _WORD.findall(line)
    if not words or len(words) > 8 or _TERMINAL.search(line) or _RELATION.search(line):
        return False
    return _capitalised(line) >= 0.6


def _is_clause(s: str) -> bool:
    """
    Whether a unit reads as a statement rather than a heading or a label.

    No part-of-speech tagger is installed, so this is a heuristic: a relation
    symbol, an auxiliary or modal verb, or five or more words most of which
    are lower case — which is what separates prose and imperative bullets
    ("insert each item into AVL tree") from title-case headings.
    """
    if _RELATION.search(s):
        return True
    words = _WORD.findall(s)
    if any(w.lower() in _AUX for w in words):
        return True
    if len(words) >= 5:
        lower = sum(1 for w in words[1:] if w[0].islower())
        return lower >= 0.5 * (len(words) - 1)
    return False


def content_lines(text: str) -> list[tuple[int, Optional[str]]]:
    """
    (offset, line) for every non-blank line of the text, with the line set to
    None where it is not the document's own content: running heads, the title
    and author/affiliation lines at the front, an overview or outline list,
    and everything from a References-type heading on.

    Shared by the picker and by notes_service's concept positions, so a
    concept named in a lecture's overview list is placed where the lecture
    teaches it, not at 2%.
    """
    n = max(len(text), 1)
    lines, pos = [], 0
    for raw in text.split("\n"):
        if raw.strip():
            lines.append((pos + len(raw) - len(raw.lstrip()), raw.strip()))
        pos += len(raw) + 1

    counts = Counter(l for _, l in lines)
    end = n
    for off, line in lines:
        if off > n * _REFERENCES_FROM and _REFERENCES.match(line):
            end = off
            break

    out: list[tuple[int, Optional[str]]] = []
    in_overview = False
    # The title is the block of heading-shaped lines the document opens with;
    # the first bullet, overview or line of content closes it, so a section
    # heading further down the first page is kept.
    in_title = True
    for off, line in lines:
        if off >= end:
            out.append((off, None))
            continue
        formula = bool(_FORMULA.search(line))
        # Running heads repeat on every page. Formula lines are exempt: a
        # derivation repeats "=" and ">" legitimately.
        running_head = counts[line] >= 3 and not formula
        if running_head:
            out.append((off, None))
            continue
        author = off < n * _FRONT and (
            _EMAIL.search(line) or _HONORIFIC.match(line)
            or (_AFFILIATION.search(line) and not _TERMINAL.search(line)
                and len(line.split()) <= 12 and _capitalised(line) >= 0.5))
        if author:
            out.append((off, None))
            continue
        if off < n * _OVERVIEW_ZONE and _OVERVIEW.match(line):
            in_overview = True
            in_title = False
            out.append((off, None))
            continue
        if in_title and off < n * _FRONT and not _BULLET.match(line) and _is_heading(line):
            out.append((off, None))
            continue
        in_title = False
        # The overview is its bullet list; the first line that is not a
        # bullet is content again.
        if in_overview and _BULLET.match(line):
            out.append((off, None))
            continue
        in_overview = False
        out.append((off, line))
    return out


def content_text(text: str) -> str:
    """The document's content lines only, one per line (see content_lines)."""
    return "\n".join(line for _, line in content_lines(text) if line)


def candidate_units(text: str) -> list[tuple[int, str]]:
    """
    (offset, unit) pairs: the sentence-like units a pick is chosen from.

    Lines are merged into statements, since PDFs and slide decks break text
    into short lines with no full stops. A line starts a new unit when it is a
    bullet or the previous unit has ended; it breaks the current unit when it
    is a caption, a figure label, a heading or not content at all (see
    content_lines). A short "Label:" line is carried onto the unit after it,
    so bullets under "AVL sort:" keep the name of what they describe, and
    consecutive bullets under one label are one unit: a labelled list makes
    one point.
    """
    units: list[tuple[int, list[str]]] = []
    current: list | None = None
    prefix: str | None = None

    def close():
        nonlocal current
        if current is not None:
            units.append((current[0], current[1]))
            current = None

    for off, line in content_lines(text):
        if line is None:
            close()
            continue
        formula = bool(_FORMULA.search(line))
        if _CAPTION.match(line):
            close()
            continue
        if _is_label(line):
            close()
            continue

        bullet = bool(_BULLET.match(line))
        if formula and not bullet and current is not None:
            current[1].append(line)
            continue
        if line.endswith(":") and len(line.split()) <= 4:
            close()
            prefix = line[:-1].strip()
            continue
        if _is_heading(_BULLET.sub("", line)):
            close()
            prefix = None
            continue

        body = _BULLET.sub("", line)
        if bullet and prefix and current is not None and current[2] == prefix:
            current[1].append("; " + body)
        elif bullet or current is None or _TERMINAL.search(current[1][-1]):
            close()
            current = [off, [f"{prefix}: {body}" if prefix else body],
                       prefix if bullet else None]
            if not bullet:
                prefix = None
        else:
            current[1].append(body)
    close()

    out = []
    for off, parts in units:
        merged = " ".join(parts).replace(" ; ", "; ")
        start = 0
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z(“\"])", merged):
            s = sentence.strip()
            at = merged.find(s, start)
            start = max(at, start)
            if _keep(s):
                out.append((off + max(at, 0), s))
    return out


def _keep(s: str) -> bool:
    if len(s.split()) < 4:
        return False
    floor = 0.25 if _RELATION.search(s) else 0.5
    return _letters(s) >= floor and _is_clause(s)


def _textrank(emb: np.ndarray, damping: float = 0.85, iters: int = 60) -> np.ndarray:
    sim = np.clip(emb @ emb.T, 0, None)
    np.fill_diagonal(sim, 0)
    rows = sim.sum(1, keepdims=True)
    rows[rows == 0] = 1
    step = sim / rows
    rank = np.full(len(emb), 1 / len(emb))
    for _ in range(iters):
        rank = (1 - damping) / len(emb) + damping * step.T @ rank
    return rank


def _boost(s: str) -> float:
    return (1.0 + BOOST_DEFINITION * bool(_DEFINITION.search(s))
            + BOOST_STEP * bool(_STEP.search(s))
            + BOOST_NUMBER * bool(re.search(r"\d", s))
            + BOOST_CONCLUSION * bool(_CONCLUSION.search(s)))


def pick(text: str, encode: Callable) -> list[dict]:
    """
    The must-cover sentences of one document, in document order:
    [{"text", "offset", "fraction", "score"}].

    `encode(list[str])` must return L2-normalised embeddings.
    """
    units = candidate_units(text)
    if not units:
        return []
    emb = np.asarray(encode([u for _, u in units]))
    score = _textrank(emb) * np.array([_boost(u) for _, u in units])
    k = int(np.clip(len(text) // CHARS_PER_PICK, MIN_PICKS, MAX_PICKS))
    n = max(len(text), 1)
    region = [min(REGIONS - 1, int(REGIONS * off / n)) for off, _ in units]
    order = list(np.argsort(-score))

    chosen: list[int] = []

    def fresh(i):
        return all(float(emb[i] @ emb[j]) < REDUNDANT for j in chosen)

    # Each region's share first, so no stretch of the document goes unpicked
    # and a dense stretch (a derivation, a comparison) gets more than one...
    per_region = Counter(region)
    share = {r: max(1, round(k * c / len(units))) for r, c in per_region.items()}
    taken: Counter = Counter()
    for i in order:
        if taken[region[i]] < share[region[i]] and fresh(i):
            taken[region[i]] += 1
            chosen.append(i)
    if len(chosen) > k:
        chosen = sorted(chosen, key=lambda i: -score[i])[:k]
    # ...then the rest by score.
    for i in order:
        if len(chosen) >= k:
            break
        if i not in chosen and fresh(i):
            chosen.append(i)

    return [{"text": units[i][1], "offset": units[i][0],
             "fraction": units[i][0] / n, "score": round(float(score[i]), 5)}
            for i in sorted(chosen, key=lambda i: units[i][0])]
