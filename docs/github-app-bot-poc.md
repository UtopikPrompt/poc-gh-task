# GitHub App Bot POC Proposal

## Purpose and hypothesis

Determine whether a custom GitHub App bot should become part of the task flow. The proposed alternative hypothesis is that assigning a repository issue to the bot can trigger a custom workflow. This hypothesis is unconfirmed; the actual POC and any inclusion decision are pending.

Until a POC decision changes it, the current intake baseline remains a human setting the GitHub Project task to `ready`.

## Boundaries and non-goals

- Evaluate one target repository and one task; use least privilege scoped to the selected repository.
- LangGraph produces PRs only and must not invoke or launch external commands.
- GitHub-native CI is a separate system; its required checks must pass before merge.
- Only a human may merge. A successful merge sets the task to `done`.
- The POC has no Copilot dependency.
- This proposal does not authorize implementation, deployment, or a change to the current task flow.

## POC stages and acceptance criteria

1. **Verify the identity.** Confirm the actual GitHub App bot identity is assignable to an issue in the selected test repository. Record the observed identity and any prerequisites.
2. **Verify event delivery and handling.** Confirm an assignment delivers the `issues.assigned` webhook through the intended App installation, and repeated delivery is handled idempotently.
3. **Measure permissions.** Determine and record the minimal App permissions needed for the evaluated behavior. Treat permissions as a POC result, not an assumption.
4. **Demonstrate bounded PR creation.** Produce a PR on a branch using bounded GitHub operations, without LangGraph launching commands. Record the App's effective authority and confirm it cannot merge.
5. **Verify PR governance.** Confirm PR check status is observable, required GitHub-native CI must succeed before merge, and a human alone performs the merge. Confirm successful merge sets the task to `done`.
6. **Bound any broader claim.** Before claiming support for multiple repositories or organizations, decide whether testing more than one repository in the selected organization, or testing another organization, is necessary. A one-repository result does not establish multi-repository or multi-organization support.

Acceptance requires evidence for each applicable stage, including failures and limitations. No exact permission set or broader support claim is presumed.

## Evidence and results

The recorded result is limited to the reported assignability check. It does not establish that the remaining POC stages pass.

| Stage | Evidence or observation | Result (pass, fail, blocked, or not run) | Blocker or limitation |
| --- | --- | --- | --- |
| Bot identity assignable | For candidate `app-bot-poc[bot]` in `UtopikPoC/github-app-bot-poc`, the installation-token eligibility GET returned HTTP 404. Repository-access and candidate-identity probes passed in the updated checker, per the user's report of the same result after those controls were added. | Fail | The candidate was not assignable according to the eligibility endpoint in the reported run. That HTTP 404 preceded the change to request `issues:write`; no post-change run was reported, so the `issues:write` experiment is unverified and the result is not attributed to that scope. |
| `issues.assigned` delivery and idempotent handling | Not run. | Not run | No webhook delivery or idempotency test was reported. |
| Minimal permissions measured | Not run. | Not run | The `issues:write` experiment has no reported post-change result. |
| Bounded PR on a branch; no LangGraph command launch | Not run. | Not run | No PR workflow test was reported. |
| PR checks observable; CI required; human-only merge; merge sets task done | Not run. | Not run | No PR governance or merge lifecycle test was reported. |
| Scope needed for broader repository or organization claims | Not run. | Not run | No broader-scope evaluation was reported. |

**Overall outcome:** Blocked/failed at the bot-assignability gate; the broader POC is incomplete.

**Evidence references:** User-reported checker observations; no post-`issues:write` result is available.

**Open blockers or alternatives:** Assignment-trigger intake is blocked by the reported eligibility result. The event, permission, PR, governance, and broader-scope stages remain untested. Retain the current human-set `ready` trigger baseline; no decision to include the App in the task flow has been made.

## Decision rule

Adopt assignment-triggered bot intake only if the acceptance criteria pass and a separate explicit decision approves inclusion. If any criterion fails or remains blocked, preserve the `ready` trigger baseline and record the blocker and an alternative. The reported assignability result blocks this hypothesis; the other POC stages have not been run, so the POC is incomplete and no inclusion decision has been made.