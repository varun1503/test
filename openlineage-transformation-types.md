# OpenLineage transformation vocabulary (for the `transformation` field)

Model each hop on the OpenLineage ColumnLineageDatasetFacet.

## type
- DIRECT   — the value of the target is derived from the value of the source.
- INDIRECT — the source influences the target without its value flowing in
             (a condition, filter, join key, grouping, ordering).

## subtype
DIRECT:
- IDENTITY        — value copied/passed through unchanged (getter, assignment, body()).
- TRANSFORMATION  — value computed/formatted/parsed (concat, cast, map, arithmetic).
- AGGREGATION     — value produced by an aggregate (sum, count, max) over the source.

INDIRECT:
- JOIN        — source used as a join key.
- GROUP_BY    — source used to group.
- FILTER      — source used in a filter/where.
- SORT        — source used to order.
- WINDOW      — source used in a window partition/order.
- CONDITIONAL — source gates the target via a guard/if/validation.

## masking (boolean)
Set true when the step strips, redacts, removes, hashes, or otherwise obscures the data
(e.g. `requestHeaders.remove("AUTHORIZATION")`). This is orthogonal to type/subtype.

Note: the older `transformationType` / `transformationDescription` fields are deprecated
in favor of the `transformations` array; validate against the current 1-2-0 spec.
