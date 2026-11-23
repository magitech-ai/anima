# 0012. Lineage disclosure

Date: 2026-11-22
Status: accepted

## Context

Anima is a Python implementation of the kernel paradigm formalized in
"A Programming Paradigm for Spatiotemporal Composability" (arXiv:2608.25512)
and realized in Cordis, using DeepSeek Harness as the conformance reference
(frozen at `fcc0ad46`, `dsh v0.2.2-rc.1`). This was documented in internal
ADRs and parity maps but deliberately kept out of user-facing docs and commit
messages: `agents-home/scripts/scrub_check.py` failed the local gate on any
provenance wording, and a history rewrite had been rehearsed but never pushed.

Both reference projects are MIT-licensed, and the design is published openly.
Attribution is therefore sufficient; reimplementation is legitimate. A conformance
map to a named reference is a correctness asset, not an embarrassment. Hiding
the lineage creates credibility risk without buying protection — the event
modes, `Fiber`, `inject` and `ctx.effect()` make the relationship evident to
anyone familiar with the reference.

## Decision

Accepted 2026-11-22: publish the lineage openly.

- `README.md` gains a "Lineage" section naming the paper, Cordis and DeepSeek
  Harness, the frozen conformance pin, and the attribution file.
- `NOTICE` carries the MIT copyright and permission notices for Cordis
  (© Shigma) and DeepSeek Harness (© DeepSeek). Anima's own MIT `LICENSE` is
  unchanged. `pyproject.toml` declares `license-files = ["LICENSE", "NOTICE"]`
  so both ship in built wheels.
- `PARITY.md` presents the frozen pin as a deliberate compatibility target.
- The history-scrub gate is retired in `agents-home`; no history rewrite is
  performed or authorised. The two residual 2026-11-07 commit messages stay.
- agents-home decision D-025 records the policy.

## Consequences

- Provenance wording in docs, code comments and commit messages is now allowed
  and expected where relevant; do not reintroduce a scrub gate.
- Future upstream drift is still translated through ratified decisions (see
  ADR 0004 for the plugin-to-component rename); disclosure does not change the
  parity process.
- Revisit only if upstream licensing changes materially or a compliance ruling
  requires different handling.
