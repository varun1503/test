# Java Data-Lineage Extractor (hybrid AST + LLM)

Traces **data-element variables ("PD variables")** through a folder of Java files and emits
**Source | Transformation | Target** triplets — the lineage rows in your `data_lineage_rules`
sheet — grounded in real `file:line` code references, with a reusable prompt **skill card**
the agent loads to stay consistent, plus **suggestions** (security / data-quality / missing lineage).

## Why hybrid (and not pure-LLM)
Deterministic parsing owns the **skeleton** it is not safe to let an LLM invent — the inventory
of data elements, def-use chains, the call graph, and Vert.x EventBus wiring. The LLM only
**names and classifies** the transformation at each grounded hop and bridges framework
indirection (EventBus address joins) it cannot resolve statically. This keeps recall on the
parser and precision on the LLM, and every triplet is validated against source before it is kept.
Pure-LLM-over-chunks is stochastically incomplete and far costlier for graph construction.

## Layout
```
src/
  parser.py     tree-sitter: data-element discovery, symbol table, read/write facts,
                def-use + mutation hops, call graph, Vert.x EventBus + constant resolution
  graph.py      NetworkX code graph + lineage graph, EventBus producer->consumer join,
                multi-hop forward/backward traversal (impact & root-cause)
  schema.py     Pydantic triplets aligned to OpenLineage ColumnLineageDatasetFacet
  namers.py     RuleNamer (deterministic, no key) | LLMNamer (structured-output production)
  validate.py   grounding gate: reject any triplet whose refs/endpoints aren't in source
  prompts.py    loads the skill card; builds grounded per-method extraction prompts
  pipeline.py   discover -> relate -> assemble -> validate -> suggest (plain functions)
  agents.py     the same steps as a LangGraph StateGraph (orchestrator-worker + HITL + checkpoint)
  run.py        CLI
skills/java-data-lineage-extractor/
  SKILL.md      the reusable prompt skill card (progressive disclosure)
  references/   OpenLineage vocab, Vert.x patterns + worked KYC example, grounding/eval
sample/         a KYC verticle mirroring your screenshot, for a self-test
```

## Install
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run (deterministic, no API key)
```bash
python -m src.run sample --graph
```
Reproduces the screenshot lineage, e.g. the multi-hop chain
`message → requestBody → applicationIdentifier` and the `AUTHORIZATION` masking edge,
plus grounded suggestions. Add `--json out.json` to export, `--hitl` to run through
LangGraph with human approval of inferred edges.

## Switch to the production LLM namer
```bash
pip install langchain-anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python -m src.run /path/to/java/folder --ns my-dataset --llm --model claude-sonnet-4-5 --graph
```
`LLMNamer` sends `SKILL.md` (+ references) as the system prompt and the grounded candidate
hops as the user turn, with Pydantic structured output enforcing the triplet schema. Output
still passes through `validate.py`, so ungrounded LLM triplets are dropped, not trusted.

## Programmatic use
```python
from src import run_pipeline, build_lineage_graph, trace_forward, chain_to_string
r = run_pipeline("path/to/folder", dataset_ns="my-dataset")     # RuleNamer by default
for t in r.triplets:
    print(t.line())
g = build_lineage_graph(r.triplets)
# impact analysis: what does applicationIdentifier flow into?
for p in trace_forward(g, "my-dataset.applicationIdentifier"):
    print(chain_to_string(g, p))
```
Swap the namer: `run_pipeline(folder, namer=LLMNamer())`.

## The skill card
`skills/java-data-lineage-extractor/SKILL.md` is the reusable prompt: role, when-to-use, the
triplet JSON schema, extraction rules, grounding requirements, self-check, and a worked
few-shot in `references/`. It follows Anthropic's Agent Skills format (always-loaded YAML
frontmatter + on-demand references) so it is portable and composable, and it is what
`LLMNamer` loads. Edit it to add frameworks (Spring DI, Kafka) — add a `references/*.md`
per framework and a rule pointing at it.

## Extending
- **Deeper dataflow:** the intra-procedural def-use here is heuristic. For heap-aware
  inter-procedural value-flow, add a Soot/WALA (or CodeQL taint) pass and feed its edges into
  `candidate_hops`. `graph.py`/`validate.py` are agnostic to where a grounded hop came from.
- **Cross-file constants:** in-file constant resolution is done; lift `constants` into a global
  map in `graph.py` to resolve `$CONST` bus addresses across files.
- **Production graph store:** replace NetworkX with Neo4j; the node/edge shape already mirrors
  the OpenLineage facet, so Cypher impact queries are a thin add.
- **Retrieval at scale:** AST-chunk with tree-sitter, embed, FAISS-index, and graph-expand the
  retrieved seed set before prompting (GraphRAG) for very large repos.

## Grounding & evaluation
A triplet is kept only if both endpoint leaf names exist in the symbol table and every
code_ref points at a line containing its symbol (`validate.py`). Track precision / recall / F1
against a hand-labeled gold set, plus hallucination rate (ungrounded fraction, target < 2%) as
a release gate. See `references/grounding-and-eval.md`.
```
```
