import json
import logging
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from pathlib import Path

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from services.embeddings import encode_texts
from app.config import (
    COMPLETION_TOKEN_PARAMETER,
    EXTRACTION_MODEL,
    FUSION_DIR,
    NON_RAG_REASONING_EFFORT,
)
from services.fusion_log import FusionLog
from services.llm_gateway import estimate_tokens, graph_limiter
from services.llm_provider import BAD_REQUEST_ERRORS, RATE_LIMIT_ERRORS

_MODEL = EXTRACTION_MODEL
logger = logging.getLogger(__name__)

AUTO_MERGE_SIM = 0.95
# Pairs at or above this that fail the name check are logged as possible synonyms, not merged.
POSSIBLE_SYNONYM_SIM = 0.85
NEGATION_PREFIXES = ("un", "non", "in", "im", "dis", "ir", "a", "anti", "de", "mis")

# Pairs per verdict call (steps 3 and 5), from FUSION_VERDICT_BATCH, clamped to 1-25.
_VERDICT_PAIR_BATCH = min(25, max(1, int(os.getenv("FUSION_VERDICT_BATCH", "10"))))
# Resends of the same request after malformed JSON. At temperature 0 a resend mostly repeats
# itself, so one is enough; 429s count separately.
_VERDICT_MAX_RETRIES = 1
# Cap on pairs a failed batch may re-ask one at a time; each single costs a call. At the default
# batch of 10 it matches the batch, so a malformed batch re-asks every pair.
_VERDICT_MAX_SINGLES = min(_VERDICT_PAIR_BATCH, int(os.getenv("FUSION_VERDICT_MAX_SINGLES", "10")))
_NEIGHBOURS_PER_NODE = 5
# Steps 3 and 5 send the few sentences nearest both names, not chunk openings.
_EVIDENCE_CHUNKS_PER_SIDE = 2
_EVIDENCE_SENTENCES_PER_PAIR = 3
_EVIDENCE_SENTENCE_CHARS = 240
_SUMMARY_SENTENCE_CHARS = 200

