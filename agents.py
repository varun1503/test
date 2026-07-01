"""
agents.py — LangGraph orchestration.

Wraps the same discover/relate/assemble/validate/suggest steps as a StateGraph with an
orchestrator-worker shape:

    discover -> relate -> assemble -> validate -> (HITL gate) -> suggest -> END

- assemble uses the injected Namer (RuleNamer or LLMNamer).
- validate grounds every triplet; ungrounded ones are separated out.
- an optional HITL interrupt lets a human approve low-confidence / llm-inferred edges
  before they enter the authoritative lineage (durable via a checkpointer).

Runs fully with RuleNamer (no API key). Swap in LLMNamer for the production semantic layer.
"""
from __future__ import annotations

from typing import Annotated, TypedDict, Optional
import operator

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt

from .parser import ParsedFile, Hop
from .namers import Namer, RuleNamer
from . import pipeline
from .validate import validate_batch
from .schema import Triplet, Suggestion


class LineageState(TypedDict, total=False):
    folder: str
    dataset_ns: str
    files: list[ParsedFile]
    hops_by_file: dict
    bus_edges: list[dict]
    raw_triplets: list[Triplet]
    triplets: Annotated[list[Triplet], operator.add]   # grounded, appended by workers
    rejected: list
    suggestions: list[Suggestion]
    require_human_approval: bool


def make_graph(namer: Optional[Namer] = None, checkpointer=None):
    namer = namer or RuleNamer()

    def discover_node(state: LineageState) -> dict:
        return {"files": pipeline.discover(state["folder"])}

    def relate_node(state: LineageState) -> dict:
        hops_by_file, bus_edges = pipeline.relate(state["files"])
        return {"hops_by_file": hops_by_file, "bus_edges": bus_edges}

    def assemble_node(state: LineageState) -> dict:
        raw = pipeline.assemble(state["files"], state["hops_by_file"],
                                state["bus_edges"], namer, state["dataset_ns"])
        return {"raw_triplets": raw}

    def validate_node(state: LineageState) -> dict:
        grounded, rejected = validate_batch(state["raw_triplets"], state["files"])
        return {"triplets": grounded, "rejected": rejected}

    def hitl_node(state: LineageState) -> dict:
        if not state.get("require_human_approval"):
            return {}
        low_conf = [t for t in state["triplets"]
                    if t.provenance == "llm-inferred" or t.confidence in ("low", "medium")]
        if not low_conf:
            return {}
        decision = interrupt({
            "message": "Approve low-confidence / inferred lineage edges?",
            "edges": [t.line() for t in low_conf],
        })
        if decision == "reject_inferred":
            keep = [t for t in state["triplets"] if t not in low_conf]
            return {"triplets": keep}
        return {}

    def suggest_node(state: LineageState) -> dict:
        return {"suggestions": pipeline.suggest(state["files"], state["triplets"])}

    g = StateGraph(LineageState)
    g.add_node("discover", discover_node)
    g.add_node("relate", relate_node)
    g.add_node("assemble", assemble_node)
    g.add_node("validate", validate_node)
    g.add_node("hitl", hitl_node)
    g.add_node("suggest", suggest_node)

    g.add_edge(START, "discover")
    g.add_edge("discover", "relate")
    g.add_edge("relate", "assemble")
    g.add_edge("assemble", "validate")
    g.add_edge("validate", "hitl")
    g.add_edge("hitl", "suggest")
    g.add_edge("suggest", END)

    return g.compile(checkpointer=checkpointer or InMemorySaver())


def run_graph(folder: str, dataset_ns: str = "one-data-global-merchant-setup-kyc",
              namer: Optional[Namer] = None, require_human_approval: bool = False,
              thread_id: str = "lineage-1") -> LineageState:
    app = make_graph(namer)
    cfg = {"configurable": {"thread_id": thread_id}}
    return app.invoke({
        "folder": folder, "dataset_ns": dataset_ns,
        "require_human_approval": require_human_approval,
    }, cfg)
