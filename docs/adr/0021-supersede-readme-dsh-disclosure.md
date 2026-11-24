# 0021. Supersede the README DeepSeek Harness disclosure requirement

Date: 2026-11-24
Status: accepted (user decision)

## Context

D-025 in `agents-home` (2026-11-22) reversed an earlier history-scrub policy.
ADR 0012 implemented it and required `README.md` to name the paper, Cordis and
DeepSeek Harness, with the frozen pin `fcc0ad46` (`dsh v0.2.2-rc.1`). That was
correct at the time: the 0.3.0 kernel had been built and conformance-checked
against that pin, so full disclosure meant naming it. The decision was sound
when made; its premise later changed.

ADR 0013 moved the conformance target to canonical Cordis `v4.0.0-rc.13`.
ADR 0016 deleted the DSH-fork extension set. The current kernel contains no
DeepSeek Harness-derived code and implements the paper's paradigm. DeepSeek
Harness is therefore historical provenance with no relationship to Animas's
present design or compatibility contract.

ADR 0020 made the paper the primary citation and described Cordis rc.13 as
historical conformance evidence, but explicitly preserved ADR 0012's README
naming requirement. That requirement now makes current-facing documentation
name a project that has no present role in Animas.

## Decision

- The README Lineage section no longer names DeepSeek Harness or its pin.
- ADR 0012 is **not edited**. It remains an accurate record of the 2026-11-22
  decision and its then-current premise. Its README requirement is superseded
  by this ADR, in the same way D-025 superseded the earlier scrub policy rather
  than rewriting it.
- ADR 0020's statement that the ADR 0012 naming requirement "is still satisfied"
  is likewise superseded by this ADR; ADR 0020 is not edited.
- The DeepSeek Harness MIT copyright and permission notice stays verbatim in
  `NOTICE`, and ADRs 0012–0016 plus `PARITY.md` keep the historical record. Only
  the current-facing README changes.

## Consequences

- User-facing docs cite the paper and one completed Cordis rc.13 conformance
  milestone; no reader is led to think DeepSeek Harness is a current dependency.
- Prior decisions remain auditable: the reason for the 2026-11-22 disclosure is
  still legible in ADR 0012, and the reason it was retired is here.
- Legal attribution is unaffected because the MIT notice is unchanged and still
  ships in the wheel and sdist via `license-files` in `pyproject.toml`.
