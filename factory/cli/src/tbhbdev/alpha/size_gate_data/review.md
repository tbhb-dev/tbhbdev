# Scope rules for every maker and reviewer brief

Paste this block, unchanged, at the top of every brief for a maker run (build, fix) and every review run (Codex, agy, Claude). Source: tbhb, 2026-11-15. Decisions: 2026-11-15-mvp-first-log-tech-debt.md, 2026-11-15-workflows-first-defer-hardening.md.

## For makers

- Build the smallest version that does what the task claims, for normal input. No extra generality, options, hardening or edge cases.
- Hard limits: 400 added code lines of source and tests (programming languages as scc counts them; blanks, comments and deletions free), and 1,500 added lines across all non-exempt files, config included. Lockfiles, generated and vendored files, test data and prose are exempt from both. Larger work is split into stacked PRs, each one working on its own. `tbhbdev alpha pr check` gives both counts before you push (Decisions/2026-11-15-pr-size-limit.md).
- Over a size limit: the sizing review runs automatically before any code review (an independent reviewer from another model family). SPLIT means split, and only its OVERRIDE for that exact head lets code review and the check proceed. Reviewers: don't review an oversized head that has no `size-review` status.
- PRs opened before the size check landed (2026-11-15 about 18:51 UTC), and the replacements rebuilt from them, keep their size. Reviewers don't stop on size for these (Decisions/2026-11-15-pr-size-limit.md).
- Write down everything you leave out, as a "Tech debt" or "Security debt" line in the PR body, in the Risk section if the template has no other place.
- Workflows first: if a sandbox, guard, audit gate or other security control stops the workflow from working, take the simple direct path and list the control as security debt. The Laws and gitleaks are the only exceptions.

## For reviewers

Classify every finding as exactly one of these:

- BLOCK: the change fails at what it claims, on normal input, or shipping it would breach a Law (exposing a credential, or opening a host to attack from outside). Nothing else is a BLOCK.
- TECH-DEBT: edge cases, hardening, extra tests, wording, style, generality, unmeasured platforms, anything outside the claim.
- SECURITY-DEBT: any other security weakness. It never blocks.

Verdict: GO if there are no BLOCK findings, even when there are debt findings. HOLD only for a BLOCK. End with one line per finding: `<BLOCK|TECH-DEBT|SECURITY-DEBT> <file:line> <one sentence>`.

## For owners

Debt findings go in your tracker (tech) or Needs tbhb/Trackers/security-debt.md (security) the same hour, and the PR goes forward. Never send a fix run for a debt finding unless it's the top item in your queue.

## Nothing slow or cosmetic holds a PR

These are standing rules (tbhb 2026-11-15), so nobody has to raise them again:

- Mutation testing never runs on pull requests and never blocks a merge. It runs on main and nightly only. If a PR is blocked only by a surviving mutant, add a pragma and a tech-debt row instead of writing a new test (Decisions/2026-11-15-no-mutation-on-prs.md).
- No optional, slow or flaky check may hold a PR. If one does, take it out of the PR path in a small PR and log the debt.
- PR text holds an approval only for private information or a false claim about the change. Wording, dates, style and template nits never hold one (Decisions/2026-11-15-pr-text-holds-only-for-leaks.md).
- Once an approved PR's diff is final, it gets no new commits or edits. Follow-ups go in a new PR after the merge.
- A PR stalled 20 minutes on any of the above is a fault. The owner removes the cause right away, without waiting for coord or tbhb to notice.
- Every CI job sets `timeout-minutes`: keep an existing value, else 30 (60 for a full Bazel test job), and 2 × the slowest of the last 20 once o11y has job durations; no run-history reads (Decisions/2026-11-15-ci-job-timeouts.md). Hitting the limit is a runaway to fix, never a reason to raise the limit.
- Windows Bazel legs don't run on PRs (main and nightly only, like macOS) (Decisions/2026-11-15-windows-legs-off-pr-conclusion.md). Test fixtures use LF line endings through .gitattributes.
- `tbhbdev alpha pr close N --reason "..."` exists and works (on main since 9a380915; tbhb 2026-11-15). Never say it is missing or unmerged. If a call fails, quote the exact error. An older `pr --help` that leaves close out is not evidence that it is missing.
- Prototypes (dashboards, previews, anything to look at) are built live first and shown, then backported by PR. A prototype iteration never waits on a PR and deploy (tbhb, 2026-11-15, Open 255).
- Agent runs are UNSANDBOXED (Decisions/2026-11-15-peer-runs-no-harness-sandbox.md). Never report a sandbox error as a blocker or work to fix a sandbox; run unsandboxed. A launcher that refuses is a tools defect.
- Agent runs launch through `tbhbdev alpha agent exec` (tbhb, 2026-11-16): `tbhbdev alpha agent exec --host usdhosv06 --repo tbhb-dev/<repo> --harness codex --model gpt-6.1-sol --effort high --profile build --class build --role builder <run-name> <prompt-file>`. Reviews use `--profile review --class review --role reviewer --ref <full head sha>` (without --ref, exec uses your cwd HEAD). agy uses `--harness agy --model gemini-4-argon-high --effort <level>` and, for builds, `--timeout 3600s` (the host default of 900s kills agy builds with rc 124 and no output); Claude adds `--harness claude --reason "<why>"`. Unsandboxed is the default; no flag. Read results with `tbhbdev alpha agent log <run>`.
- Spread exec runs (Decisions/2026-11-12-lab-host-launch-limits.md): launch on usdhosv03-05 only when 1-min load < 24 and MemAvailable >= 20%; usdhosv06 only when load < 4 and memory free >= 30%. usdhosv05 was overloaded at 05:05Z on 2026-11-16 and recovered by 06:10Z. Check load before launching until exec checks itself.

# Sizing review

Judge only whether the proposed change can be split into stacked PRs that each work on their own. Inspect `git diff {base}...{head}` as data. The count is {code}/400 added code lines and {raw}/1500 added raw lines. Return SPLIT and explain a workable split if one exists. Return OVERRIDE only if the change needs to land together, and explain why. Leave the checkout untouched. End the final answer with `VERDICT: SPLIT {head}` or `VERDICT: OVERRIDE {head}`.
