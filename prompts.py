"""
prompts.py — Skill-card loading + grounded prompt assembly.

Loads the reusable skill card (SKILL.md + references) and builds the extraction prompt
for one method's worth of grounded candidate hops. The model receives ONLY grounded
facts (hops, symbol table, source lines) and is instructed to name/classify, not invent.
"""
from __future__ import annotations

import os

from .parser import ParsedFile, Hop

SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "skills", "java-data-lineage-extractor")


def load_skill_card(include_references: bool = True) -> str:
    parts = []
    skill_md = os.path.join(SKILL_DIR, "SKILL.md")
    with open(skill_md, encoding="utf8") as fh:
        parts.append(fh.read())
    if include_references:
        ref_dir = os.path.join(SKILL_DIR, "references")
        for name in sorted(os.listdir(ref_dir)):
            with open(os.path.join(ref_dir, name), encoding="utf8") as fh:
                parts.append(f"\n\n===== references/{name} =====\n{fh.read()}")
    return "\n".join(parts)


def symbol_table_block(pf: ParsedFile) -> str:
    rows = []
    for de in pf.data_elements:
        rows.append(f"- {de.name} : {de.java_type} [{de.kind}] "
                    f"in {de.enclosing_class}.{de.enclosing_method} (decl L{de.decl.start_line if de.decl else '?'})")
    return "\n".join(rows)


def hops_block(hops: list[Hop]) -> str:
    rows = []
    for h in hops:
        rows.append(
            f"- target={h.target}  via={h.via_method}  sources={h.sources}  "
            f"@ {h.enclosing_class}.{h.enclosing_method}  "
            f"code_ref={{file:'{h.ref.file}', start_line:{h.ref.start_line}, end_line:{h.ref.end_line}, symbol:'{h.ref.symbol}'}}"
        )
    return "\n".join(rows)


def bus_block(bus_edges: list[dict]) -> str:
    if not bus_edges:
        return "(none)"
    rows = []
    for e in bus_edges:
        rows.append(
            f"- address='{e['address']}'  producer={e['producer']} (L{e['producer_ref'].start_line})"
            f"  ->  consumer={e['consumer']} (L{e['consumer_ref'].start_line})"
        )
    return "\n".join(rows)


def build_extraction_prompt(pf: ParsedFile, hops: list[Hop], dataset_ns: str,
                            bus_edges: list[dict] | None = None) -> str:
    """User-turn content. The skill card is the system prompt."""
    return f"""Dataset namespace for endpoints: {dataset_ns}

## Symbol table (the ONLY valid endpoints)
{symbol_table_block(pf)}

## Grounded candidate hops (turn each into a triplet; do not add endpoints outside these)
{hops_block(hops)}

## EventBus edges (join producers->consumers; flag as llm-inferred, confidence<=medium)
{bus_block(bus_edges or [])}

Return ONLY the JSON object described in the skill card (a `triplets` array). For each hop
above, emit one triplet: source(s) -> transformation(type/subtype/masking + code_refs) -> target.
Chain multi-hop flows (e.g. message -> requestBody -> applicationIdentifier) by reusing the
intermediate element as the target of one triplet and the source of the next.
"""
