# 0010. Internal symbol key reconciliation

Date: 2026-11-11
Status: accepted

## Context

Anima's internal protocol keys are plain strings (`"anima.*"`) — Python
has no equivalent of the upstream runtime's unforgeable symbols, so
strings are the only portable representation. The prefix gap is
deliberate, not accidental, but the key set has drifted from upstream in
both directions.

Anima is missing two keys upstream has: `config` and `resolveConfig`.
The reflect layer already treats both as reserved words, so the reflect
layer and the symbols dict disagree today. `Service.resolve_config`
exists, but its docstring cites the `resolveConfig` key while the code
keys it under `resolveIntercept`.

Anima adds five keys with no upstream counterpart: `caller`,
`decorator`, `root`, `mergeIntercept`, `resolveIntercept`. Verified by
grep: `caller`, `decorator`, and `root` are unused as symbol keys
anywhere in `src/`; `mergeIntercept` and `resolveIntercept` are the
intercept-chain machinery used by the service and context symbol tables.

## Decision

In chunk P1-2: add the `config` and `resolveConfig` keys;
`Service.resolve_config` moves under the `resolveConfig` key, matching
its own docstring; `mergeIntercept` and `resolveIntercept` stay as
anima-only intercept-chain machinery, documented as such;
`caller`, `decorator`, and `root` are deleted as unused keys.

## Consequences

- The reflect layer and the symbols dict agree on which names are
  special.
- The two kept extras are named as anima-only, so a future upstream diff
  does not mistake them for a missing port.
- No behavior changes in P1-0; this record is the decision P1-2
  implements.
