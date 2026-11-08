# Superseded content-token comparison

The v0.9.0 trace and result files in this directory are preserved as historical
evidence. Their reported 10,532-token / 48.6% "formatting" reduction used a control
that duplicated the same data in both MCP fields. It is **not a valid
single-representation content-formatting comparison and is withdrawn**.

An independent recount of the old trace gives 20,768 rendered-content tokens
versus 20,768 compact-JSON content tokens across all 30 tool replies: zero content
reduction. For the 18 measured writer calls, both counts are 10,495. The original
`audit.json` confirmed its then-defined double-representation calculation; it
does not validate the withdrawn content-savings interpretation.

The integration assertions remain historical evidence of that run. Do not combine
its token aggregates with corrected measurement-version-2 values. See the
[corrected report](../../RECURRING_WORKFLOW.md) and
[new evidence](../2026-11-08-recurring-content-v2/results.json).
