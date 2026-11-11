# 0009. Fiber state wire mapping

Date: 2026-11-11
Status: accepted

## Context

Anima's `FiberState` is a string enum with values `"PENDING"`,
`"LOADING"`, `"ACTIVE"`, `"UNLOADING"`, `"DISPOSED"`, `"FAILED"`, in
that order. Upstream's is a numeric const enum: PENDING = 0, LOADING = 1,
ACTIVE = 2, FAILED = 3, DISPOSED = 4, UNLOADING = 5. The names match;
the values and the ordinal order differ (FAILED / UNLOADING / DISPOSED
are permuted). Any numeric comparison, ordering, or serialized state
therefore diverges.

## Decision

Name-based wire mapping. State crosses any boundary — logs, serialized
sessions, cross-runtime comparison — by name, never by ordinal or
numeric value. Anima keeps its string enum; the mapping is:

| Name | Upstream numeric value |
| --- | --- |
| PENDING | 0 |
| LOADING | 1 |
| ACTIVE | 2 |
| FAILED | 3 |
| DISPOSED | 4 |
| UNLOADING | 5 |

No code compares states numerically or relies on their order.

## Consequences

- Serialized state stays comparable across runtimes by name; a numeric
  value on the wire is a bug, not a representation.
- Chunk P1-1 implements the epoch machine against names, not ordinals.
