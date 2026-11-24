# 0020. Citation and attribution policy

Date: 2026-11-24
Status: accepted (user decision, independently reviewed)

## Context

ADR 0017 made Animas an independent project with the research paper as its
design invariant and upstream Cordis as history rather than a forward pin.
The documentation had not fully caught up: `README.md` still described the
paradigm as "formalized in Cordis" and Animas as "Cordis-powered", while the
paper — the actual design source — was cited in only a handful of files.

At the same time, `NOTICE` reproduces four full MIT copyright and permission
blocks (Cordis, DeepSeek Harness, schemastery, cosmokit) and `pyproject.toml`
ships that file in every wheel. ADR 0012 requires lineage disclosure; ADR 0017
requires that independence not rewrite history. Any reframing must satisfy
both.

## Decision

- The paper *"A Programming Paradigm for Spatiotemporal Composability"*
  (arXiv:2608.25512) is the **design source** and is cited first in README and
  NOTICE.
- Cordis `v4.0.0-rc.13` is a **historical conformance reference**, not an
  ancestor or a forward target. The DeepSeek Harness pin `fcc0ad46` is retained
  as **historical provenance and a secondary conformance source**.
- The MIT copyright and permission notices in `NOTICE` are **non-negotiable**
  and remain verbatim, as does `license-files` in `pyproject.toml`.
- The historical conformance record — `PARITY.md` verdicts, ADRs 0012–0016,
  upstream `file:line` citations in parity tests, and code comments recording
  which rc.13 behavior a port matches — is **kept intact**. Independence means
  reinterpretation of that record, not erasure.
- "Animas is a Python implementation of the paradigm" replaces "reimplementation"
  when describing the project: it implements the paper's paradigm and was
  conformance-checked against a reference kernel, rather than being a second
  implementation derived from that kernel's source.

This ADR consolidates and interprets ADRs 0012, 0013 and 0017; it changes none
of their decisions. In particular, ADR 0012's requirement that the README
Lineage section name the paper, Cordis and DeepSeek Harness is still satisfied.

## Consequences

- Documentation leads with the paper. Cordis appears as completed historical
  evidence, never as a roadmap or a compatibility constraint.
- Legal attribution obligations remain satisfied because the MIT notices are
  unchanged and still shipped in the wheel and sdist.
- The conformance evidence stays auditable: a reader can still trace a behavior
  to the exact upstream location that was checked.
