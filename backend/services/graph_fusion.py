import json
import os
import re
import time
import unicodedata
from itertools import combinations
from pathlib import Path

import numpy as np
from groq import BadRequestError, RateLimitError
from sklearn.metrics.pairwise import cosine_similarity
from app.config import EXTRACTION_MODEL, FUSION_DIR
from services.fusion_log import FusionLog
from services.llm_gateway import estimate_tokens, limiter

_MODEL = EXTRACTION_MODEL

AUTO_MERGE_SIM = 0.95
LLM_LOW_SIM = 0.55
NEGATION_PREFIXES = ("un", "non", "in", "im", "dis", "ir", "a", "anti", "de", "mis")

_VERDICT_PAIR_BATCH = 4
# Resends of the same request after malformed JSON. At temperature 0 a resend mostly repeats
# itself, so one is enough; 429s count separately.
_VERDICT_MAX_RETRIES = 1
# Cap on pairs a failed batch may re-ask one at a time; each single costs a call.
_VERDICT_MAX_SINGLES = 4
_NEIGHBOURS_PER_NODE = 5
_EVIDENCE_CHUNKS_PER_NODE = 1
_EVIDENCE_SNIPPET_CHARS = 200
# Steps 3 and 5 send the few sentences nearest both names, not chunk openings.
_EVIDENCE_CHUNKS_PER_SIDE = 2
_EVIDENCE_SENTENCES_PER_PAIR = 3
_EVIDENCE_SENTENCE_CHARS = 240
_SUMMARY_SENTENCE_CHARS = 200

# Output cap per verdict call: a 4-pair batch measured 195-350 tokens, and two calls at 450 fit the
# 1,000-a-minute output limit.
_VERDICT_MAX_TOKENS = 450
# Characters of a response body logged when it parses but has the wrong shape.
_RAW_LOG_CHARS = 500
_CLUSTER_MAX_TOKENS_BASE = 150
_CLUSTER_MAX_TOKENS_PER = 60
_CLUSTER_BATCH = 4
# Retries after a 429, waiting what the server asks; fusion runs in the background, so waiting is
# free.
_RATE_LIMIT_RETRIES = 2

# Bumped for temperature 0 and an output cap; older verdicts were sampled at the default
# temperature.
_STEP3_PROMPT_VERSION = "step3-evidence-v6"
_STEP5_PROMPT_VERSION = "step5-evidence-v5"
_CLUSTER_PROMPT_VERSION = "cluster-verify-v2"

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
    """Whether a sentence names both concepts, by name or defined abbreviation. A containing
    name is blanked first."""
    aliases = aliases or {}
    text = normalize_concept(sentence)
    if not text:
        return False

    def patterns(name):
        found = [p for p in [_phrase_pattern(name)] if p is not None]
        found += [re.compile(r"(?<!\w)" + re.escape(al) + r"s?(?!\w)") for al in sorted(aliases.get(name, ()))]
        return found

    na, nb = normalize_concept(a), normalize_concept(b)
    longer, shorter = (a, b) if len(na) >= len(nb) else (b, a)
    long_patterns = patterns(longer)
    if not any(p.search(text) for p in long_patterns):
        return False
    masked = text
    for p in long_patterns:
        masked = p.sub(lambda m: " " * len(m.group()), masked)
    return any(p.search(masked) for p in patterns(shorter))


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
    while True:
        reason = None
        try:
            # Reserved from the budget extraction and evolution share, so
            # fusion no longer races them into Groq's per-minute limits.
            limiter.acquire_sync(estimate_tokens(system) + estimate_tokens(user), max_tokens)
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
                reasoning_effort="none",
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=max_tokens,
            )
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
        except RateLimitError as e:
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
        except BadRequestError as e:
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