# Output cap per verdict call: a 4-pair batch measured 195-350 tokens, so 450 per 4 pairs.
_VERDICT_MAX_TOKENS = -(-450 * _VERDICT_PAIR_BATCH // 4)
# Step 3 answers only same/different (about 12-20 tokens a pair); step 5 keeps the cap above.
_STEP3_MAX_TOKENS = 40 + 30 * _VERDICT_PAIR_BATCH
# Characters of a response body logged when it parses but has the wrong shape.
_RAW_LOG_CHARS = 500
# Retries after a 429, waiting what the server asks; fusion runs in the background, so waiting is
# free.
_RATE_LIMIT_RETRIES = 2
# Concurrent verdict calls: a 4-pair call measured 850-1,050 input tokens and 1.4-2.7 s on OpenAI.
_FUSION_MAX_WORKERS = int(os.getenv("FUSION_MAX_WORKERS", "8"))
_FUSION_GLOBAL_SLOTS = max(1, int(os.getenv("FUSION_GLOBAL_SLOTS", "16")))
_FUSION_SLOT_SEMAPHORE = threading.BoundedSemaphore(_FUSION_GLOBAL_SLOTS)
_CALL_INPUT_TOKENS = 275 * _VERDICT_PAIR_BATCH
_CALL_SECONDS = 3.0

# v7: merge-only (v6 carried relations). v8: no canonical field; labels are chosen without the LLM.
_STEP3_PROMPT_VERSION = "step3-merge-v8"
_STEP5_PROMPT_VERSION = "step5-evidence-v5"

VERDICT_CACHE_FILE = FUSION_DIR / "verdict_cache.json"
FUSION_LOG_DIR = FUSION_DIR

_BRIDGE_MIN_SHARED_NEIGHBOURS = 2
_HUB_DEGREE_PERCENTILE = 90.0
_HUB_MIN_ABSOLUTE_DEGREE = 8


_STRIP_CHARS = re.compile(r"[,;:\"'`''""]")
_WS = re.compile(r"\s+")
# Sentence ends, or line breaks: slide text often has no full stops.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


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


def _is_modifier_variant(a, b):
    """True when one name's words are a strict subset of the other's ('tree' / 'binary tree'):
    never auto-merged, and step 3 holds SAME to a stricter bar."""
    ta = list(dict.fromkeys(normalize_concept(a).split()))
    tb = list(dict.fromkeys(normalize_concept(b).split()))
    if not ta or not tb or len(ta) == len(tb):
        return False
    shorter, longer = (ta, tb) if len(ta) < len(tb) else (tb, ta)
    unused = list(longer)
    for word in shorter:
        match = next((w for w in unused if _words_match(word, w)), None)
        if match is None:
            return False
        unused.remove(match)
    return True


def _words_match(x, y):
    return x == y or y in (x + "s", x + "es") or x in (y + "s", y + "es")


# Symbols that change what a name means, so they are not stripped as punctuation: B+ tree, C#.
_MEANINGFUL_SYMBOLS = "+#"


def _singular(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _surface_key(name):
    """A name without case, hyphens, spacing, punctuation or plural endings."""
    text = re.sub(r"[^\w\s" + re.escape(_MEANINGFUL_SYMBOLS) + r"]", " ", normalize_concept(name))
    return "".join(_singular(w) for w in text.split())


def _is_morphological_variant(a, b, aliases=None):
    """True when two names differ only in form (case, hyphen, spacing, punctuation, plural) or in
    abbreviations: a document-defined one, a trailing (ACRONYM), or an initial like 'm.'. Only these
    merge on SAME."""
    ka, kb = _surface_key(a), _surface_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    aliases = aliases or {}
    if (kb in {_surface_key(x) for x in aliases.get(a, ())}
            or ka in {_surface_key(x) for x in aliases.get(b, ())}):
        return True
    expand = _alias_expansions(aliases)
    wa, wb = _name_words(a, expand), _name_words(b, expand)
    return _surface_key(" ".join(wa)) == _surface_key(" ".join(wb)) or _initial_match(wa, wb)


_TRAILING_PAREN = re.compile(r"^(.*\S)\s*\(([^()]{1,15})\)$")
_INITIAL = re.compile(r"[a-z]\.")


def _is_acronym(abbr, words):
    """Right to left, abbr's letters occur in order in words: the last in the last word, the first
    in the first word."""
    letters = [c for c in abbr if c.isalnum()]
    text = " ".join(words)
    if not letters or len(letters) > 10 or len(words) < 2:
        return False
    pos = len(text)
    for i, c in enumerate(reversed(letters)):
        pos = text.rfind(c, 0, pos)
        if pos < 0 or (i == 0 and pos < len(text) - len(words[-1])):
            return False
    return pos < len(words[0])


def _alias_expansions(aliases):
    """{abbreviation: full name} for document-defined abbreviations; ambiguous ones are skipped."""
    expand = {}
    for full, abbrs in aliases.items():
        for abbr in abbrs:
            expand.setdefault(abbr, set()).add(normalize_concept(full))
    return {abbr: next(iter(fulls)) for abbr, fulls in expand.items() if len(fulls) == 1}


def _name_words(name, expand):
    """A name's words with a trailing (ACRONYM) of itself removed and abbreviations expanded."""
    text = normalize_concept(name)
    m = _TRAILING_PAREN.match(text)
    if m and _is_acronym(m.group(2), m.group(1).split()):
        text = m.group(1)
    return [w for t in text.split() for w in expand.get(t, t).split()]


def _initial_match(wa, wb):
    """An initial with a period stands for one word ("m. tuberculosis"); a bare letter does not
    (b tree). Every other word must match, at least one exactly."""
    if len(wa) != len(wb):
        return False
    same = [_surface_key(x) == _surface_key(y) for x, y in zip(wa, wb)]
    initial = [bool(_INITIAL.fullmatch(x) and len(y) > 2 and y[0] == x[0]
                    or _INITIAL.fullmatch(y) and len(x) > 2 and x[0] == y[0])
               for x, y in zip(wa, wb)]
    return any(same) and any(initial) and all(s or i for s, i in zip(same, initial))


def _variant_pairs(names, aliases):
    """Every (a, b) pair of names, a < b, that _is_morphological_variant accepts. Names are grouped
    by normalised key instead of checking all n^2 pairs; initials get a pass of their own."""
    expand = _alias_expansions(aliases)
    words = {n: _name_words(n, expand) for n in names}
    groups = {}
    for n in names:
        for key in {_surface_key(n), _surface_key(" ".join(words[n]))}:
            if key:
                groups.setdefault(key, set()).add(n)
    # A name and its document-defined abbreviation used as a name of its own ("ltbi").
    for full, abbrs in aliases.items():
        for abbr in abbrs:
            if full in words and _surface_key(abbr) in groups:
                groups[_surface_key(abbr)].add(full)
    found = {tuple(sorted(p)) for g in groups.values() for p in combinations(g, 2)}
    by_length = {}
    for n in names:
        by_length.setdefault(len(words[n]), []).append(n)
    for n in names:
        if any(_INITIAL.fullmatch(w) for w in words[n]):
            found |= {tuple(sorted((n, m))) for m in by_length[len(words[n])]
                      if m != n and _initial_match(words[n], words[m])}
    # Grouping over-collects slightly (two keys per name); the check itself decides.
    return sorted(p for p in found if _is_morphological_variant(p[0], p[1], aliases))


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


# Labels that say two concepts touch without saying how; they rank below every specific label.
_GENERIC_RELATIONS = _VAGUE_RELATIONS | {"involves", "linked to", "is linked to"}


def _is_generic_relation(rel):
    return _clean_relation(rel).lower().replace("_", " ") in _GENERIC_RELATIONS


_HIER_BROADER_FIRST = {"generalizes", "generalises", "includes", "include",
                       "subsumes", "encompasses", "comprises", "contains",
                       "underlies", "underlie"}
_HIER_NARROWER_FIRST = {"is a", "is an", "is a type of", "type of", "is type of",
                        "is a kind of", "kind of", "is a form of", "form of",
                        "specializes", "specialises", "is a subtype of",
                        "subtype of", "is a special case of", "special case of",
                        "instance of", "is an instance of"}


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
    # Irregular participles the '-ed' rule below cannot reach.
    "led": "leads", "fed": "feeds", "bred": "breeds", "held": "holds",
    "driven": "drives", "written": "writes", "known": "knows", "taken": "takes",
    "seen": "sees", "found": "finds", "kept": "keeps", "chosen": "chooses",
    "drawn": "draws", "taught": "teaches", "understood": "understands",
    "brought": "brings", "caught": "catches", "sought": "seeks", "run": "runs",
    "set": "sets", "sent": "sends", "broken": "breaks", "hidden": "hides",
    "grown": "grows", "thrown": "throws", "spoken": "speaks", "told": "tells",
    "cut": "cuts", "put": "puts", "hit": "hits", "bound": "binds",
    "overcome": "overcomes", "undergone": "undergoes", "spread": "spreads",
    "shed": "sheds", "split": "splits", "read": "reads", "rebuilt": "rebuilds",
    "upheld": "upholds", "overthrown": "overthrows", "broadcast": "broadcasts",
    "forecast": "forecasts", "offset": "offsets",
}
_PASSIVE_RE = re.compile(r"^(?:is|are|was|were|be|been|being)\s+(.+?)\s+by$")

_VOWELS = "aeiouy"
# '-ed' stems the rule below gets wrong, one way or the other.
_STEM_TAKES_E = {"cit", "excit", "invit", "ignit", "incit", "recit", "unit",
                 "complet", "delet", "compet", "secret", "excret", "deplet",
                 "creat", "rout",
                 "stor", "restor", "scor", "explor", "ignor", "implor", "deplor"}
_STEM_NO_E = {"focus", "bias"}


def _syllables(word):
    return len(re.findall(r"[aeiouy]+", word))


def _stem_takes_e(s):
    """Whether an '-ed' stem lost a silent e: 'regulat' -> 'regulate' but
    'inhibit' stays. English spelling decides this, so it is a heuristic."""
    if s in _STEM_TAKES_E:
        return True
    if s in _STEM_NO_E or len(s) < 2:
        return False
    last, prev = s[-1], s[-2]
    if last in "vcue":                              # achiev, produc, argu, agre
        return True
    if last == "z":                                 # catalyz, optimiz
        return prev != "z"
    if last == "s":                                 # catalys, caus, releas, not process
        return prev != "s"
    if last == "g":                                 # merg, judg, manag, chang, challeng
        return prev in "rda" or s.endswith(("ang", "eng"))
    if last == "l" and prev not in _VOWELS:         # handl, enabl, not control
        return prev not in "lr"
    if last == "r" and prev not in _VOWELS:         # centr, not alter
        return True
    if s.endswith("at"):                            # regulat, mediat, not treat
        return not s.endswith(("eat", "oat", "aat"))
    if s.endswith(("uad", "uid")):                  # persuad, guid, not load
        return True
    # Otherwise only a single vowel between consonants can hide an e. The u
    # of 'qu' and 'gu' counts as a consonant (acquir, not acquirs).
    before = s[-3] if len(s) >= 3 else ""
    if before == "u" and len(s) >= 4 and s[-4] in "qg":
        before = s[-4]
    if not before or prev not in _VOWELS or before in _VOWELS:
        return False
    one_syllable = _syllables(s) == 1
    if last == "t":
        return prev in "ou"                         # promot, comput, not inhibit
    if last == "d":
        return prev in "aeiou"                      # degrad, decid, encod, supersed
    if last in "kb":
        return True                                 # invok, describ
    if last == "m":
        return True                                 # assum, consum, not form
    if last == "n":
        return prev in "iu" or (prev == "o" and one_syllable)   # determin, tun, zon
    if last == "r":
        return prev in "aiuy"                       # requir, measur, not order or monitor
    if last == "p":
        return one_syllable                         # shap, typ, not develop
    if last == "l":
        # schedul, compil, consol, scal; not model, label, signal
        return prev in "iou" or (prev == "a" and one_syllable)
    return False


def _third_person(stem):
    if len(stem) > 1 and stem[-1] == "y" and stem[-2] not in _VOWELS:
        return stem[:-1] + "ies"
    if stem.endswith(("s", "x", "z", "ch", "sh", "o")):
        return stem + "es"
    return stem + "s"


def _active_from_participle(word):
    """Present-tense active form of a regular '-ed' participle ('regulated' -> 'regulates'), or
    None."""
    if word.endswith("ied") and len(word) > 4:
        return word[:-3] + "ies"                    # applied, classified
    if not word.endswith("ed") or len(word) < 5:
        return None
    s = word[:-2]
    if len(s) >= 4 and s[-1] == s[-2] and s[-1] in "bdgmnprt":
        s = s[:-1]                                  # stopped, referred, embedded
    elif s.endswith("ll") and (_syllables(s) >= 2 or s.endswith(("uell", "iall", "uall"))):
        s = s[:-1]                                  # controlled, labelled, fuelled, not called
    elif _stem_takes_e(s):
        s += "e"
    return _third_person(s)


def _passive_to_active(rel):
    """'<participle> by' -> the active verb, read with the ends swapped: 'A regulated by B' is
    'B regulates A'."""
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
    active = _PARTICIPLE_ACTIVE.get(toks[-1]) or _active_from_participle(toks[-1])
    if active is None:
        return None
    return " ".join(toks[:-1] + [active]).strip()


def _orient(a, b, relation, direction, broader):
    """(from, to, relation, broader_conflict). broader sets the direction only for taxonomy
    labels; the conflict flag means it disagreed with `direction`."""
    relation = _clean_relation(relation)
    active = _passive_to_active(relation)
    swap_passive = active is not None
    if swap_passive:
        relation = active
    frm, to = (b, a) if str(direction) == "B_to_A" else (a, b)
    if swap_passive:
        frm, to = to, frm
    conflict = False
    hier = _taxonomy_kind(_label_key(relation))
    if hier and str(broader).strip().upper() in ("A", "B"):
        broad = a if broader.strip().upper() == "A" else b
        narrow = b if broader.strip().upper() == "A" else a
        by_broader = (broad, narrow) if hier == "broader_first" else (narrow, broad)
        conflict = by_broader != (frm, to)
        frm, to = by_broader
    return frm, to, relation, conflict


def _orient_relation(a, b, relation, direction, broader):
    return _orient(a, b, relation, direction, broader)[:3]


def _label_names_concept(label, name):
    """Whether a relation label contains a concept name as whole words, so a
    label naming an end is rejected but 'generates' is not taken for 'gene'."""
    key = normalize_concept(name)
    return bool(key) and re.search(
        r"(?<!\w)" + re.escape(key) + r"(?!\w)", normalize_concept(label)) is not None


def _name_pattern(name):
    # Whole words, any spacing, and a plain plural ('binary search trees').
    words = normalize_concept(name).split()
    return re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, words)) + r"(?:e?s)?(?!\w)")


