# Hermes UX system evidence — November 18, 2026

Claim class: advisory. Implementation head `069ca611b383b65ea3b44876756ae423f33a2e4f`, for [Hermes PR #157340](https://github.com/NousResearch/hermes-agent/pull/157340).

## Frozen Hermes first wave

| Metric | Value | Sample | Receipt |
| --- | --- | --- | --- |
| Open reported problem clusters | 10 | 19 selected clusters | [counts.json](counts.json) |
| Qualifying merged problem clusters | 7 | 19 selected clusters | [counts.json](counts.json) |
| Product fixing PRs for qualifying merged clusters | 8 | 7 qualifying merged clusters | [snapshot](hermes-first-wave.json) |

Scope is Oct 1–Nov 18, 2026 Bangkok, plus one September carry-in. The ten open clusters include nine newly filed clusters. Two recovery-ambiguous merged candidates remain excluded. This bounded historical screen is a lower bound, not a census. Metadata was refreshed Nov 18. Each cluster explicitly records current release-blocker assessment as `not_assessed`.

Run and re-score the frozen counts with `python3 recount.py`. It reads only the included snapshot. It does not reclassify reports or query current release behavior. The manifest hashes each result input.

## Implementation validation

[GitHub-hosted run](https://github.com/100yenadmin/hermes-agent-for-upstream-PR-only/actions/runs/44296829176), at the exact implementation head above, ran:

```sh
scripts/run_tests.sh -j 2 --file-timeout 180 --file-retries 0 tests/scripts/test_ux_blockers.py tests/scripts/test_release_sequencer.py -q
```

Observed lines copied from the hosted log:

```text
Known repository writer permission verified
=== Summary: 2 files, 70 tests passed, 0 failed (100% complete) in 1.3s (2 workers) ===
```

The permission smoke independently enumerated a writer and required a positive live permission result using the built-in Actions token. The fixture suite covers intake/confirmation/disposition, acknowledged-source deletion plus label loss, closed-inclusive canonical inventory, merged fix inclusion, malformed untrusted input, duplicate cycles, and mandatory checks before all four publication mutation paths.

Independent agent semantic review initially identified four verified blockers. The consolidated correction fixed canonical inventory, the merged-PR query, malformed URL handling and per-issue event coalescing. Targeted delta review returned PASS at the same head. This is independent source review, not a maintainer approval. Actionlint, focused Ruff and the repository code-health ratchet also passed locally.

## Not proven

Upstream deployment or maintainer adoption, a live posted triage cycle, actual stable publication, full repository CI, native macOS red/green recovery, or current release failure for all selected reports. The test fixtures prove the implemented system decisions, not that individual product bugs are fixed. No live customer or production agent was exercised.

The current upstream main moved to `0e8e42164063bf426af328ef94e2001dcaf9db1f`; its changed files do not overlap this PR's files. Product fix #128465 remains a separate open PR at `4506f9e8f0bdfdb4401a03b071933dab2f4f4c16`.