def _verdict_call(client, system, payload, log, tag, failures=None, max_tokens=_VERDICT_MAX_TOKENS):
    results, reason = _verdict_attempt(client, system, payload, log, tag, failures, max_tokens)
    if results is not None:
        return results
    if len(payload) <= 1:
        log(f"{tag} defaults to unrelated (not cached)")
        return []

    # Splitting isolates a malformed pair; it cannot help a rate limit, so only malformed output is
    # split, up to _VERDICT_MAX_SINGLES.
    if reason is None or not str(reason).startswith(("malformed", "json_validate_failed")):
        log(f"{tag} not splitting after {reason} — {len(payload)} pair(s) default to unrelated")
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
        self._model = engine.model
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
        cached = self._sentence_cache.get(id(chunk))
        if cached is None:
            sentences = []
            for part in _SENTENCE_SPLIT.split(chunk["text"]):
                part = _WS.sub(" ", part).strip()[:_EVIDENCE_SENTENCE_CHARS]
                if len(part.split()) >= 3 and part not in sentences:
                    sentences.append(part)
            vectors = (np.asarray(self._model.encode(sentences, show_progress_bar=False))
                       if sentences else None)
            cached = (sentences, vectors)
            self._sentence_cache[id(chunk)] = cached
        return cached

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
    run = FusionLog(
        model=_MODEL,
        prompt_versions={"step3": _STEP3_PROMPT_VERSION, "step5": _STEP5_PROMPT_VERSION,
                         "cluster": _CLUSTER_PROMPT_VERSION},
        settings={"auto_merge": AUTO_MERGE_SIM, "llm_low": LLM_LOW_SIM,
                  "batch": _VERDICT_PAIR_BATCH, "neighbours": _NEIGHBOURS_PER_NODE},
        documents=sorted(getattr(engine, "uploads_by_week", {}).items()),
    )

    nodes = _collect_nodes(engine)
    if not nodes:
        log("Fusion: no concepts")
        run.result["outcome"] = "no concepts to fuse"
        run.write(FUSION_LOG_DIR)
        return {}, []

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
                    run.blocked.append((a, b, sim))
                    continue
                if sim >= AUTO_MERGE_SIM and _is_modifier_variant(a, b):
                    run.modifier_held.append((a, b, sim))
                    llm_candidates.append((a, b, sim))
                elif sim >= AUTO_MERGE_SIM:
                    dsu.union(a, b)
                    auto_merged += 1
                    run.auto_merged.append((a, b, sim))
                else:
                    llm_candidates.append((a, b, sim))
    kept = [(a, b, s) for (a, b, s) in llm_candidates if dsu.find(a) != dsu.find(b)]
    run.already_linked = [c for c in llm_candidates if c not in kept]
    llm_candidates = kept
    run.candidates = list(llm_candidates)
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
            llm_candidates, interim_cmap, adjacency, nodes, name_vecs, engine, client, log, run)
        for (a, b) in merges:
            dsu.union(a, b)

    split_out = _verify_clusters(dsu, nodes, engine, client, log, run)

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
    }
    written = run.write(FUSION_LOG_DIR)
    if written:
        log(f"Fusion log: {written}")
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