def _name_positions(a, b, text):
    """Where names a and b first appear in text, or None. A containing name is blanked first so
    the shorter cannot match inside it."""
    text = normalize_concept(text)
    na, nb = normalize_concept(a), normalize_concept(b)
    if not text or not na or not nb or na == nb:
        return None, None
    longer, shorter = (na, nb) if len(na) >= len(nb) else (nb, na)
    long_re = _name_pattern(longer)
    m_long = long_re.search(text)
    masked = long_re.sub(lambda m: " " * len(m.group()), text)
    m_short = _name_pattern(shorter).search(masked)
    pos = {longer: m_long.start() if m_long else None,
           shorter: m_short.start() if m_short else None}
    return pos[na], pos[nb]


def _sentence_direction(a, b, sentence):
    """'A_to_B' when a comes first in the relation sentence, 'B_to_A' when b
    does, None when the sentence does not contain both names."""
    pa, pb = _name_positions(a, b, sentence)
    if pa is None or pb is None:
        return None
    return "A_to_B" if pa < pb else "B_to_A"


# A 'related' verdict's own sentence must name both concepts, by name (singular or plural) or by an
# abbreviation the documents define; otherwise the edge is dropped.

_ABBREVIATION = re.compile(r"\(\s*([A-Za-z][A-Za-z0-9]{1,9})\s*\)")
# Characters of text before "(ABBR)" searched for the name it abbreviates.
_ABBREVIATION_WINDOW = 160


def _plural_pattern(word):
    """Regex for a word in its singular or regular plural form, whichever was given."""
    if len(word) <= 2 or not word.isalpha():
        return re.escape(word)
    if word.endswith("ies") and len(word) > 4:
        return re.escape(word[:-3]) + "(?:y|ies)"
    if word.endswith("y") and word[-2] not in "aeiou":
        return re.escape(word[:-1]) + "(?:y|ies)"
    if word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return re.escape(word[:-2]) + "(?:es)?"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return re.escape(word[:-1]) + "(?:e?s)?"
    return re.escape(word) + "(?:e?s)?"


def _phrase_pattern(name):
    """A normalised name as whole words, its last word singular or plural."""
    words = normalize_concept(name).split()
    if not words:
        return None
    body = r"\s+".join([re.escape(w) for w in words[:-1]] + [_plural_pattern(words[-1])])
    return re.compile(r"(?<!\w)" + body + r"(?!\w)")


def _abbreviates(abbr, name):
    """Whether abbr can abbreviate name: its letters occur in order, the first at a word start
    (Schwartz and Hearst)."""
    short, long_ = abbr.lower(), normalize_concept(name)
    si, li = len(short) - 1, len(long_) - 1
    while si >= 0:
        c = short[si]
        if not c.isalnum():
            si -= 1
            continue
        while li >= 0 and (long_[li] != c or (si == 0 and li > 0 and long_[li - 1].isalnum())):
            li -= 1
        if li < 0:
            return False
        li -= 1
        si -= 1
    return True


def _engine_texts(engine):
    return [ch.get("text") or "" for chunks in getattr(engine, "chunks_by_week", {}).values()
            for ch in chunks]


def _source_aliases(texts, names):
    """{name: {abbreviation}} for every name the texts define as '<name> (ABBR)'. Nothing is
    assumed from outside the documents."""
    patterns = {n: _phrase_pattern(n) for n in names}
    out = {}
    for text in texts:
        text = re.sub("­\\s*", "", unicodedata.normalize("NFKC", text))
        for m in _ABBREVIATION.finditer(text):
            abbr = m.group(1)
            before = normalize_concept(text[max(0, m.start() - _ABBREVIATION_WINDOW):m.start()])
            for name, pattern in patterns.items():
                if pattern is None or normalize_concept(abbr) == normalize_concept(name):
                    continue
                last = None
                for last in pattern.finditer(before):
                    pass
                if last is not None and last.end() == len(before) and _abbreviates(abbr, name):
                    out.setdefault(name, set()).add(abbr.lower())
    return out


def _named_both(a, b, sentence, aliases=None):
    """Whether a sentence names both concepts, by name or defined abbreviation, at places that do
    not overlap ('binary search' inside 'binary search tree' does not count)."""
    aliases = aliases or {}
    text = normalize_concept(sentence)
    if not text:
        return False

    def spans(name):
        found = [p for p in [_phrase_pattern(name)] if p is not None]
        found += [re.compile(r"(?<!\w)" + re.escape(al) + r"s?(?!\w)") for al in sorted(aliases.get(name, ()))]
        return [m.span() for p in found for m in p.finditer(text)]

    return any(sa[1] <= sb[0] or sb[1] <= sa[0] for sa in spans(a) for sb in spans(b))


def _verdict_direction(a, b, r):
    """Direction for a 'related' verdict and whether the sentence gave it.
    The sentence is read first; the direction field is the fallback."""
    derived = _sentence_direction(a, b, r.get("sentence") or "")
    if derived is not None:
        return derived, True
    return r.get("direction") or "A_to_B", False


def _load_verdict_cache():
    try:
        with open(VERDICT_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_verdict_cache(cache):
    # Temporary file swapped in, so a reader never loads a half-written cache.
    tmp = VERDICT_CACHE_FILE.with_suffix(".tmp")
    try:
        VERDICT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp, VERDICT_CACHE_FILE)
    except OSError:
        pass


def _pair_key(version, a, b):
    x, y = sorted((a, b))
    return f"{version}|{_MODEL}|{x}||{y}"


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
            # Names both concepts itself, so unlike direction it needs no swap.
            "sentence": _WS.sub(" ", str(r.get("sentence") or "")).strip()[:_SUMMARY_SENTENCE_CHARS],
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


# The prompt's prose says 'DIFFERENT BUT RELATED' and the model sometimes echoes it, so those
# variants are accepted; anything else is logged.
_VERDICT_SYNONYMS = {
    "same": "same",
    "related": "related",
    "unrelated": "unrelated",
    "different but related": "related",
    "different-but-related": "related",
    "different, but related": "related",
    # Step 3's merge-only answer for "not the same concept".
    "different": "unrelated",
}


def _normalise_verdict(raw):
    """Canonical verdict, or None when the answer is not one we recognise."""
    return _VERDICT_SYNONYMS.get(_WS.sub(" ", str(raw or "").strip().lower()))


def _verdict_or_unrelated(raw, log, tag, a, b):
    """Canonical verdict, defaulting to unrelated; an unmappable answer is reported with its
    pair."""
    verdict = _normalise_verdict(raw)
    if verdict is None:
        if str(raw or "").strip():
            log(f"{tag} unrecognised verdict {str(raw)!r} for {a!r} / {b!r} "
                f"— treated as unrelated, edge dropped")
        return "unrelated"
    return verdict


class _MalformedVerdict(Exception):
    pass


def _is_json_validate_failed(e):
    return "json_validate_failed" in str(e)


def retry_after_seconds(error):
    """The wait a 429 asks for in seconds, or None; retry-after-ms is preferred."""
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        try:
            value = float(headers.get(name))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value / scale
    return None


def is_request_too_large(error):
    """A 429 for a request whose expected output alone exceeds the per-minute
    limit. Waiting does not help, so it is never retried."""
    return "request too large" in str(error).lower()


def _raw_excerpt(raw):
    raw = _WS.sub(" ", str(raw or "")).strip()
    return raw if len(raw) <= _RAW_LOG_CHARS else raw[:_RAW_LOG_CHARS] + "…"


