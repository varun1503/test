"""
pipeline.py — plain orchestration (no framework dependency).

discover -> relate -> assemble -> validate -> suggest. Uses an injected Namer for the
semantic step so you can run fully deterministic (RuleNamer) or LLM-backed (LLMNamer).
agents.py wraps these same steps in a LangGraph StateGraph.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .parser import parse_folder, dataflow_hops, ParsedFile, Hop
from .graph import bus_join, build_lineage_graph, trace_forward, trace_backward, chain_to_string
from .namers import Namer, RuleNamer
from .validate import validate_batch
from .schema import Triplet, Suggestion, CodeRefModel


@dataclass
class LineageResult:
    files: list[ParsedFile]
    triplets: list[Triplet]
    rejected: list[tuple]
    suggestions: list[Suggestion]
    dataset_ns: str = ""


def discover(folder: str) -> list[ParsedFile]:
    return parse_folder(folder)


def relate(files: list[ParsedFile]) -> tuple[dict[str, list[Hop]], list[dict]]:
    hops_by_file = {pf.path: dataflow_hops(pf.path) for pf in files}
    return hops_by_file, bus_join(files)


def assemble(files, hops_by_file, bus_edges, namer: Namer, dataset_ns: str) -> list[Triplet]:
    out: list[Triplet] = []
    bus_by_file = {}
    for e in bus_edges:
        bus_by_file.setdefault(e["consumer_ref"].file, []).append(e)
    for pf in files:
        hops = hops_by_file.get(pf.path, [])
        out.extend(namer.name(pf, hops, dataset_ns, bus_by_file.get(pf.path, [])))
    return out


def suggest(files: list[ParsedFile], triplets: list[Triplet]) -> list[Suggestion]:
    """Deterministic first-pass suggestions. Swap/augment with an LLM suggestion worker
    (see agents.py) for semantic findings."""
    s: list[Suggestion] = []
    targets = {t.target.field.split(".")[-1] for t in triplets}

    for pf in files:
        for de in pf.data_elements:
            # security: auth/PII stripped -> confirm intent (positive control)
            for w in de.reads + de.writes:
                if "remove" in w.snippet and ("AUTHORIZATION" in w.snippet or "auth" in w.snippet.lower()):
                    s.append(Suggestion(
                        category="security", severity="info",
                        message=f"AUTHORIZATION stripped in {de.enclosing_class}.{de.enclosing_method} "
                                f"(masking hop). Confirm this is intended and that downstream SOR calls "
                                f"re-attach auth from a trusted source.",
                        code_refs=[CodeRefModel(file=w.file, start_line=w.start_line,
                                                end_line=w.end_line, symbol=w.symbol)]))
            # data quality: payload getter without an obvious null/blank guard
            if de.kind == "payload_key" and de.java_type == "string":
                leaf = de.name.split(".")[-1]
                guarded = any("isBlank" in r.snippet or "!= null" in r.snippet for r in de.reads)
                if not guarded:
                    s.append(Suggestion(
                        category="data_quality", severity="warning",
                        message=f"Payload field '{leaf}' read via getString without a visible "
                                f"null/blank guard in {de.enclosing_class}.{de.enclosing_method}.",
                        code_refs=[CodeRefModel(file=de.decl.file, start_line=de.decl.start_line,
                                                end_line=de.decl.end_line, symbol=de.decl.symbol)]
                                  if de.decl else []))
            # missing lineage: declared data element never appears as a lineage target/source
            if de.kind in ("field", "local") and de.name not in targets and de.reads == []:
                s.append(Suggestion(
                    category="missing_lineage", severity="info",
                    message=f"Data element '{de.name}' in {de.enclosing_class}.{de.enclosing_method} "
                            f"is declared but never read — possible dead field or missing lineage hop.",
                    code_refs=[CodeRefModel(file=de.decl.file, start_line=de.decl.start_line,
                                            end_line=de.decl.end_line, symbol=de.decl.symbol)]
                              if de.decl else []))
    return s


def run_pipeline(folder: str, dataset_ns: str = "one-data-global-merchant-setup-kyc",
                 namer: Namer | None = None) -> LineageResult:
    namer = namer or RuleNamer()
    files = discover(folder)
    hops_by_file, bus_edges = relate(files)
    raw = assemble(files, hops_by_file, bus_edges, namer, dataset_ns)
    grounded, rejected = validate_batch(raw, files)
    suggestions = suggest(files, grounded)
    return LineageResult(files=files, triplets=grounded, rejected=rejected,
                         suggestions=suggestions, dataset_ns=dataset_ns)
