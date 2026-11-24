# 0017. Independence and 1.0 stability

Date: 2026-11-24
Status: accepted (user decision)

## Context

ADRs 0013–0016 took Anima to a completed conformance milestone against canonical
Cordis `v4.0.0-rc.13`, ratified the target, removed the DSH-fork extension set,
and fixed the remaining divergences. The independent completeness audit's final
verdict was COMPLETE. The open correctness PRs were triaged against current
`main` and landed (#98); the one rejected as a regression (#87) was closed with
reproduced evidence.

The user then decided that, with the paradigm complete, Anima is free to become
a fully independent project — not a perpetual port of upstream Cordis — and
that the next milestone is a stable `1.0.0` contract. The user also specified
that unpinning from upstream does not mean abandoning the research paper.

## Decision

- Anima is an **independent project**. Future work is judged against the
  paper's invariants (revertible effects, reactive coeffects, unified context,
  temporal and spatial composability) and Anima's own compatibility policy —
  not against upstream Cordis releases. No future upstream version is a target.
- The Cordis rc.13 parity map is a **completed historical conformance record**,
  not a forward-looking pin. No parity re-audit is required unless we choose to
  revisit it.
- Anima's version becomes **`1.0.0`** with the compatibility policy in
  `docs/COMPATIBILITY.md`: additive changes in `1.x`, breaking changes only in
  `2.0.0` with a deprecation period where feasible.
- Downstream constraint: `magitech-harness` moves from `anima>=0.3,<0.4` to
  `anima>=1,<2` in a coordinated PR.
- Provenance stays accurate: `NOTICE`, README "Lineage", and ADRs 0012–0016
  remain. Independence does not mean rewriting history.

## Consequences

- Contributors no longer need to ask "what would Cordis do?" for new work; they
  ask "does this preserve the paradigm and our published contract?".
- The kernel may evolve in `1.x` without losing downstream compatibility.
- A `PyPI` publisher can be added by the user once 1.0.0 is released.
- `PARITY.md` is read as evidence of the completed milestone, not as a live
  roadmap.