def _results_list(data):
    """The verdicts in a parsed answer: its 'results' list, or a bare verdict object with an
    'id' (one-pair answers). None otherwise."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("results"), list):
        return data["results"]
    if "id" in data:
        return [data]
    return None


def _verdict_attempt(client, system, payload, log, tag, failures=None, max_tokens=_VERDICT_MAX_TOKENS):
    user = "PAIRS:\n" + json.dumps(payload, ensure_ascii=False)
    attempt = rate_retries = 0
    rate_limit_count = 0
    started = time.perf_counter()
    limiter_wait = 0.0
    # Summed over every attempt this call made; finish_reason is the last response's.
    prompt_tokens = completion_tokens = reasoning_tokens = 0
    finish_reason = None
    try:
        while True:
            reason = None
            try:
                # Reserved from the budget extraction and evolution share, so
                # fusion no longer races them into the provider's per-minute limits.
                wait_started = time.perf_counter()
                graph_limiter.acquire_sync(estimate_tokens(system) + estimate_tokens(user), max_tokens)
                limiter_wait += time.perf_counter() - wait_started
            except ValueError as e:
                log(f"{tag} larger than the per-minute token budget ({e}) — not sent")
                return None, "over budget"
            try:
                completion = client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    model=_MODEL,
                    reasoning_effort=NON_RAG_REASONING_EFFORT,
                    response_format={"type": "json_object"},
                    # Accepted by OpenAI only with reasoning_effort "none".
                    temperature=0,
                    # OpenAI reasoning models use max_completion_tokens.
                    **{COMPLETION_TOKEN_PARAMETER: max_tokens},
                )
                usage = getattr(completion, "usage", None)
                prompt_tokens += getattr(usage, "prompt_tokens", None) or 0
                completion_tokens += getattr(usage, "completion_tokens", None) or 0
                details = getattr(usage, "completion_tokens_details", None)
                reasoning_tokens += getattr(details, "reasoning_tokens", None) or 0
                finish_reason = completion.choices[0].finish_reason
                raw = completion.choices[0].message.content or ""
                data = json.loads(raw)
                results = _results_list(data)
                if results is None or (payload and not results):
                    # Valid JSON in the wrong shape: the body is the only record
                    # of what the model actually sent.
                    log(f"{tag} unexpected response shape: {_raw_excerpt(raw)}")
                    raise _MalformedVerdict("no 'results' list" if results is None
                                            else "empty 'results' for non-empty batch")
                if not isinstance(data.get("results"), list):
                    log(f"{tag} bare verdict object taken as a one-item result: {_raw_excerpt(raw)}")
                return results, None
            except RATE_LIMIT_ERRORS as e:
                rate_limit_count += 1
                if failures is not None:
                    failures["rate_limited"] = failures.get("rate_limited", 0) + 1
                if is_request_too_large(e):
                    log(f"{tag} request too large for the per-minute limit — not retried")
                    return None, "request too large"
                if rate_retries >= _RATE_LIMIT_RETRIES:
                    log(f"{tag} still rate limited after {rate_retries} retries")
                    return None, "rate limited"
                rate_retries += 1
                asked = retry_after_seconds(e)
                delay = asked if asked is not None else min(60, 5 * (3 ** (rate_retries - 1)))
                log(f"{tag} rate limited (retry {rate_retries}/{_RATE_LIMIT_RETRIES}) — waiting {delay:.1f}s")
                time.sleep(delay)
                continue
            except (_MalformedVerdict, json.JSONDecodeError) as e:
                reason = f"malformed ({type(e).__name__})"
            except BAD_REQUEST_ERRORS as e:
                if not _is_json_validate_failed(e):
                    log(f"{tag} rejected — defaults to unrelated")
                    return None, "rejected"
                reason = "json_validate_failed"
                if failures is not None:
                    failures["json_validate_failed"] = failures.get("json_validate_failed", 0) + 1
            except Exception as e:
                reason = f"{type(e).__name__}"

            if attempt == _VERDICT_MAX_RETRIES:
                log(f"{tag} failed after {_VERDICT_MAX_RETRIES} retries ({reason})")
                return None, reason
            delay = min(60, 5 * (3 ** attempt))
            attempt += 1
            log(f"{tag} {reason} (attempt {attempt}/{_VERDICT_MAX_RETRIES}) — waiting {delay:.1f}s")
            time.sleep(delay)
    finally:
        if failures is not None:
            failures["retries"] = failures.get("retries", 0) + attempt + rate_retries
            failures["limiter_wait_seconds"] = failures.get("limiter_wait_seconds", 0.0) + limiter_wait
        logger.info(
            "fusion_verdict_call tag=%s pair_count=%d duration_ms=%.0f retries=%d "
            "rate_limit_count=%d rate_limit_retries=%d limiter_wait_ms=%.0f "
            "prompt_tokens=%d completion_tokens=%d reasoning_tokens=%d finish_reason=%s",
            tag, len(payload), (time.perf_counter() - started) * 1000,
            attempt + rate_retries, rate_limit_count, rate_retries, limiter_wait * 1000,
            prompt_tokens, completion_tokens, reasoning_tokens, finish_reason,
        )


def _verdict_call(client, system, payload, log, tag, failures=None, max_tokens=_VERDICT_MAX_TOKENS):
    results, reason = _verdict_attempt(client, system, payload, log, tag, failures, max_tokens)
    if results is not None:
        if payload and all("a" in item and "b" in item for item in payload):
            expected = {item.get("id") for item in payload}
            returned = {
                result.get("id") for result in results
                if isinstance(result, dict)
                and "id" in result
                and _normalise_verdict(result.get("verdict")) is not None
            }
            missing = len(expected - returned)
            if failures is not None and missing:
                failures["defaults_to_unrelated"] = failures.get("defaults_to_unrelated", 0) + missing
        return results
    if len(payload) <= 1:
        log(f"{tag} defaults to unrelated (not cached)")
        if failures is not None:
            failures["defaults_to_unrelated"] = failures.get("defaults_to_unrelated", 0) + len(payload)
        return []

    # Splitting isolates a malformed pair; it cannot help a rate limit, so only malformed output is
    # split, up to _VERDICT_MAX_SINGLES.
    if reason is None or not str(reason).startswith(("malformed", "json_validate_failed")):
        log(f"{tag} not splitting after {reason} — {len(payload)} pair(s) default to unrelated")
        if failures is not None:
            failures["defaults_to_unrelated"] = failures.get("defaults_to_unrelated", 0) + len(payload)
        return []

    head = payload[:_VERDICT_MAX_SINGLES]
    if len(payload) > len(head):
        log(f"{tag} splitting {len(head)} of {len(payload)}; "
            f"{len(payload) - len(head)} default to unrelated")
    else:
        log(f"{tag} splitting batch of {len(payload)} into singles")
    salvaged = []
    for item in head:
        one, _ = _verdict_attempt(client, system, [item], log, f"{tag} [id={item.get('id')}]", failures, max_tokens)
        if one:
            salvaged.extend(one)
    log(f"{tag} recovered {len(salvaged)}/{len(payload)} from singles")
    if failures is not None:
        expected = {item.get("id") for item in payload}
        returned = {
            result.get("id") for result in salvaged
            if isinstance(result, dict)
            and "id" in result
            and _normalise_verdict(result.get("verdict")) is not None
        }
        failures["defaults_to_unrelated"] = failures.get("defaults_to_unrelated", 0) + len(expected - returned)
    return salvaged


def fusion_workers(input_limit=None, output_limit=None):
    return _FUSION_MAX_WORKERS


def _run_verdict_calls(client, system, jobs, log, tag, failures, workers):
    """Sends (payload, max_tokens) jobs, `workers` at a time, and returns their results in job
    order. Each call's log lines and failure counts are replayed in job order too."""
    def one(job):
        payload, max_tokens = job
        lines, fails = [], {}
        with _FUSION_SLOT_SEMAPHORE:
            results = _verdict_call(client, system, payload, lines.append, tag, fails, max_tokens)
        return results, lines, fails

    def replay(out):
        results, lines, fails = out
        for line in lines:
            log(line)
        if failures is not None:
            for key, n in fails.items():
                failures[key] = failures.get(key, 0) + n
        return results

    if workers <= 1 or len(jobs) <= 1:
        return [replay(one(job)) for job in jobs]
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        return [replay(out) for out in pool.map(one, jobs)]


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
        self._sentence_cache = {}       # id(chunk) -> (sentences, vectors)
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

    def _split(self, chunk):
        sentences = []
        for part in _SENTENCE_SPLIT.split(chunk["text"]):
            part = _WS.sub(" ", part).strip()[:_EVIDENCE_SENTENCE_CHARS]
            if len(part.split()) >= 3 and part not in sentences:
                sentences.append(part)
        return sentences

    def _embed_sentences(self, chunks):
        pending = []
        seen = set()
        for chunk in chunks:
            if id(chunk) in self._sentence_cache:
                continue
            for sentence in self._split(chunk):
                if sentence not in seen:
                    pending.append(sentence)
                    seen.add(sentence)
        vectors_by_sentence = {}
        api_batches = 0
        if pending:
            for start in range(0, len(pending), 2048):
                batch = pending[start:start + 2048]
                vectors_by_sentence.update(dict(zip(batch, encode_texts(batch))))
                api_batches += 1
            for chunk in chunks:
                if id(chunk) in self._sentence_cache:
                    continue
                sentences = self._split(chunk)
                if sentences and all(sentence in vectors_by_sentence for sentence in sentences):
                    self._sentence_cache[id(chunk)] = (
                        sentences,
                        np.asarray([vectors_by_sentence[sentence] for sentence in sentences]),
                    )
        for chunk in chunks:
            self._sentence_cache.setdefault(id(chunk), (self._split(chunk), None))
        return len(pending), api_batches

    def prefetch_sentences(self, pairs):
        started = time.perf_counter()
        chunks = []
        seen = set()
        for a_sources, b_sources, a, b in pairs:
            a_vec, b_vec = self._name_vecs.get(a), self._name_vecs.get(b)
            if a_vec is None or b_vec is None:
                continue
            pair_vec = (np.asarray(a_vec) + np.asarray(b_vec)) / 2
            for sources in (a_sources, b_sources):
                for chunk in self._nearest_chunks(sources, pair_vec, _EVIDENCE_CHUNKS_PER_SIDE):
                    if id(chunk) not in seen:
                        seen.add(id(chunk))
                        chunks.append(chunk)
        unique_sentences, api_batches = self._embed_sentences(chunks)
        logger.info(
            "fusion_sentence_prefetch chunks=%d unique_sentences=%d api_batches=%d elapsed_ms=%.0f",
            len(chunks), unique_sentences, api_batches,
            (time.perf_counter() - started) * 1000,
        )

    def _nearest_chunks(self, sources, query_vec, k):
        chunks, vectors = [], []
        for doc in sorted(sources):
            entry = self._by_doc.get(doc)
            if entry is not None:
                chunks.extend(entry[0])
                vectors.append(entry[1])
        if not chunks:
            return []
        sims = cosine_similarity(query_vec.reshape(1, -1), np.vstack(vectors))[0]
        order = sorted(range(len(chunks)), key=lambda i: (-sims[i], i))
        return [chunks[i] for i in order[:k]]

    def _sentences(self, chunk):
        if id(chunk) not in self._sentence_cache:
            self._embed_sentences([chunk])
        return self._sentence_cache[id(chunk)]

    def pair_sentences(self, a_sources, b_sources, a, b, k=_EVIDENCE_SENTENCES_PER_PAIR):
        """The k sentences closest to both names from the chunks nearest the pair; a sentence
        naming a concept ranks above a merely similar one."""
        a_vec, b_vec = self._name_vecs.get(a), self._name_vecs.get(b)
        if a_vec is None or b_vec is None:
            return []
        pair_vec = (np.asarray(a_vec) + np.asarray(b_vec)) / 2
        chunks, seen = [], set()
        for sources in (a_sources, b_sources):
            for ch in self._nearest_chunks(sources, pair_vec, _EVIDENCE_CHUNKS_PER_SIDE):
                if id(ch) not in seen:
                    seen.add(id(ch))
                    chunks.append(ch)
        if not chunks:
            self.missing += 1
            if not self._missing_logged:
                self._missing_logged = True
                self._log("[fusion] no embedded chunks for node sources")
            return []

        scored = {}
        for ch in chunks:
            sentences, vectors = self._sentences(ch)
            if not sentences:
                continue
            sims = cosine_similarity(np.vstack([a_vec, b_vec]), vectors)
            for i, sentence in enumerate(sentences):
                pa, pb = _name_positions(a, b, sentence)
                score = (float(sims[0, i]) + float(sims[1, i])) / 2
                score += 0.5 * (pa is not None) + 0.5 * (pb is not None)
                if score > scored.get(sentence, float("-inf")):
                    scored[sentence] = score
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
        return [sentence for sentence, _score in ranked[:k]]


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


