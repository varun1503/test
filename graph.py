"""
graph.py — Structure layer.

Builds two graphs:
  * code graph      : classes / methods / data-elements + declares/reads/writes/calls
  * lineage graph   : data-elements as nodes, validated triplets as edges (flows_to)

Also joins Vert.x EventBus producers to consumers by resolved address string — the
one edge type static call-graph analysis cannot see, surfaced here for the LLM/graph
to stitch cross-file lineage.
"""
from __future__ import annotations

import networkx as nx

from .parser import ParsedFile, Hop, dataflow_hops
from .schema import Triplet


def build_code_graph(files: list[ParsedFile]) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    for pf in files:
        for cls in pf.classes:
            g.add_node(f"class:{cls}", kind="class", file=pf.path)
        for de in pf.data_elements:
            nid = f"var:{de.enclosing_class}.{de.enclosing_method}.{de.name}"
            g.add_node(nid, kind="data_element", name=de.name, dtype=de.java_type,
                       de_kind=de.kind, file=pf.path,
                       line=de.decl.start_line if de.decl else None)
            if de.enclosing_class:
                g.add_edge(f"class:{de.enclosing_class}", nid, rel="declares")
            for r in de.reads:
                g.add_edge(nid, f"loc:{pf.path}:{r.start_line}", rel="read")
            for w in de.writes:
                g.add_edge(f"loc:{pf.path}:{w.start_line}", nid, rel="write")
        for c in pf.calls:
            g.add_edge(f"m:{c.caller}", f"m:?.{c.callee}", rel="calls", line=c.ref.start_line)
    return g


def bus_join(files: list[ParsedFile]) -> list[dict]:
    """Match producers (send/request/publish addr) to consumers (consumer addr).
    Returns candidate cross-file edges the LLM should confirm (provenance=llm-inferred)."""
    producers, consumers = {}, {}
    for pf in files:
        for be in pf.bus_edges:
            if not be.address or be.address.startswith("$"):
                continue
            bucket = consumers if be.kind == "consumer" else producers
            bucket.setdefault(be.address, []).append((pf, be))
    edges = []
    for addr, cons in consumers.items():
        for pf_c, be_c in cons:
            for pf_p, be_p in producers.get(addr, []):
                edges.append({
                    "address": addr,
                    "producer": f"{be_p.enclosing_class}.{be_p.enclosing_method}",
                    "producer_ref": be_p.ref,
                    "consumer": f"{be_c.enclosing_class}.{be_c.enclosing_method}",
                    "consumer_ref": be_c.ref,
                })
    return edges


def candidate_hops(files: list[ParsedFile]) -> list[Hop]:
    """All grounded intra-method def-use hops across the folder — the units the LLM
    names into transformations. The LLM may not add endpoints outside this set
    (except LLM-inferred bus edges), which is what keeps it from hallucinating."""
    out: list[Hop] = []
    for pf in files:
        out.extend(dataflow_hops(pf.path))
    return out


# ---------- lineage graph + multi-hop traversal --------------------------------

def build_lineage_graph(triplets: list[Triplet]) -> nx.DiGraph:
    g = nx.DiGraph()
    for t in triplets:
        s, d = t.source.qualified, t.target.qualified
        g.add_node(s); g.add_node(d)
        g.add_edge(s, d,
                   transformation=t.transformation.description,
                   type=t.transformation.type.value,
                   subtype=t.transformation.subtype.value,
                   masking=t.transformation.masking,
                   confidence=t.confidence,
                   provenance=t.provenance)
    return g


def trace_forward(g: nx.DiGraph, node: str) -> list[list[str]]:
    """All downstream lineage paths from a data element (impact analysis)."""
    paths = []
    def dfs(cur, acc):
        succ = list(g.successors(cur))
        if not succ:
            paths.append(acc)
            return
        for n in succ:
            if n in acc:      # cycle guard
                paths.append(acc + [n]); continue
            dfs(n, acc + [n])
    dfs(node, [node])
    return paths


def trace_backward(g: nx.DiGraph, node: str) -> list[list[str]]:
    """All upstream lineage paths into a data element (root-cause analysis)."""
    rg = g.reverse(copy=True)
    return [list(reversed(p)) for p in trace_forward(rg, node)]


def chain_to_string(g: nx.DiGraph, path: list[str]) -> str:
    parts = [path[0]]
    for a, b in zip(path, path[1:]):
        e = g.get_edge_data(a, b)
        parts.append(f" --[{e['type']}/{e['subtype']}{'/mask' if e['masking'] else ''}]--> {b}")
    return "".join(parts)
