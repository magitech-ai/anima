# 0013. Port target: canonical Cordis v4.0.0-rc.13

Date: 2026-11-23
Status: accepted

## Context

ADR 0012 disclosed Anima's lineage and pinned conformance to DeepSeek Harness
at `fcc0ad46` (`dsh v0.2.2-rc.1`, 2026-11-04), the target of the former 1.0
gate. The user has since directed that Anima must be a port of the **latest**
Cordis as of 2026-11-23, not that frozen pin.

Ground truth gathered on 2026-11-23:

- The canonical Cordis repository is `cordiverse/cordis`. Its latest GitHub
  release is `v4.0.0-rc.13`, published 2026-11-12 at commit `bc9fae7`
  (`https://api.github.com/repos/cordiverse/cordis/releases/443266891`).
- The npm `latest` dist-tag for `cordis` resolves to `4.0.0-rc.13`
  (`https://registry.npmjs.org/cordis/latest`), tarball published from the
  same commit.
- DeepSeek Harness vendors a *fork*, `@deepseek-ai/cordis`. Its master HEAD
  (`4b399c39`, 2026-11-22) carries `4.2.0-alpha.1`. That fork is downstream of
  the canonical `cordis` package and is not what "the latest Cordis" names; it
  remains a secondary conformance source for harness-specific behavior.

`v4.0.0-rc.13` is a breaking release relative to the old pin: plugins were
renamed to components (`ctx.use()`), `registry.delete()` was removed,
`entry.fiber` became `entry.innerFiber`, HMR was reorganized around a
programmatic `ctx.hmr.reconcile()` with per-unit commit/abort, file watching
moved to an opt-in `fs` service, and loader entry options gained explicit
injection scopes. A "1.0 gate" against rc.1 is therefore no longer evidence of
parity with the target.

## Decision

Anima's port target is canonical Cordis `v4.0.0-rc.13` (`bc9fae7`), with the
paper (arXiv:2608.25512) as the theory reference. Concretely:

- `PARITY.md` records rc.13 as the target and lists verified gaps as OPEN, not
  DONE. The former rc.1 "1.0 gate" is historical context only.
- `README.md` and `NOTICE` name rc.13 and state that the port is in progress.
- Every rc.13 behavior change is adopted through a test first (TDD) with a
  citation to the upstream `file:line` or paper algorithm; deliberate
  Python-platform divergences are recorded in this ADR series, not hidden.
- The DeepSeek Harness pin remains cited as historical provenance and as the
  harness conformance source, never as the port target.

## Consequences

- Claims of "1.0 complete" or "all items DONE" against rc.1 are withdrawn.
- The rc.13 breaking changes become tracked work items; until each is
  implemented and tested it is an admitted gap, not a silent divergence.
- Where rc.13 depends on Node.js internals (HMR's module loader), Anima ports
  the observable contract onto Python equivalents (`sys.modules`,
  `importlib`), and the substitution is documented rather than presented as
  identical code.
- Attribution remains open per ADR 0012; upstream MIT notices are shipped in
  `NOTICE`.
