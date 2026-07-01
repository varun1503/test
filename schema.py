"""
schema.py — Output contracts.

The triplet schema mirrors the OpenLineage ColumnLineageDatasetFacet vocabulary
(DIRECT/INDIRECT type + subtype + masking) so lineage is interoperable with
DataHub/Marquez/Atlan. Every hop is grounded in >=1 CodeRefModel; validate.py
rejects any triplet whose grounding does not check out against parsed source.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Literal
from pydantic import BaseModel, Field


class TransformType(str, Enum):
    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"


class TransformSubtype(str, Enum):
    # DIRECT
    IDENTITY = "IDENTITY"
    TRANSFORMATION = "TRANSFORMATION"
    AGGREGATION = "AGGREGATION"
    # INDIRECT
    JOIN = "JOIN"
    GROUP_BY = "GROUP_BY"
    FILTER = "FILTER"
    SORT = "SORT"
    WINDOW = "WINDOW"
    CONDITIONAL = "CONDITIONAL"


class CodeRefModel(BaseModel):
    file: str
    start_line: int
    end_line: int
    symbol: str
    snippet: Optional[str] = None


class Endpoint(BaseModel):
    """A data element: dataset namespace + field name, e.g.
    dataset='one-data-global-merchant-setup-kyc.$requestBody', field='applicationIdentifier'."""
    dataset: str
    field: str

    @property
    def qualified(self) -> str:
        return f"{self.dataset}.{self.field}"


class Transformation(BaseModel):
    description: str = Field(..., description="Human-readable summary of what happens at this hop")
    type: TransformType
    subtype: TransformSubtype
    masking: bool = Field(False, description="True if the step masks/strips/redacts the data")
    code_refs: list[CodeRefModel] = Field(..., min_length=1)


class Triplet(BaseModel):
    """One lineage hop: Source | Transformation | Target."""
    source: Endpoint
    transformation: Transformation
    target: Endpoint
    confidence: Literal["high", "medium", "low"] = "high"
    provenance: Literal["parser", "llm-inferred"] = "parser"
    grounded: bool = Field(False, description="Set by validate.py after checking code_refs")

    def line(self) -> str:
        return f"{self.source.qualified}  ->  [{self.transformation.type}/{self.transformation.subtype}" \
               f"{'/mask' if self.transformation.masking else ''}] {self.transformation.description}  ->  {self.target.qualified}"


class TripletBatch(BaseModel):
    """What the LLM must return: a list of triplets, nothing else."""
    triplets: list[Triplet]


class Suggestion(BaseModel):
    category: Literal["data_quality", "missing_lineage", "security", "refactor"]
    severity: Literal["info", "warning", "critical"]
    message: str
    code_refs: list[CodeRefModel] = Field(default_factory=list)


class SuggestionBatch(BaseModel):
    suggestions: list[Suggestion]
