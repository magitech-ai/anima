# 0006. Expression evaluation scope for configuration values

Date: 2026-11-11
Status: accepted

## Context

Configuration values may be expressions (`!!js` tag in YAML, stored as
`{__jsExpr: ...}`), evaluated in loader context. Upstream evaluates them
with full JavaScript `eval`. Python's standard library has no JavaScript
`eval`, so full parity is impossible without a JavaScript engine.

The attested patterns in upstream configurations, verified by grep, are
narrow: `process.env.*` (including `JSON.parse(process.env...)` and
`Number(process.env...)`), `ctx.*` (`ctx.headlessStartup.*`,
`ctx.webStartup.host ?? ...`), and `String(...)` call shapes, plus
comparisons, ternaries, and boolean operators around them.

## Decision

Port a restricted evaluator covering the attested patterns only: environment
access, loader-context access, comparisons / ternaries / boolean operators,
and the attested call shapes. Anything outside the attested set fails loud
at load time instead of being silently evaluated or silently skipped. No
JavaScript engine dependency in Phase 1. The `!!js` YAML tag and its
round-trip on dump are part of the same chunk (P1-5).

## Consequences

- Chunk P1-5 is bounded; the evaluator cannot balloon into a language
  implementation.
- A configuration that needs a fuller language is a new decision (a JS
  engine dependency, with its own security and packaging questions), not
  something the restricted evaluator grows into silently.
- Expressions outside the attested set are rejected at load, so a missing
  construct surfaces immediately instead of misconfiguring a plugin.
