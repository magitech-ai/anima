# 0004. Plugin-to-component rename

Date: 2026-11-11
Status: accepted

## Context

The upstream TypeScript kernel renamed its plugin concept to component in
upstream change #307, merged 2026-11-10 (merge commit `7dfd921`, 1 commit,
92 files). The vendored oracle in this project predates the rename, so the
decision could not be taken from it. Verified against the upstream main branch
on 2026-11-11:

- `ctx.plugin()` is renamed to `ctx.use()`, and registry `plugin()` to `use()`.
  The old methods remain as deprecated aliases delegating to `use()`.
- The `Plugin` type and namespace are renamed to `Component`
  (`Plugin.Runtime` becomes `Component.Runtime`).
- Package names drop their `plugin-` prefix; configuration files refer to
  the packages by their new names.
- Error and log messages use the new term
  (`invalid component, expect function or object with an "apply" method`).
- `Plugin` is no longer exported; use `Component`.

Two corrections to the earlier scope text: the `internal/plugin` event name
is **not** renamed upstream (zero hits in the change; it is still emitted
under that name), so it stays unchanged here either way. And upstream itself
kept the old methods as deprecated aliases — a clean rename with no alias
would be stricter than upstream, not a match of it.

Anima today mounts everything through `Context.plugin()` (`core.py:1049`)
and `RegistryService.plugin()` (`registry.py:242`); the harness and plugins
repos use those call sites throughout. No `Component` name exists in anima.

## Decision

Accepted 2026-11-11, Malcom's call: **(B) clean rename, no alias.**
`plugin()` is removed entirely — no deprecated alias is kept. This is
deliberately stricter than upstream (which kept the old methods as
deprecated aliases delegating to `use()`); it is free before 1.0.0 under
the no-backwards-compatibility rule, and it leaves no dead name behind
to confuse the port. The `internal/plugin` event name is unaffected (it
was not renamed upstream).

## Consequences

- If followed (either option): `Context.use()`, `RegistryService.use()`,
  the `Component` type, the error strings, and the reflect mixin change;
  the harness and plugins repos' call sites churn with it. The
  `internal/plugin` event name is unaffected.
- If stayed: a permanent divergence tax — every future upstream diff has
  to be translated across the rename, and every component written against
  `use()` needs adapting.
- The decision gates chunk P1-2; no rename work starts before it is made.