def merge_graph(engine, client, log=print):
    workers = fusion_workers()
    logger.info(
        "fusion_concurrency workers=%d global_slots=%d verdict_batch=%d max_singles=%d "
        "input_tpm=%d output_tpm=%d",
        workers, _FUSION_GLOBAL_SLOTS, _VERDICT_PAIR_BATCH, _VERDICT_MAX_SINGLES,
        graph_limiter.input_limit, graph_limiter.output_limit,
    )
    run = FusionLog(
        model=_MODEL,
        prompt_versions={"step3": _STEP3_PROMPT_VERSION, "step5": _STEP5_PROMPT_VERSION},
        settings={"auto_merge": AUTO_MERGE_SIM, "possible_synonym": POSSIBLE_SYNONYM_SIM,
                  "batch": _VERDICT_PAIR_BATCH, "neighbours": _NEIGHBOURS_PER_NODE,
                  "workers": workers, "global_slots": _FUSION_GLOBAL_SLOTS},
        documents=sorted(getattr(engine, "uploads_by_week", {}).items()),
    )

    nodes = _collect_nodes(engine)
    if not nodes:
        log("Fusion: no concepts")
        run.result["outcome"] = "no concepts to fuse"
        run.write(FUSION_LOG_DIR)
        return {}, []

    step_times = {}
    clock = time.perf_counter()

    def mark(step):
        nonlocal clock
        now = time.perf_counter()
        step_times[step] = now - clock
        clock = now

    unique_names = sorted(nodes.keys())
    log(f"Fusion step 1: {len(engine.concepts)} mentions -> {len(unique_names)} unique")
    run.mentions, run.unique = len(engine.concepts), len(unique_names)
    run.variants = [
        (norm, sorted(rec["variants"]))
        for norm, rec in sorted(nodes.items())
        if rec["variants"] != {norm}
    ]

    dsu = _DSU(unique_names)
    llm_candidates = []
    auto_merged = blocked = 0
    # Abbreviations the documents define for any node name; steps 2, 3b and the check share them.
    aliases = _source_aliases(_engine_texts(engine), unique_names)
    if len(unique_names) >= 2:
        name_embeddings = np.asarray(encode_texts(unique_names))
        sim_matrix = cosine_similarity(name_embeddings)
        index = {n: i for i, n in enumerate(unique_names)}
        # No similarity floor: only pairs that pass the name check can merge, at any similarity.
        check_started = time.perf_counter()
        passed = _variant_pairs(unique_names, aliases)
        run.check_ms = (time.perf_counter() - check_started) * 1000
        run.pairs_checked = len(unique_names) * (len(unique_names) - 1) // 2
        run.pairs_passed = len(passed)
        for a, b in passed:
            sim = float(sim_matrix[index[a], index[b]])
            if _is_negation_variant(a, b):
                blocked += 1
                run.blocked.append((a, b, sim))
            elif sim >= AUTO_MERGE_SIM and not _is_modifier_variant(a, b):
                dsu.union(a, b)
                auto_merged += 1
                run.auto_merged.append((a, b, sim))
            else:
                if sim >= AUTO_MERGE_SIM:
                    run.modifier_held.append((a, b, sim))
                llm_candidates.append((a, b, sim))
        passed_set = set(passed)
        for i, j in np.argwhere(np.triu(sim_matrix, 1) >= POSSIBLE_SYNONYM_SIM):
            a, b = unique_names[i], unique_names[j]
            if (a, b) not in passed_set and not _is_negation_variant(a, b):
                run.possible_synonyms.append((a, b, float(sim_matrix[i, j])))
    kept = [(a, b, s) for (a, b, s) in llm_candidates if dsu.find(a) != dsu.find(b)]
    run.already_linked = [c for c in llm_candidates if c not in kept]
    llm_candidates = kept
    run.candidates = list(llm_candidates)
    log(f"Fusion step 2: {run.pairs_checked} pairs checked, {run.pairs_passed} passed, "
        f"{auto_merged} auto-merged, {blocked} blocked, {len(llm_candidates)} for LLM, "
        f"{len(run.possible_synonyms)} possible synonym(s), check {run.check_ms:.1f} ms")
    logger.info("fusion_step2_check names=%d pairs_checked=%d pairs_passed=%d auto_merged=%d "
                "sent=%d possible_synonyms=%d check_ms=%.1f",
                len(unique_names), run.pairs_checked, run.pairs_passed, auto_merged,
                len(llm_candidates), len(run.possible_synonyms), run.check_ms)
    mark("1-2")

    interim_cmap = _build_canonical_map(dsu, nodes, aliases)

    adjacency = {}
    for e in engine.edges:
        f = interim_cmap.get(normalize_concept(e["from"]), normalize_concept(e["from"]))
        t = interim_cmap.get(normalize_concept(e["to"]), normalize_concept(e["to"]))
        if not f or not t or f == t:
            continue
        adjacency.setdefault(f, []).append((t, e["relation"]))
        adjacency.setdefault(t, []).append((f, e["relation"]))

    step3_new_edges = []
    if llm_candidates:
        ask_names = sorted({n for (a, b, _s) in llm_candidates for n in (a, b)})
        name_vecs = {nm: np.asarray(vec) for nm, vec in zip(ask_names, encode_texts(ask_names))}

        merges, step3_new_edges = _llm_evaluate_pairs(
            llm_candidates, interim_cmap, adjacency, nodes, name_vecs, engine, client, log, run)
        for (a, b) in merges:
            dsu.union(a, b)
    mark("3")

    split_out = _verify_clusters(dsu, nodes, log, run, aliases)
    mark("3b")

    # The label of each merged group is chosen by _canonical_rank, not by the LLM.
    final_cmap = _build_canonical_map(dsu, nodes, aliases, split_out)

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

    def _add_edge(frm, to, relation, discovered_by, doc=None, summary="", context="",
                  llm_sentence=False):
        nonlocal skipped
        relation = _clean_relation(relation)
        f, t = _final_canon(frm), _final_canon(to)
        if not f or not t or f == t:
            skipped += 1
            # Both ends can be distinct raw names that a merge put on the same
            # node, so name the node rather than just calling it a self-loop.
            detail = f"{frm} -[{relation}]-> {to}"
            if f and f == t:
                detail += f"   (both ends are now {f})"
            run.removals.append(("merged onto itself" if f == t else "empty node", detail))
            return
        if f not in final_nodes or t not in final_nodes:
            skipped += 1
            run.removals.append(("unknown node", f"{f} -[{relation}]-> {t}"))
            return
        key = (f, t, relation)
        rec = edge_groups.get(key)
        if rec is not None:
            run.removals.append(("exact duplicate",
                                 f"{f} -[{relation}]-> {t}   (now {rec['evidence'] + 1} pieces of evidence)"))
        if rec is None:
            rec = {"from": f, "to": t, "relation": relation, "sources": set(),
                   "evidence": 0, "discovered_by": discovered_by,
                   "summary": summary or "", "context": context or "",
                   "llm_sentence": False}
            edge_groups[key] = rec
        else:
            # The verdict's sentence was checked against both names, so it
            # replaces an extraction summary rather than waiting behind it.
            if summary and (llm_sentence or not rec.get("summary")):
                rec["summary"] = summary
            if not rec.get("context") and context:
                rec["context"] = context
        rec["evidence"] += 1
        rec["llm_sentence"] = rec["llm_sentence"] or llm_sentence
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
                  summary=e.get("summary", ""), context=e.get("context", ""),
                  llm_sentence=e.get("llm_sentence", False))

    input_edges = len(engine.edges) + len(step3_new_edges)
    exact_count = len(edge_groups)

    pair_best = {}
    for rec in edge_groups.values():
        pkey = (rec["from"], rec["to"])
        cur = pair_best.get(pkey)
        if cur is None:
            pair_best[pkey] = rec
        elif _edge_rank(rec) > _edge_rank(cur):
            pair_best[pkey] = rec
            run.removals.append(("same pair",
                                 f"{cur['from']} -[{cur['relation']}]-> {cur['to']}"
                                 f"   lost to  -[{rec['relation']}]->"))
        else:
            run.removals.append(("same pair",
                                 f"{rec['from']} -[{rec['relation']}]-> {rec['to']}"
                                 f"   lost to  -[{cur['relation']}]->"))
    pair_count = len(pair_best)

    edge_list = _reconcile_opposite(list(pair_best.values()), run)

    for rec in edge_list:
        if not rec["sources"]:
            rec["sources"] = set(final_nodes[rec["from"]]["sources"]) | \
                             set(final_nodes[rec["to"]]["sources"])

    run.edges_in = input_edges
    run.edges_in_detail = (f"  ({len(engine.edges)} from extraction"
                           f" + {len(step3_new_edges)} from step 3)")
    log(f"Fusion step 4: {input_edges} -> {len(edge_list)} edges "
        f"(exact -{(input_edges - skipped) - exact_count}, "
        f"pair -{exact_count - pair_count}, opposite -{pair_count - len(edge_list)})")
    mark("4")

    for fe in _bridge_fusion(final_nodes, edge_list, engine, client, log, run):
        key = (fe["from"], fe["to"], fe["relation"])
        if key in edge_groups:
            continue
        src = set(final_nodes[fe["from"]]["sources"]) | set(final_nodes[fe["to"]]["sources"])
        rec = {"from": fe["from"], "to": fe["to"], "relation": fe["relation"],
               "sources": src, "evidence": 1, "discovered_by": "bridge_fusion",
               "summary": fe.get("summary", ""), "context": fe.get("context", "")}
        edge_groups[key] = rec
        edge_list.append(rec)
    mark("5")
    logger.info("fusion_step_times %s", step_times)
    failure_totals = {
        key: sum(counts.get(key, 0) for counts in run.call_failures.values())
        for key in ("defaults_to_unrelated", "json_validate_failed", "rate_limited")
    }
    limiter_wait_seconds = sum(
        counts.get("limiter_wait_seconds", 0.0) for counts in run.call_failures.values()
    )
    logger.info(
        "fusion_failures defaults_to_unrelated=%d json_validate_failed=%d "
        "rate_limited=%d limiter_wait_seconds=%.3f",
        failure_totals["defaults_to_unrelated"], failure_totals["json_validate_failed"],
        failure_totals["rate_limited"], limiter_wait_seconds,
    )

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

    run.edges_out = len(fused_edges)
    run.result = {
        "canonical nodes": len(final_nodes),
        "fused edges": len(fused_edges),
        "by discovery": ", ".join(f"{k} {v}" for k, v in sorted(by_discovery.items())),
        "name variants mapped": len(canonical_nodes),
        "wall time per step": ", ".join(f"{k} {v:.2f}s" for k, v in step_times.items()),
    }
    written = run.write(FUSION_LOG_DIR)
    if written:
        log(f"Fusion log: {written}")
    return canonical_nodes, fused_edges


