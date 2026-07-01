"""
namers.py — the swappable semantic layer.

A "namer" turns grounded candidate hops into classified triplets. Two implementations:

  RuleNamer  — deterministic, no LLM. Lets the whole pipeline run and be tested with
               zero API cost, and serves as a strong baseline / fallback.
  LLMNamer   — production path. Sends the skill card (system) + grounded prompt (user)
               to a model with Pydantic structured output. Never invents endpoints.

Both return list[Triplet]; validate.py grounds the result either way.
"""
from __future__ import annotations

from typing import Protocol

from .parser import ParsedFile, Hop, CodeRef
from .schema import (Triplet, Endpoint, Transformation, TransformType,
                     TransformSubtype, CodeRefModel, TripletBatch)
from .prompts import load_skill_card, build_extraction_prompt


class Namer(Protocol):
    def name(self, pf: ParsedFile, hops: list[Hop], dataset_ns: str,
             bus_edges: list[dict]) -> list[Triplet]: ...


def _cr(ref: CodeRef) -> CodeRefModel:
    return CodeRefModel(file=ref.file, start_line=ref.start_line,
                        end_line=ref.end_line, symbol=ref.symbol, snippet=ref.snippet)


# ---------- deterministic baseline ---------------------------------------------

_MASK_METHODS = {"remove", "clear", "redact", "mask", "hash", "encrypt"}
_IDENTITY_METHODS = {"body", "headers", "getString", "getInteger", "getLong",
                     "getBoolean", "getDouble", "getFloat", "getJsonObject",
                     "getJsonArray", "getValue", "get", "mapFrom"}


class RuleNamer:
    def name(self, pf, hops, dataset_ns, bus_edges):
        triplets: list[Triplet] = []
        for h in hops:
            via = (h.via_method or "").strip()
            if via in _MASK_METHODS:
                ttype, sub, masking = TransformType.INDIRECT, TransformSubtype.CONDITIONAL, True
            elif via in _IDENTITY_METHODS:
                ttype, sub, masking = TransformType.DIRECT, TransformSubtype.IDENTITY, False
            else:
                ttype, sub, masking = TransformType.DIRECT, TransformSubtype.TRANSFORMATION, False
            desc = f"{h.enclosing_class}.{h.enclosing_method}(): {h.target} := " \
                   f"{via + '(' if via else ''}{', '.join(h.sources)}{')' if via else ''}"
            for s in h.sources:
                triplets.append(Triplet(
                    source=Endpoint(dataset=dataset_ns, field=s),
                    transformation=Transformation(
                        description=desc, type=ttype, subtype=sub, masking=masking,
                        code_refs=[_cr(h.ref)]),
                    target=Endpoint(dataset=dataset_ns, field=h.target),
                    confidence="high", provenance="parser",
                ))
        # EventBus joins -> inferred cross-file edges
        for e in bus_edges:
            triplets.append(Triplet(
                source=Endpoint(dataset=dataset_ns, field=f"{e['producer']}::{e['address']}"),
                transformation=Transformation(
                    description=f"EventBus '{e['address']}' delivers payload from {e['producer']} to {e['consumer']}",
                    type=TransformType.DIRECT, subtype=TransformSubtype.IDENTITY, masking=False,
                    code_refs=[_cr(e['producer_ref']), _cr(e['consumer_ref'])]),
                target=Endpoint(dataset=dataset_ns, field=f"{e['consumer']}::{e['address']}"),
                confidence="medium", provenance="llm-inferred",
            ))
        return triplets


# ---------- production LLM namer -----------------------------------------------

class LLMNamer:
    """Requires: pip install langchain-anthropic  (or any LangChain chat model).
    Pass a model id; the skill card is the system prompt, the grounded hops the user turn,
    and Pydantic structured output enforces the triplet schema."""

    def __init__(self, model: str = "claude-sonnet-4-5", temperature: float = 0.0,
                 chat_model=None):
        self._system = load_skill_card(include_references=True)
        if chat_model is not None:
            self._llm = chat_model
        else:
            from langchain_anthropic import ChatAnthropic
            self._llm = ChatAnthropic(model=model, temperature=temperature)
        self._structured = self._llm.with_structured_output(TripletBatch)

    def name(self, pf, hops, dataset_ns, bus_edges):
        if not hops and not bus_edges:
            return []
        user = build_extraction_prompt(pf, hops, dataset_ns, bus_edges)
        from langchain_core.messages import SystemMessage, HumanMessage
        result: TripletBatch = self._structured.invoke(
            [SystemMessage(content=self._system), HumanMessage(content=user)])
        return result.triplets
