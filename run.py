"""
run.py — CLI.

    python -m src.run <folder> [--ns DATASET] [--llm] [--model MODEL]
                      [--json out.json] [--graph] [--hitl]

Deterministic by default (RuleNamer, no API key). --llm switches to LLMNamer
(requires langchain-anthropic and ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import argparse
import json
import sys

from .pipeline import run_pipeline
from .namers import RuleNamer
from .graph import build_lineage_graph, trace_forward, trace_backward, chain_to_string


def _fmt_triplet_table(triplets) -> str:
    lines = ["", "ID  | Source | Transformation | Target",
             "----+--------+----------------+-------"]
    for i, t in enumerate(triplets, 1):
        tr = f"[{t.transformation.type.value}/{t.transformation.subtype.value}" \
             f"{'/mask' if t.transformation.masking else ''}] {t.transformation.description}"
        flag = "" if t.grounded else "  (UNGROUNDED)"
        star = " *" if t.provenance == "llm-inferred" else ""
        lines.append(f"L{i:<3}| {t.source.qualified}\n    | {tr}\n    | {t.target.qualified}{star}{flag}\n")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Java data-lineage extractor")
    ap.add_argument("folder")
    ap.add_argument("--ns", default="one-data-global-merchant-setup-kyc",
                    help="dataset namespace for endpoints")
    ap.add_argument("--llm", action="store_true", help="use LLMNamer (needs API key)")
    ap.add_argument("--model", default="claude-sonnet-4-5")
    ap.add_argument("--json", help="write full result to this JSON path")
    ap.add_argument("--graph", action="store_true", help="print multi-hop chains")
    ap.add_argument("--hitl", action="store_true",
                    help="run via LangGraph with human approval of inferred edges")
    args = ap.parse_args(argv)

    namer = None
    if args.llm:
        from .namers import LLMNamer
        namer = LLMNamer(model=args.model)

    if args.hitl:
        from .agents import run_graph
        state = run_graph(args.folder, args.ns, namer, require_human_approval=True)
        triplets, rejected, suggestions = (state["triplets"], state["rejected"],
                                           state["suggestions"])
        files = state["files"]
    else:
        r = run_pipeline(args.folder, args.ns, namer=namer or RuleNamer())
        triplets, rejected, suggestions, files = (r.triplets, r.rejected,
                                                  r.suggestions, r.files)

    n_elems = sum(len(pf.data_elements) for pf in files)
    print(f"Parsed {len(files)} file(s), discovered {n_elems} data elements.")
    print(f"Grounded triplets: {len(triplets)}   Rejected(ungrounded): {len(rejected)}")
    print(_fmt_triplet_table(triplets))
    print(f"(* = llm-inferred cross-file EventBus edge)\n")

    if args.graph:
        g = build_lineage_graph(triplets)
        print("=== Multi-hop lineage chains ===")
        roots = [n for n in g.nodes if g.in_degree(n) == 0]
        for root in roots:
            for path in trace_forward(g, root):
                if len(path) > 1:
                    print("  " + chain_to_string(g, path))
        print()

    if suggestions:
        print("=== Suggestions ===")
        for s in suggestions:
            loc = f" ({s.code_refs[0].file}:{s.code_refs[0].start_line})" if s.code_refs else ""
            print(f"  [{s.category}/{s.severity}] {s.message}{loc}")
        print()

    if rejected:
        print("=== Rejected (ungrounded) — review, do not trust ===")
        for t, probs in rejected:
            print(f"  X {t.source.qualified} -> {t.target.qualified}: {'; '.join(probs)}")

    if args.json:
        payload = {
            "dataset_ns": args.ns,
            "triplets": [t.model_dump(mode="json") for t in triplets],
            "suggestions": [s.model_dump(mode="json") for s in suggestions],
            "rejected": [{"triplet": t.model_dump(mode="json"), "problems": p}
                         for t, p in rejected],
        }
        with open(args.json, "w", encoding="utf8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"Wrote {args.json}")


if __name__ == "__main__":
    main(sys.argv[1:])
