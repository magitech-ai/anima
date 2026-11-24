# Animas compatibility policy

Animas follows [Semantic Versioning 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH`.

## Public API

The supported surface is:

- every name exported by `animas` (`animas.__all__`);
- documented behavior of those names, including the component lifecycle,
  effect tracking, coeffect resolution, event modes, config reconciliation,
  and the `fs`/`hmr` service contracts;
- documented event names and their payloads (`internal/*`, `loader/*`,
  `hmr/*`, `watcher/*`); and
- the YAML profile format documented in `README.md`.

Internal helpers (names prefixed with `_`, private modules, and undocumented
attributes) are not part of the contract.

## What each version bump permits

| Bump | Permitted changes |
|---|---|
| `PATCH` | Bug fixes, documentation, tests; no observable API change except fixing behavior that contradicted documentation. |
| `MINOR` | New exports, events, services, optional parameters, and new documented behavior; existing callers continue to work unchanged. |
| `MAJOR` | Breaking changes: removing or renaming exported names, incompatible signature or event-payload changes, or removal of documented behavior. |

## Deprecation

A breaking change in `2.0.0` will be announced in a `1.x` release: the old
name or behavior remains supported and emits a `DeprecationWarning` for at
least one minor release before removal. The release notes must name the
replacement and the removal version.

## Guarantees at 1.0.0

- Every `1.x` release passes the full gate: pytest with 100% statement and
  branch coverage, `ruff check`, `ruff format --check`, and strict `mypy src`.
- CI on all configured OS legs is green before merge; CI is the authority and
  a human merges.
- Downstream consumers may pin `animas>=1,<2` and expect no breaking changes.

## Non-goals

This policy does not freeze the project against improvement, does not promise
that bugs are impossible, and does not make upstream Cordis releases part of
the contract. Animas is independent; the research paper remains the design
invariant, and upstream Cordis is historical reference material.