_STEP3_SYSTEM = (
    "Resolve whether two concept names are SAME, DIFFERENT BUT RELATED, or UNRELATED. "
    "Judge PRIMARILY from the evidence sentences; use neighbourhood as secondary context. "
    "Never judge from name similarity alone. Answer 'related' ONLY for meaningful academic relationships. "
    "If an evidence sentence directly states how A and B relate to each other, the verdict is 'related', "
    "not 'unrelated'. Two names merely appearing in the same sentence is not enough.\n"
    "SAME RULES:\n"
    "- NOT same if the evidence contrasts or distinguishes A and B with each other "
    "(e.g. 'A, unlike B', 'A whereas B', 'A while B' used as a contrast). A contrast of A or B "
    "with some third concept does not count, nor does 'while' meaning 'at the same time as'.\n"
    "- If modifier_variant is true, one name is the other plus extra words, which usually "
    "narrow it. Answer same only if the evidence uses the two names for one and the same thing.\n"
    "RELATION LABEL RULES (when 'related'):\n"
    "- 1-3 words, directed verb phrase in ACTIVE voice (e.g. 'is a type of', 'generalises'). "
    "NOT passive voice.\n"
    "- Must NOT contain either name, NOT a full sentence, NOT vague ('is related to', 'similar to').\n"
    "- If HIERARCHICAL (is a/type of/generalises/includes/underlies), return 'broader': 'A'|'B' "
    "naming the more general concept.\n"
    "- 'sentence': one short sentence (max 12 words) stating the relation, subject first, "
    "using both names exactly as given.\n"
    'Return: {"id": int, "verdict": "same"|"related"|"unrelated", '
    '"canonical": "A"|"B" (if same), "relation": phrase (if related), '
    '"sentence": text (if related), '
    '"direction": "A_to_B"|"B_to_A" (if related), "broader": "A"|"B" (if related + hierarchical)}. '
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
            if _is_modifier_variant(a, b):
                item["modifier_variant"] = True
            ev = evidence.pair_sentences(nodes[a]["sources"], nodes[b]["sources"], a, b)
            if ev:
                item["evidence"] = ev
            payload.append(item)

        by_id = _results_by_id(_verdict_call(client, _STEP3_SYSTEM, payload, log, "Fusion step 3:",
                                                  run.call_failures["step3"]))
        for idx, (a, b, sim) in enumerate(sub):
            r = by_id.get(idx)
            if r is None:
                resolved[(a, b)] = {"verdict": "unrelated"}
                continue
            rec = _to_cache_record(a, b, r)
            resolved[(a, b)] = _from_cache_record(a, b, rec)
            # Only an answer we can read is kept: a missing or unknown verdict
            # would otherwise stay "unrelated" for good. It is asked again next time.
            if _normalise_verdict(r.get("verdict")) is not None:
                cache[_pair_key(_STEP3_PROMPT_VERSION, a, b)] = rec
                cache_dirty = True
    if cache_dirty:
        _save_verdict_cache(cache)

    merges, new_edges, canonical_votes = [], [], {}
    n_same = n_related = n_unrelated = n_not_named = 0
    aliases = _source_aliases(_engine_texts(engine), sorted({n for (a, b, _s) in candidates for n in (a, b)}))
    for (a, b, sim) in candidates:
        r = resolved.get((a, b), {})
        verdict = _verdict_or_unrelated(r.get("verdict"), log, "Fusion step 3:", a, b)
        source = "cache" if (a, b) in from_cache else "call"
        detail = ""
        direction, from_sentence = _verdict_direction(a, b, r)
        if verdict == "same":
            detail = f"canonical {r.get('canonical') or '?'}"
        elif verdict == "related":
            detail = (f'"{_clean_relation(r.get("relation") or "")}" ({direction}, '
                      f'{"from sentence" if from_sentence else "direction field"})')
            if r.get("sentence"):
                detail += f'   "{r["sentence"]}"'
        if _is_modifier_variant(a, b):
            detail = (detail + "   [modifier variant]").strip()
        run.verdicts.append((source, a, b, sim, verdict, detail))
        if verdict == "same":
            merges.append((a, b))
            canonical = str(r.get("canonical", "")).strip().upper()
            if canonical in ("A", "B"):
                chosen = a if canonical == "A" else b
                canonical_votes[chosen] = canonical_votes.get(chosen, 0) + 1
            n_same += 1
        elif verdict == "related":
            frm, to, relation, conflict = _orient(
                a, b, r.get("relation") or "", direction, r.get("broader") or "")
            if conflict and from_sentence:
                # The model's broader field and its own sentence disagree on
                # which way the edge runs; neither can be trusted over the other.
                run.verdicts[-1] = (source, a, b, sim, "dropped",
                                    detail + "   [broader contradicts sentence]")
                n_unrelated += 1
                continue
            if not _named_both(a, b, r.get("sentence") or "", aliases):
                run.verdicts[-1] = (source, a, b, sim, "dropped",
                                    (detail + "   [sentence does not name both]").strip())
                n_not_named += 1
                continue
            if (not relation or _is_vague_relation(relation)
                    or _label_names_concept(relation, a) or _label_names_concept(relation, b)):
                n_unrelated += 1
                continue
            new_edges.append({"from": frm, "to": to, "relation": relation,
                              "summary": r.get("sentence") or "",
                              "llm_sentence": from_sentence})
            n_related += 1
        else:
            n_unrelated += 1

    run.verdict_totals = {"same": n_same, "related": n_related, "unrelated": n_unrelated,
                          "dropped (sentence does not name both)": n_not_named}
    log(f"Fusion step 3: {len(candidates)} -> same {n_same} / related {n_related} / unrelated {n_unrelated}"
        f" / not named {n_not_named}")
    return merges, new_edges, canonical_votes


_STEP3B_SYSTEM = (
    "Several concept NAMES merged into ONE node. Using source text, decide which "
    "names truly refer to the SAME concept and which are DIFFERENT (pulled in by mistake). "
    "Return names that do NOT belong in 'split_out'; if all are the same, return [].\n"
    'Return: {"id": int, "split_out": [name, ...]}. '
    'Only JSON: {"results": [...]}'
)


def _verify_clusters(dsu, nodes, engine, client, log, run):
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

        cache_dirty = False
        for start in range(0, len(to_ask), _CLUSTER_BATCH):
            batch = to_ask[start:start + _CLUSTER_BATCH]
            payload = []
            for (ci, members) in batch:
                entry = {"id": ci, "names": []}
                for m in members:
                    name_item = {"name": m}
                    snippets = evidence.snippets(nodes[m]["sources"], m)
                    if snippets:
                        name_item["evidence"] = snippets
                    entry["names"].append(name_item)
                payload.append(entry)

            run.cluster_calls += 1
            by_id = _results_by_id(_verdict_call(
                client, _STEP3B_SYSTEM, payload, log, "Fusion step 3b:", run.call_failures["step3b"],
                max_tokens=_CLUSTER_MAX_TOKENS_BASE + _CLUSTER_MAX_TOKENS_PER * len(batch)))
            for (ci, members) in batch:
                r = by_id.get(ci)
                if r is None or not isinstance(r.get("split_out"), list):
                    # No answer for this cluster: kept together this run, but
                    # not cached, so a failed call is not remembered as "same".
                    resolved[ci] = {"split_out": []}
                    continue
                rec = {"split_out": [m for m in r["split_out"] if m in members]}
                cache[_ckey(members)] = rec
                cache_dirty = True
                resolved[ci] = rec
        if cache_dirty:
            _save_verdict_cache(cache)

    asked = {ci for (ci, _m) in to_ask}
    split_out = set()
    for ci, members in enumerate(clusters):
        so = [m for m in ((resolved.get(ci) or {}).get("split_out") or []) if m in members]
        run.clusters.append((list(members), list(so), "call" if ci in asked else "cache"))
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
            ev = evidence.pair_sentences(final_nodes[a]["sources"], final_nodes[b]["sources"], a, b)
            if ev:
                item["evidence"] = ev
            payload.append(item)

        by_id = _results_by_id(_verdict_call(client, _STEP5_SYSTEM, payload, log, "Fusion step 5:",
                                                  run.call_failures["step5"]))
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