def _canonical_rank(name, nodes, expand):
    """Sort key for a merged group's label, best lowest: a full form (no period-initial, no
    document-defined abbreviation word, no trailing acronym), then most mentions, longest,
    alphabetical."""
    text = normalize_concept(name)
    words = text.split()
    m = _TRAILING_PAREN.match(text)
    abbreviated = (any(_INITIAL.fullmatch(w) or w in expand for w in words)
                   or bool(m and _is_acronym(m.group(2), m.group(1).split())))
    return (abbreviated, -nodes[name]["mentions"], -len(name), name)


def _build_canonical_map(dsu, nodes, aliases=None, split_out=None):
    expand = _alias_expansions(aliases or {})
    split = split_out or set()
    cmap = {}
    for members in dsu.groups().values():
        active_split = split & set(members)
        kept = [m for m in members if m not in active_split]
        if not kept:
            active_split, kept = set(), members
        best = min(kept, key=lambda m: _canonical_rank(m, nodes, expand))
        for m in members:
            cmap[m] = m if m in active_split else best
    return cmap


def _summary_order(rec):
    """+1 when the edge's summary names `from` before `to`, -1 when it names
    them the other way round, 0 when it does not name both."""
    direction = _sentence_direction(rec["from"], rec["to"], rec.get("summary") or "")
    return {"A_to_B": 1, "B_to_A": -1}.get(direction, 0)


