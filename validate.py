"""
validate.py — Grounding gate.

The single most important defense against hallucinated lineage: every triplet must
cite a code_ref whose line actually contains the claimed symbol, and both endpoints
must correspond to a real discovered data element. Anything that fails is flagged
(grounded=False) so the pipeline/HITL can drop or review it.
"""
from __future__ import annotations

import os

from .parser import ParsedFile
from .schema import Triplet


def build_symbol_index(files: list[ParsedFile]):
    """(symbol_names, source_lines_by_file) for O(1) grounding checks."""
    names: set[str] = set()
    lines_by_file: dict[str, list[str]] = {}
    for pf in files:
        for de in pf.data_elements:
            names.add(de.name)
            names.add(de.name.split(".")[-1])   # payload_key leaf
        if os.path.exists(pf.path):
            with open(pf.path, "r", encoding="utf8", errors="replace") as fh:
                lines_by_file[pf.path] = fh.read().splitlines()
    return names, lines_by_file


def validate_triplet(t: Triplet, names: set[str], lines_by_file: dict[str, list[str]]) -> tuple[bool, list[str]]:
    problems: list[str] = []

    # endpoints must be known data elements (leaf name match)
    for role, ep in (("source", t.source), ("target", t.target)):
        leaf = ep.field.split(".")[-1]
        if leaf not in names:
            problems.append(f"{role} field '{ep.field}' not found in symbol table")

    # every code_ref must point at a line that actually contains the symbol
    if not t.transformation.code_refs:
        problems.append("no code_refs on transformation")
    for cr in t.transformation.code_refs:
        src_lines = lines_by_file.get(cr.file)
        if src_lines is None:
            problems.append(f"code_ref file not parsed: {cr.file}")
            continue
        if not (1 <= cr.start_line <= len(src_lines)):
            problems.append(f"code_ref line {cr.start_line} out of range in {cr.file}")
            continue
        window = " ".join(src_lines[cr.start_line - 1: max(cr.start_line, cr.end_line)])
        token = cr.symbol.split(".")[-1].split("(")[0]
        if token and token not in window:
            problems.append(f"symbol '{cr.symbol}' not on lines {cr.start_line}-{cr.end_line} of {cr.file}")

    ok = len(problems) == 0
    return ok, problems


def validate_batch(triplets: list[Triplet], files: list[ParsedFile]):
    names, lines_by_file = build_symbol_index(files)
    grounded, rejected = [], []
    for t in triplets:
        ok, problems = validate_triplet(t, names, lines_by_file)
        t.grounded = ok
        if ok:
            grounded.append(t)
        else:
            rejected.append((t, problems))
    return grounded, rejected
