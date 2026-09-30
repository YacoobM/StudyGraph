"""Per-run record of what fusion actually did.

The console line for each step says how many pairs it handled; this says which
ones, and why each edge that disappeared disappeared. One file per run, named
by timestamp, so a demo run is not buried under the development ones.

Written by merge_graph at the end of every fusion. Nothing reads it back — it
exists to be read by a person afterwards, which is why it is text and not JSON.
"""

from datetime import datetime
from itertools import count

# Distinguishes two runs started in the same millisecond of one process.
_RUN_COUNTER = count(1)


def _pair(a, b, sim=None):
    head = f"{sim:.2f}  " if sim is not None else ""
    return f"{head}{a}  |  {b}"


class FusionLog:
    """Collects one run's detail. Every field starts empty, so a step that did
    not run reports that it did not rather than being left out."""

    def __init__(self, model, prompt_versions, settings, documents):
        self.started = datetime.now()
        self.model = model
        self.prompt_versions = prompt_versions   # {"step3": ..., "step5": ..., "cluster": ...}
        self.settings = settings                 # {"auto_merge": ..., "llm_low": ..., ...}
        self.documents = documents               # [(week, filename)]

        self.mentions = 0
        self.unique = 0
        self.variants = []           # (canonical, [raw variants])
        self.auto_merged = []        # (a, b, sim)
        self.blocked = []            # (a, b, sim)
        self.modifier_held = []      # (a, b, sim) — above auto-merge, asked instead
        self.candidates = []         # (a, b, sim)
        self.already_linked = []     # (a, b, sim) — dropped before asking
        self.verdicts = []           # (source, a, b, sim, verdict, detail)
        self.verdict_totals = {}
        self.step3_calls = 0
        self.clusters = []           # (members, split_out, source)
        self.cluster_calls = 0
        self.edges_in = 0
        self.edges_in_detail = ""
        self.edges_out = 0
        self.removals = []           # (kind, description)
        self.kept_unclassified = []  # (edge, reverse edge) kept both ways, labels unclassified
        self.hubs = []
        self.bridge_candidates = []  # (a, b, [shared neighbours])
        self.bridge_verdicts = []    # (source, a, b, verdict, detail)
        self.bridge_edges = []       # (frm, to, relation)
        self.bridge_calls = 0
        self.call_failures = {"step3": {}, "step3b": {}, "step5": {}}   # step -> {reason: count}
        self.result = {}

    # -- sections -----------------------------------------------------------

    def _header(self, out):
        out += [
            "StudyGraph fusion run",
            "=" * 68,
            f"timestamp        {self.started.isoformat(timespec='seconds')}",
            f"model            {self.model}",
            "prompt versions  " + "  ".join(f"{k}={v}" for k, v in self.prompt_versions.items()),
            "settings         " + "  ".join(f"{k}={v}" for k, v in self.settings.items()),
        ]
        if self.documents:
            out.append(f"documents        {len(self.documents)}")
            for week, name in self.documents:
                out.append(f"                   {week}  {name}")
        else:
            out.append("documents        (none recorded)")
        out.append("")

    def _step1(self, out):
        out += [
            "step 1  collect nodes",
            f"  {self.mentions} mentions -> {self.unique} unique names",
        ]
        if self.variants:
            out.append(f"  {len(self.variants)} name(s) absorbed a variant:")
            for canon, variants in self.variants:
                out.append(f"    {canon}")
                for v in variants:
                    out.append(f"        <- {v}")
        else:
            out.append("  every name already matched its normalised form")
        out.append("")

    def _step2(self, out):
        out += [
            "step 2  similarity screen",
            f"  auto-merged at or above {self.settings.get('auto_merge')}: {len(self.auto_merged)}",
        ]
        out += [f"    {_pair(a, b, s)}" for a, b, s in self.auto_merged]
        out.append(f"  blocked as negation variants: {len(self.blocked)}")
        out += [f"    {_pair(a, b, s)}" for a, b, s in self.blocked]
        if self.modifier_held:
            out.append(f"  held back from auto-merge as modifier variants: {len(self.modifier_held)}")
            out += [f"    {_pair(a, b, s)}" for a, b, s in self.modifier_held]
        if self.already_linked:
            out.append(f"  already grouped by an auto-merge, not asked: {len(self.already_linked)}")
            out += [f"    {_pair(a, b, s)}" for a, b, s in self.already_linked]
        out.append(f"  sent to the LLM: {len(self.candidates)}")
        out += [f"    {_pair(a, b, s)}" for a, b, s in self.candidates]
        out.append("")

    def _step3(self, out):
        out.append("step 3  pair verdicts")
        if not self.verdicts:
            out += ["  no pair reached the LLM", ""]
            return
        cached = sum(1 for v in self.verdicts if v[0] == "cache")
        out.append(
            f"  {len(self.verdicts)} pair(s): {cached} from cache, "
            f"{len(self.verdicts) - cached} from {self.step3_calls} call(s)"
        )
        for source, a, b, sim, verdict, detail in self.verdicts:
            line = f"    {source:<6} {verdict:<9} {_pair(a, b, sim)}"
            if detail:
                line += f"   -> {detail}"
            out.append(line)
        out += ["  totals: " + ", ".join(f"{k} {v}" for k, v in self.verdict_totals.items()), ""]

    def _step3b(self, out):
        out.append("step 3b cluster re-verification")
        if not self.clusters:
            out += ["  no group reached 3 names, so nothing to re-verify", ""]
            return
        out.append(f"  {len(self.clusters)} cluster(s), {self.cluster_calls} call(s)")
        for members, split_out, source in self.clusters:
            out.append(f"    {source:<6} {', '.join(members)}")
            out.append(
                f"           split out: {', '.join(split_out)}" if split_out
                else "           kept together"
            )
        out.append("")

    def _step4(self, out):
        out += [
            "step 4  edge reduction",
            f"  {self.edges_in} edges in{self.edges_in_detail}",
        ]
        if self.removals:
            out.append(f"  {len(self.removals)} removal(s):")
            width = max(len(kind) for kind, _ in self.removals)
            for kind, detail in self.removals:
                out.append(f"    {kind:<{width}}  {detail}")
        else:
            out.append("  nothing removed")
        if self.kept_unclassified:
            out.append(f"  {len(self.kept_unclassified)} pair(s) kept both ways only because "
                       f"their labels are unclassified:")
            for edge, reverse in self.kept_unclassified:
                out.append(f"    {edge}   and   {reverse}")
        out += [f"  {self.edges_out} edges out", ""]

    def _step5(self, out):
        out.append("step 5  bridge fusion")
        out.append(f"  {len(self.hubs)} hub(s)" + (f": {', '.join(self.hubs)}" if self.hubs else ""))
        if not self.bridge_candidates:
            out += ["  no pair shared enough non-hub neighbours to be a candidate", ""]
            return
        out.append(f"  {len(self.bridge_candidates)} candidate(s), {self.bridge_calls} call(s)")
        by_pair = {(a, b): (src, verdict, det) for src, a, b, verdict, det in self.bridge_verdicts}
        for a, b, shared in self.bridge_candidates:
            src, verdict, det = by_pair.get((a, b), ("-", "not asked", ""))
            line = f"    {src:<6} {verdict:<9} {_pair(a, b)}   shared: {', '.join(shared)}"
            if det:
                line += f"   -> {det}"
            out.append(line)
        out.append(f"  {len(self.bridge_edges)} bridge edge(s) created:")
        out += [f"    {frm} -[{rel}]-> {to}" for frm, to, rel in self.bridge_edges]
        out.append("")

    # -- output -------------------------------------------------------------

    def render(self):
        out = []
        for section in (self._header, self._step1, self._step2,
                        self._step3, self._step3b, self._step4, self._step5):
            section(out)
        out.append("result")
        for key, value in self.result.items():
            out.append(f"  {key}: {value}")
        failed = {step: counts.get("json_validate_failed", 0)
                  for step, counts in self.call_failures.items()}
        out.append(f"  json_validate_failed retries: {sum(failed.values())}  ("
                   + ", ".join(f"{step} {n}" for step, n in failed.items()) + ")")
        out.append(f"  elapsed: {(datetime.now() - self.started).total_seconds():.1f}s")
        return "\n".join(out) + "\n"

    def write(self, directory):
        """Write this run to its own file. Never raises — a log that cannot be
        written, or a bug in rendering it, must not fail the fusion that
        produced it. The name carries milliseconds and a per-process counter,
        and the file is opened exclusively, so two runs never share a file."""
        try:
            text = self.render()
        except Exception as error:
            text = ("StudyGraph fusion run\n"
                    f"log could not be rendered: {type(error).__name__}: {error}\n")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = self.started.strftime("%Y%m%d_%H%M%S_") + f"{self.started.microsecond // 1000:03d}"
            while True:
                path = directory / f"fusion_{stamp}_{next(_RUN_COUNTER)}.log"
                try:
                    with open(path, "x", encoding="utf-8") as handle:
                        handle.write(text)
                    return str(path)
                except FileExistsError:
                    continue
        except Exception as error:
            print(f"Fusion log could not be written: {error}")
            return None