def _edge_rank(rec):
    """Order for edges over one pair, best highest: generic labels lose, then sentence-backed
    verdicts win, then summary order, then evidence count."""
    return (0 if _is_generic_relation(rec["relation"]) else 1,
            1 if rec.get("llm_sentence") else 0,
            _summary_order(rec),
            rec["evidence"],
            1 if rec["discovered_by"] == "extraction" else 0,
            -len(rec["relation"]),
            rec["relation"])


def _label_key(rel):
    return _WS.sub(" ", _clean_relation(rel).lower().replace("_", " ").replace("-", " ")).strip()


# Taxonomy labels. 'is a/an ... of' counts only with a noun on this list, so 'is a cause of' is not
# taxonomy.
_TAXONOMY_NOUNS = r"(?:type|kind|form|subtype|special case|instance|example|part|member|component|subset)s?"
_TAXONOMY_OF = re.compile(r"^(?:(?:is|are|was|were) )?(?:(?:an?|the) )?(?:\w+ )?" + _TAXONOMY_NOUNS + r" of\b")
_BROADER_VERB = re.compile(r"^(?:generali|subsum|underl|encompass|includ|contain|compris)")
_NARROWER_VERB = re.compile(r"^speciali")
# Part-whole and membership phrasings with no "is a ... of". The whole comes
# first in "has part", "consists of" and "made up of", the part in the rest.
_WHOLE_FIRST = re.compile(r"\b(?:has part|consists? of|made up of)\b")
_PART_WHOLE = re.compile(r"\b(?:part of|component of|belongs? to|member of|subset of|has part|"
                         r"consists? of|made up of|example of|instance of)\b")
# Labels that read the same both ways round.
_SYMMETRIC = re.compile(r"\b(?:contrast|differ|opposite|oppose[sd]? to|complement|reverse|inverse|"
                        r"equivalent|equal|same as|compar|alternative|versus|vs\b|all(?:y|ied|ies)\b|"
                        r"rival|substitut|trades? off|bind|interact|compet|correlat)")
# Directed labels that are not taxonomy: cause, dependency, effect, order.
_DIRECTED = re.compile(r"\b(?:caus|lead|led\b|result|produc|enabl|allow|prevent|requir|depend|rel(?:y|ies) on|"
                       r"use[sd]?\b|increas|decreas|rais|lower|reduc|affect|influenc|determin|regulat|"
                       r"inhibit|driv|explain|prerequisite|preced|follow|trigger|improv|support|deriv|"
                       r"defin|measur|generat|creat|shift|maximi|minimi|impl|appl|extend|build|based on|"
                       r"motivat|introduc|replac|evolv|transform|convert|stud|model|occur|prov|solv|"
                       r"activat|encod|cataly|transcri|secret|synthes|stimulat|overthr|succeed|conquer|"
                       r"rule[sd]?\b|found(?:ed|s)\b|sign(?:s|ed)?\b)")


def _taxonomy_kind(key):
    """'broader_first' ('A includes B'), 'narrower_first' ('A is a type of B'), or None for a
    non-taxonomy label."""
    if key in _HIER_BROADER_FIRST or _BROADER_VERB.search(key) or _WHOLE_FIRST.search(key):
        return "broader_first"
    if (key in _HIER_NARROWER_FIRST or _NARROWER_VERB.search(key)
            or _TAXONOMY_OF.search(key) or _PART_WHOLE.search(key)):
        return "narrower_first"
    return None


def _is_taxonomy(key):
    return _taxonomy_kind(key) is not None


def _label_class(rel):
    """'hierarchical', 'symmetric', 'directed', or None for a label none of
    these recognise. Used only to reconcile edges running both ways."""
    key = _label_key(rel)
    if not key:
        return None
    if _is_taxonomy(key):
        return "hierarchical"
    if _is_generic_relation(key) or _SYMMETRIC.search(key):
        return "symmetric"
    if _DIRECTED.search(key):
        return "directed"
    return None


def _reconcile_opposite(records, run=None):
    """One edge each way: with a taxonomy label it is a contradiction (best-ranked kept);
    symmetric labels are kept once; anything else is a loop and both stay."""
    by_pair = {}
    for rec in records:
        by_pair.setdefault(tuple(sorted((rec["from"], rec["to"]))), []).append(rec)
    kept = []
    for recs in by_pair.values():
        if len(recs) == 1:
            kept.append(recs[0])
            continue
        recs = sorted(recs, key=_edge_rank, reverse=True)
        classes = [_label_class(r["relation"]) for r in recs]
        best, other = recs
        if "hierarchical" in classes or classes == ["symmetric", "symmetric"]:
            kind = "hierarchy conflict" if "hierarchical" in classes else "symmetric duplicate"
            kept.append(best)
            if run is not None:
                run.removals.append((kind, f"{other['from']} -[{other['relation']}]-> {other['to']}"
                                           f"   lost to  {best['from']} -[{best['relation']}]-> {best['to']}"))
            continue
        kept.extend(recs)
        if run is not None and None in classes:
            run.kept_unclassified.append(
                (f"{best['from']} -[{best['relation']}]-> {best['to']}",
                 f"{other['from']} -[{other['relation']}]-> {other['to']}"))
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


# Merge-only: step 3 decides whether two names are one concept and adds no edges.
_STEP3_SYSTEM = (
    "Decide whether two concept names refer to the SAME concept. "
    "Judge PRIMARILY from the evidence sentences; use neighbourhood as secondary context. "
    "Never judge from name similarity alone. Related but distinct concepts are 'different'.\n"
    "SAME RULES:\n"
    "- NOT same if the evidence contrasts or distinguishes A and B with each other "
    "(e.g. 'A, unlike B', 'A whereas B', 'A while B' used as a contrast). A contrast of A or B "
    "with some third concept does not count, nor does 'while' meaning 'at the same time as'.\n"
    "- If modifier_variant is true, one name is the other plus extra words, which usually "
    "narrow it. Answer same only if the evidence uses the two names for one and the same thing.\n"
    'Return: {"id": int, "verdict": "same"|"different"}. '
    'Only JSON: {"results": [...]}'
)


