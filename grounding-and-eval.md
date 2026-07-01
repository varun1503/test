# Grounding and evaluation

## Grounding (enforced by validate.py)
A triplet is accepted only if:
- source.field and target.field leaf names exist in the parsed symbol table, AND
- every code_ref points to a real line that contains its `symbol`.
Ungrounded triplets are rejected (or routed to HITL), never silently kept.

## Metrics (against a hand-labeled gold triplet set)
- Precision / Recall / F1 at the triplet (edge) level.
- Path-level correctness: does the full multi-hop chain match gold.
- Coverage: fraction of discovered data elements appearing in >=1 triplet.
- Hallucination rate: fraction of emitted triplets with no valid grounding (target: <2%).

Expectation from the literature: the deterministic backbone carries recall; the LLM protects
precision. Track F1 and hallucination rate as regression gates between releases.
