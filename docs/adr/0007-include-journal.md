# 0007. Include journal

Date: 2026-11-11
Status: accepted

## Context

Correction: the earlier finding that "no journal oracle exists" is
withdrawn. The upstream kernel's include package has a journal module
(`journal.ts`, 242 lines), introduced in upstream change #121 on
2026-09-06 to reconcile file and runtime edits through a journal, and
present on upstream main. It was absent from the vendored oracle because
the vendored copy predates that change — the absence was an oracle gap,
not an absence upstream.

The upstream surface is: `JournalRecord` (Remove | Upsert, with `created`,
`parent`, `position`, `changes`), `Journal` as a map keyed by entry id,
`diffOptions` (deep-equal based), `mergeRecords`, `merge`, `record`,
`flatten`, `applyChanges`, `detach`, `place`, `applyJournal`, and
`reconcile` with `Conflict`. The include service records tree-side
mutations into the journal, reconciles them against file changes, and
drains the journal on write.

Anima's `Journal` is a partial re-invention of that surface: it has the
`upsert` / `remove` kinds and the `created` flag with shallow coalescing,
but is missing `parent` / `position` / `changes`, the deep-equal diff,
`mergeRecords`, `detach` / `place`, `flatten`, `reconcile`, and the
write-back integration. It is constructed in `IncludeService` and never
read by any production code.

## Decision

Re-derive anima's journal from the upstream journal module in chunk P1-7
(port, not delete). Deletion is rejected: the feature exists upstream and
is the mechanism that reconciles file edits with runtime edits, so
dropping it would leave include without write-back. Anima's
`Journal` / `JournalRecord` / `diff_entries` are replaced by the ported
surface; the loader's `internal/update` write-back and `Include.write()`
drain the journal as upstream does.

## Consequences

- Chunk P1-7 carries the journal port alongside the include replacement;
  the journal is not a separate chunk.
- Until P1-7 lands, anima's journal stays dead code — it is not wired up
  in P1-0 and must not be presented as working.
- The workspace plan's §2.2 "no oracle" claim is corrected by errata; the
  correction, not a silent re-scope, is the record.