def _llm_evaluate_pairs(candidates, interim_cmap, adjacency, nodes, name_vecs, engine, client, log, run):
    cache = _load_verdict_cache()
    resolved, to_ask = {}, []
    for (a, b, sim) in candidates:
        rec = cache.get(_pair_key(_STEP3_PROMPT_VERSION, a, b))
        if isinstance(rec, dict):
            resolved[(a, b)] = _from_cache_record(a, b, rec)
        else:
            to_ask.append((a, b, sim))
    from_cache = {(a, b) for (a, b) in resolved}
    calls = -(-len(to_ask) // _VERDICT_PAIR_BATCH)
    run.step3_calls = calls
    log(f"Fusion step 3: {len(resolved)} cache hits, {len(to_ask)} pair(s) "
        f"to ask in {calls} call(s)")

    # Built once: it stacks every document's chunk-embedding matrix, and that
    # does not change between batches.
    evidence = _EvidencePool(engine, log, name_vecs)
    evidence.prefetch_sentences([
        (nodes[a]["sources"], nodes[b]["sources"], a, b)
        for a, b, _sim in to_ask
    ])

    batches = [to_ask[i:i + _VERDICT_PAIR_BATCH] for i in range(0, len(to_ask), _VERDICT_PAIR_BATCH)]
    jobs = []
    for sub in batches:
        payload = []
        for idx, (a, b, sim) in enumerate(sub):
            item = {"id": idx, "a": a, "b": b, "similarity": round(sim, 3),
                    "a_sources": sorted(nodes[a]["sources"]),
                    "b_sources": sorted(nodes[b]["sources"]),
                    "a_neighbours": _neighbours(a, interim_cmap, adjacency, _NEIGHBOURS_PER_NODE),
                    "b_neighbours": _neighbours(b, interim_cmap, adjacency, _NEIGHBOURS_PER_NODE)}
            if _is_modifier_variant(a, b):
                item["modifier_variant"] = True
            ev = evidence.pair_sentences(nodes[a]["sources"], nodes[b]["sources"], a, b)
            if ev:
                item["evidence"] = ev
            payload.append(item)
        jobs.append((payload, _STEP3_MAX_TOKENS))
    answers = _run_verdict_calls(client, _STEP3_SYSTEM, jobs, log, "Fusion step 3:",
                                 run.call_failures["step3"], run.settings.get("workers", 1))

    cache_dirty = False
    for sub, results in zip(batches, answers):
        by_id = _results_by_id(results)
        for idx, (a, b, sim) in enumerate(sub):
            r = by_id.get(idx)
            if r is None:
                resolved[(a, b)] = {"verdict": "different"}
                continue
            # Only the verdict is kept; the merged node's label is chosen without the LLM.
            rec = {"verdict": _to_cache_record(a, b, r)["verdict"]}
            resolved[(a, b)] = rec
            # Only an answer we can read is kept: a missing or unknown verdict
            # would otherwise stay "different" for good. It is asked again next time.
            if _normalise_verdict(r.get("verdict")) is not None:
                cache[_pair_key(_STEP3_PROMPT_VERSION, a, b)] = rec
                cache_dirty = True
    if cache_dirty:
        _save_verdict_cache(cache)

    # Every candidate already passed the name check in step 2, so "same" is the only gate left.
    merges = []
    n_same = n_different = 0
    for (a, b, sim) in candidates:
        r = resolved.get((a, b), {})
        source = "cache" if (a, b) in from_cache else "call"
        if _verdict_or_unrelated(r.get("verdict"), log, "Fusion step 3:", a, b) != "same":
            run.verdicts.append((source, a, b, sim, "different", ""))
            n_different += 1
            continue
        run.verdicts.append((source, a, b, sim, "same",
                             "[modifier variant]" if _is_modifier_variant(a, b) else ""))
        merges.append((a, b))
        n_same += 1

    run.verdict_totals = {"same (merged)": n_same, "different": n_different}
    log(f"Fusion step 3: {len(candidates)} -> merged {n_same} / different {n_different}")
    logger.info("fusion_step3_verdicts pairs=%d merges=%d different=%d",
                len(candidates), n_same, n_different)
    # Merge-only: step 3 adds no edges.
    return merges, []


def _verify_clusters(dsu, nodes, log, run, aliases=None):
    """Step 3b without the LLM: in each merged group of 3+ names, a name that is not a
    morphological variant of the group's canonical name is split back out. aliases are the
    document abbreviations step 2 found for every node name."""
    expand = _alias_expansions(aliases or {})
    clusters = [sorted(m) for m in dsu.groups().values() if len(m) >= 3]
    log(f"Fusion step 3b: {len(clusters)} cluster(s) to re-verify")
    if not clusters:
        return set()

    split_out, failed_pairs = set(), 0
    for members in clusters:
        failed_pairs += sum(1 for a, b in combinations(members, 2)
                            if not _is_morphological_variant(a, b, aliases))
        # Canonical picked as _build_canonical_map picks it.
        canon = min(members, key=lambda m: _canonical_rank(m, nodes, expand))
        so = [m for m in members if m != canon and not _is_morphological_variant(m, canon, aliases)]
        run.clusters.append((list(members), so, "check"))
        split_out.update(so)
    log(f"Fusion step 3b: checked {len(clusters)} group(s), {failed_pairs} pair(s) failed, "
        f"split out {len(split_out)} name(s)")
    logger.info("fusion_step3b_check groups=%d failed_pairs=%d names_split=%d",
                len(clusters), failed_pairs, len(split_out))
    return split_out


_STEP5_SYSTEM = (
    "Two concepts from different documents share neighbours. Using source text, decide if "
    "a meaningful academic relationship connects them. Label must be grounded in text — "
    "if text doesn't support a specific relationship, answer 'unrelated'. Shared graph "
    "structure alone is NOT sufficient.\n"
    "RELATION LABEL RULES: 1-3 words, ACTIVE voice verb phrase; NOT passive; "
    "NOT containing either name; NOT vague ('related to', 'similar to'). If HIERARCHICAL, "
    'return "broader": "A"|"B".\n'
    "'sentence': one short sentence (max 12 words) stating the relation, subject first, "
    "using both names exactly as given.\n"
    'Return: {"id": int, "verdict": "related"|"unrelated", '
    '"relation": phrase (if related), "sentence": text (if related), '
    '"direction": "A_to_B"|"B_to_A" (if related), '
    '"broader": "A"|"B" (if hierarchical)}. '
    'Only JSON: {"results": [...]}'
)


def _bridge_fusion(final_nodes, edge_list, engine, client, log, run):
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

    run.hubs = sorted(hubs)
    run.bridge_candidates = [(a, b, list(common)) for (a, b, common) in candidates]
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
    from_cache = {(a, b) for (a, b) in resolved}
    run.bridge_calls = -(-len(to_ask) // _VERDICT_PAIR_BATCH)
    log(f"Fusion step 5: {len(resolved)} cache hits, {len(to_ask)} LLM calls")

    ask_names = sorted({n for (a, b, _c) in to_ask for n in (a, b)})
    name_vecs = {nm: np.asarray(vec) for nm, vec in zip(ask_names, encode_texts(ask_names))} if ask_names else {}
    evidence = _EvidencePool(engine, log, name_vecs)
    evidence.prefetch_sentences([
        (final_nodes[a]["sources"], final_nodes[b]["sources"], a, b)
        for a, b, _common in to_ask
    ])

    batches = [to_ask[i:i + _VERDICT_PAIR_BATCH] for i in range(0, len(to_ask), _VERDICT_PAIR_BATCH)]
    jobs = []
    for sub in batches:
        payload = []
        for idx, (a, b, common) in enumerate(sub):
            item = {"id": idx, "a": a, "b": b,
                    "a_sources": sorted(final_nodes[a]["sources"]),
                    "b_sources": sorted(final_nodes[b]["sources"]),
                    "shared_neighbours": common}
            ev = evidence.pair_sentences(final_nodes[a]["sources"], final_nodes[b]["sources"], a, b)
            if ev:
                item["evidence"] = ev
            payload.append(item)
        jobs.append((payload, _VERDICT_MAX_TOKENS))
    answers = _run_verdict_calls(client, _STEP5_SYSTEM, jobs, log, "Fusion step 5:",
                                 run.call_failures["step5"], run.settings.get("workers", 1))

    cache_dirty = False
    for sub, results in zip(batches, answers):
        by_id = _results_by_id(results)
        for idx, (a, b, common) in enumerate(sub):
            r = by_id.get(idx)
            if r is None:
                resolved[(a, b)] = {"verdict": "unrelated"}
                continue
            rec = _to_cache_record(a, b, r)
            resolved[(a, b)] = _from_cache_record(a, b, rec)
            if _normalise_verdict(r.get("verdict")) is not None:
                cache[_pair_key(_STEP5_PROMPT_VERSION, a, b)] = rec
                cache_dirty = True
    if cache_dirty:
        _save_verdict_cache(cache)

    fusion_edges = []
    aliases = _source_aliases(_engine_texts(engine), sorted({n for (a, b, _c) in candidates for n in (a, b)}))
    for (a, b, common) in candidates:
        r = resolved.get((a, b), {})
        verdict = _verdict_or_unrelated(r.get("verdict"), log, "Fusion step 5:", a, b)
        source = "cache" if (a, b) in from_cache else "call"
        if verdict != "related":
            run.bridge_verdicts.append((source, a, b, verdict, ""))
            continue
        direction, from_sentence = _verdict_direction(a, b, r)
        frm, to, relation, conflict = _orient(
            a, b, r.get("relation") or "", direction, r.get("broader") or "")
        if conflict and from_sentence:
            run.bridge_verdicts.append((source, a, b, "dropped", "broader contradicts sentence"))
            continue
        if not _named_both(a, b, r.get("sentence") or "", aliases):
            run.bridge_verdicts.append((source, a, b, "dropped",
                                        f'sentence does not name both: "{r.get("sentence") or ""}"'))
            continue
        if (relation and not _is_vague_relation(relation)
                and not _label_names_concept(relation, a) and not _label_names_concept(relation, b)):
            fusion_edges.append({"from": frm, "to": to, "relation": relation,
                                 "summary": r.get("sentence") or ""})
            run.bridge_verdicts.append(
                (source, a, b, verdict,
                 f'"{relation}" ({"from sentence" if from_sentence else "direction field"})'))
            run.bridge_edges.append((frm, to, relation))
        else:
            run.bridge_verdicts.append(
                (source, a, b, "rejected", f'label {relation!r} failed the label rules'))
    log(f"Fusion step 5: {len(candidates)} -> {len(fusion_edges)} fusion edge(s)")
    return fusion_edges
