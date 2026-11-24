# 0019. Rename the project to Animas

Date: 2026-11-24
Status: accepted (user decision)

## Context

The distribution name `anima` is already registered on PyPI (the project
record exists with no releases), so the project cannot publish under that
name. `animas` is unregistered and available. The user chose a complete
rename: distribution, Python import package, tests, documentation, and the
GitHub repository all become `animas`.

Anima `1.0.0` has never been published to PyPI. The compatibility policy in
`docs/COMPATIBILITY.md` promises no breaking changes to downstream consumers
of **published** `1.x` releases; there are none, so this rename does not
break that promise. The Python import path changes from `anima` to `animas`,
and every in-repo consumer is updated in the same coordinated set of PRs.

## Decision

- Distribution name: `animas` (`pyproject.toml [project].name`).
- Import package: `animas` (`src/animas/`); all imports become
  `from animas import ...`.
- Test package: `tests/unit/animas/`; coverage target `--cov=animas`.
- GitHub repository: `magitech-ai/animas`.
- PyPI trusted publisher fields: project `animas`, owner `magitech-ai`,
  repository `animas`, workflow `publish.yml`, environment `pypi`.
- Downstream constraint: `magitech-harness` moves from `anima>=1,<2` to
  `animas>=1,<2`; other Magitech repos update their imports.
- Version stays `1.0.0`: the rename precedes the first publication.

## Consequences

- `pip install animas` then `import animas`. There is no `anima` alias or
  shim; a single canonical name keeps the surface unambiguous.
- All historical ADRs and release notes are renamed with the project. Their
  decisions, dates and attributions are unchanged; this is a rename, not a
  rewrite of history. Lineage disclosure (ADR 0012) and upstream attribution
  in `NOTICE` remain intact.
- The PyPI trusted publisher must be configured with the new repository
  name; the OIDC claim uses the literal repo string and does not follow
  GitHub's old-URL redirect.
