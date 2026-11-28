# Autonomous pull request loop

An agent picks up an issue and authors a change in its own sandbox. After agent review, it fixes findings and CI failures. Once the fixes pass CI, the agent enqueues the PR and fixes queue ejections until the PR merges. The orchestrator selects the next step.

The loop starts with the pick up issue phase. It runs through the authoring, review, and merge loops and ends with watch main. Each round instantiates the shared fragments it needs.

- Provision round sandbox
- Fix round
- Publish agent-written content

## Method

The model follows Johnson and Breckenridge's “Goal-Function Tree Modeling for Systems Engineering and Fault Management” (AIAA Infotech@Aerospace, 2013).

- Each function includes its output state vector. A goal is a range of values on that vector.
- Sense, decide, and respond run in series.
- The tree describes success. It uses `AND` by default and `OR` for alternatives.
- Every path from bottom to top needs at least one detection.
- A fault tree uses the same state variables to show failure paths outside the success tree.

## Status

This repository is rebuilding the model from the operator's verification package of 2026-11-27. Keep the model fixed until the rebuild reproduces every value below.

## Verification baseline

| Method | Check | Expected |
| --- | --- | --- |
| Model lint | Tree and registries | 287 tree nodes in 8 phases and fragments, 90 triggers, 38 fault-tree leaves, 16 registered state variables, 0 errors, and 54 goals with an implicit don't-act decision. |
| Cross-check | References | 0 unresolved references across 266 tree nodes. |
| System-Theoretic Process Analysis (STPA) | Unsafe control actions | 38 unsafe control actions over 7 hazards, 32 covered, 6 partial, and 0 uncovered. |
| Failure Mode and Effects Analysis (FMEA) | Failure modes | 32 failure modes, 23 detected, 4 partly detected, and 5 undetected. |
| Fault tree | Unintended merge (smallest order 1) | 11 minimal cut sets, with 5 blocked, 6 weak, and 0 open. |
| Fault tree | Leak (smallest order 2) | Of the 5 minimal cut sets, 3 have blockers, 2 are weak, and 0 are open. |
| Fault tree | Stall (smallest order 1) | Blockers cover all 5 minimal cut sets. The weak and open counts are both 0. |
| Plans | Fault injection | The plan covers all 90 triggers. |
| Plans | Adversarial cases | 18 cases. Every expected trigger and fault-tree event resolves. |
| TLA+ (`Orchestrator`, recorded output) | Design | Every invariant and both liveness properties hold. The check visits 659,202 distinct states and reaches depth 40. |
| TLA+ (`Orchestrator`, recorded output) | Seeded configurations | `bot_bug` violates `Terminates`. `bot_bug_watchdog` holds. `platform_gate` violates `MergeSatisfiesTier`. `platform_gate_dismiss` holds. `no_ack` violates `EscalationsAcknowledged`. `no_reconcile` violates `Terminates`. |
| Alloy (`access`, recorded output) | Commands | 18 commands. |
| Alloy (`access`, recorded output) | Design safety properties | 7 checks are unsatisfiable (`Design_AllSafe`, `Design_MinimalTextSinks`, `ConfigFromBranch_StillCantPush`, `OverScopedTokens_PolicyHolds`, `BotCommentTokenPerHost_HarnessHolds`, `BotCommentToken_PolicyHolds`, and `SecretLeaked_StripAndRouteHold`). |
| Alloy (`access`, recorded output) | Variants | 8 variant checks are satisfiable. Each finds its counterexample. |
| Alloy (`access`, recorded output) | Non-vacuity | 3 non-vacuity runs are satisfiable. |
| Conformance checker | Sample logs (5) | The checker accepts two conforming logs and rejects two violating logs. It flags one stalled log. |

## Limits

- TLA+ checks one PR with bounds on heads, main moves, and lost rounds. Alloy checks up to five atoms per signature. Both results apply within those bounds.
- The checks verify the design. Deployed behavior needs its own evidence.
- The analyst who built the tree also did the STPA and FMEA, so they provide a correlated cross-check.
- The fault-tree ranking uses structure alone because the model has no failure rates.
- The model registers state variables and leaves their types unspecified.
