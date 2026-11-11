# 0005. Schema subset boundary for plugin configuration

Date: 2026-11-11
Status: accepted

## Context

Plugin Configs are written with the schema library (`Schema`). A second,
separate validation library is used elsewhere in the upstream project for
RPC, protocol, and draft validation — never in a plugin Config. The earlier
Phase 1 scope text listed constructs from that second library
(`literal` / `enum` / `tuple` / `intersect` / `record` / `date`,
refinements, discriminated unions) as schema targets. Verified by grep
across the upstream project: none of those appear in a plugin Config.
Porting them would have spent the deepest port in the phase on constructs
nothing uses, while missing the ones every Config needs.

The Config surface actually used, ranked by frequency: `object`, `number`,
`string`, `.default`, `.min` / `.max`, `.step`, `natural`, `.required`,
`.volatile`, `union`, `boolean`, `array`, `const`, `dict`, `.pattern`,
`.role`, `transform`, `never`, `.loose`, `.extra`, and `toJSON` round-trip.
The real gaps against anima's `Schema` are: no `~standard` seam, no
`Volatile`, no `natural` / `step`, no `pattern` / `role`, and object/union
error semantics that silently differ.

## Decision

Port the Config-used subset above (chunk P1-3). Explicitly exclude:
`intersect` / `tuple` / `date` / `lazy` / `is` / `function` / `regExp` /
`arrayBuffer` / `bitset` / `percent` / `simplify` / `i18n` / `autofix` /
`Schema.extend` / `Schema.from` / dict key-schema / UI-only metadata
(`disabled` / `comment` / `link` / `deprecated` / `experimental` /
`collapse`) — all zero-use in plugin Configs. The RPC/protocol validation
library is a separate workstream, not part of the Config port, and is named
as such rather than silently dropped. Any unported construct fails loud at
schema-definition time, never silently passes validation.

## Consequences

- Chunk P1-3 is scoped to the ranked list above, with a conformance test
  per construct.
- Under-scoping here silently breaks every plugin Config downstream; the
  fail-loud rule is the mitigation.
- The excluded constructs stay excluded until a Config is found that needs
  one; that is a new decision, not an accidental gap.
