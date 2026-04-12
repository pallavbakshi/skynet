# Refactoring Checklist

Rules for safe refactoring — especially when splitting monoliths into submodules.

## Before Starting

- [ ] Identify all external consumers: `grep -rn "from <module> import" src/ tests/`
- [ ] Note every public name that must remain importable
- [ ] Run full test suite and record the pass count as your baseline

## During the Split

- [ ] Every `@app.command()` or `@router.get()` decorator must register on the same app/router
- [ ] Module-level singletons must exist in exactly one place — not duplicated across files
- [ ] `from x import y` creates a local reference — patching `x.y` won't affect the caller's copy. Either:
  - Import lazily inside the function body, or
  - Patch at the call site (`caller_module.y`), not the source (`x.y`)
- [ ] Private globals mutated by tests (`_REDIS_CLIENT_FACTORY`, etc.) must be patchable at their actual location

## After the Split — Reviewer Feedback

Every reviewer finding gets an explicit disposition. No exceptions.

| Disposition | What it means |
|---|---|
| **FIXED** | Code changed, test added or updated |
| **NOT A REGRESSION** | Evidence: grep showing no callers, or proof behavior is identical |
| **KNOWN CHANGE** | Exact behavioral difference documented + why it's acceptable |

**Never skip a finding. Never summarize a finding in your own words without verifying your summary matches what the reviewer actually said.**

When you decide NOT to fix a reviewer finding, apply MORE scrutiny, not less. The default should be "the reviewer is right." Overriding that requires concrete evidence — not "probably fine."

## After the Split — Regression Check

- [ ] Every reviewer finding has an explicit disposition (see table above)
- [ ] `git diff master` shows only structural changes (moves, renames, new files) — no logic changes unless intentional and documented
- [ ] Any function that was rewritten (not just moved) has been diffed line-by-line against the original
- [ ] Error handling paths are identical — grep for `except`, `raise`, `typer.Exit` in changed files and compare with originals
- [ ] All public names from the original module are importable from the new package
- [ ] Test pass count matches baseline (not "close to" — exactly matches)
- [ ] Module-level singletons are the same instances (not duplicates created by double imports)

## Common Mistakes

**"Slightly different wording, same behavior"** — If the error message changed, the code path changed. Trace it. A `_cli_client` wrapper catching `TransportError` before the function's own handler is not "different wording" — it's a behavioral change that turns partial-data output into a hard exit.

**"I changed this to fix test mocking"** — If fixing one problem (test mock patching) creates another (behavioral regression), fix both. Don't let implementation convenience justify a regression.

**"The reviewer flagged it but it's fine"** — Re-read what the reviewer actually wrote, not your mental summary. Your summary may have compressed away the critical detail. The reviewer who spent 5 minutes tracing an error propagation path is probably more right than your 5-second "looks fine."
