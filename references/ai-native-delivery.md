# GitHub PR delivery boundary

Use this mode only for GitHub Delivery Issue implementation. These are safe defaults; the target repository's contribution guide, branch protection, module map, and workflow profile may add stricter project-specific gates.

## Delivery contract

Before dispatch, the Lead Agent must have one implementation-sized Issue containing the intended outcome, acceptance criteria, declared change types (`add`, `modify`, and/or `delete`), affected modules, validation expectations, and known risk or rollback needs. One Issue maps to one feature branch, one linked worktree, and one PR.

Apply the gates for every declared change type:

- `add`: place the capability in a named module, expose it through a deliberate public contract, add contract-level tests where applicable, and update maintained module documentation.
- `modify`: identify consumers of the old behavior, update or version affected contracts, remove the replaced path unless a time-bounded migration is declared, and update affected tests/docs.
- `delete`: audit reverse dependencies, remove implementation/tests/docs/flags and dedicated dependencies together, record any data retention or migration decision, and verify no live references remain.

Scope that crosses undeclared modules or change types returns to Issue shaping before implementation continues. Adjacent discoveries become linked follow-up Issues instead of silently expanding the PR.

## Default pipeline

Use this normal path unless the repository workflow profile explicitly overrides it:

| Stage | Trigger and work | Owner | Boundary/result |
|---|---|---|---|
| Local preflight | Read project rules; confirm Ready Issue, change types/modules, branch, worktree, and existing CI contract | Lead Agent | Exact delivery contract and isolated worktree |
| Implementation | Modify only declared scope; run local gates; update docs; commit | External Code CLI | Local evidence and committed feature branch |
| PR handoff | Push feature branch and create/update its one PR | External Code CLI | PR event triggers CI; implementation process exits without waiting |
| PR CI | Run automated checks on PR open/update/ready events | GitHub automation | Durable checks, logs, and artifacts |
| Engineering review | Inspect diff, Issue gates, local evidence, PR metadata, and one CI snapshot | Lead Agent | `changes_requested`, `awaiting_ci`, `blocked`, or `merge_ready` |
| Repair | Fix review/CI failures on the same Issue, branch, worktree, and PR; push again | External Code CLI under Lead dispatch | New PR revision and CI run; no second PR |
| Merge | Merge only after gates pass and merge authority exists | Lead Agent or authorized maintainer | Engineering Done after confirmed merge and Issue closure |
| Post-merge delivery | Default-branch CI, deploy, migrations, smoke checks, rollback readiness | GitHub automation plus release owner | Release evidence; outside the Code CLI session |
| Product acceptance | Run controlled product scenarios when required | User/product owner | Accepted or a linked fix Delivery Issue |

Typical CI starts after the feature branch is pushed and the PR is opened or updated. Repositories may also run branch-push CI, but avoid duplicate equivalent workflows. Required checks must be terminal before merge. Typical CD starts after merge to the default branch, or from an authorized tag/release; a PR preview deployment is optional, isolated, and never counts as production Released.

Do not redesign CI/CD as an incidental implementation change. If the repository has no required checks, conflicting triggers, or an unsafe runner trust boundary, report that as a workflow/profile gap unless the Delivery Issue explicitly includes CI work. Public repositories and untrusted fork PRs must not run on a persistent private self-hosted runner.

## Responsibility split

| Actor | Owns | Must not claim or do |
|---|---|---|
| Lead Agent (the user's current Agent) | Confirm Ready Issue; create/select branch and dedicated worktree; frame prompt; dispatch; independently review diff, validation and CI; request fixes; record review checkpoint; make an authorized merge decision; clean up after confirmed merge; separately verify post-merge delivery | Treat executor output or green CI as product acceptance |
| Implementation Agent (external Code CLI) | Work inside one Issue/worktree; implement declared add/modify/delete scope; update affected docs; validate locally; commit; push feature branch; create or update one PR; report current evidence | Wait/watch/poll CI; merge PR; push default branch; close Delivery Issue manually; delete branch/worktree; deploy after merge; mark Engineering Done, Accepted, or Released |
| GitHub automation | Trigger on configured events, run required checks/deploy jobs, and preserve evidence | Make product, merge-authorization, or release decisions |
| User/product owner | Product decisions, exceptions, controlled acceptance, release judgment, and merge authorization when not pre-authorized by project policy | — |

The Code CLI stopping point is an open or updated PR in `In Review`, or a precise blocker. It may report CI already visible immediately after opening the PR, but it must not remain running merely to await CI and it never merges.

## Lead Agent loop

1. Read project instructions and workflow profile. Confirm one Ready Delivery Issue with change types and affected modules, then move it to `In Progress` before implementation.
2. From the main workspace, fetch and create or reuse exactly one feature branch plus linked worktree for that Issue. Do not develop in the main workspace.
3. Launch the external CLI in `--delivery-mode pr`; the runner validates the branch/worktree and appends the non-merge/non-wait boundary to the effective prompt. When the PR exists, reflect `In Review` in the project's GitHub state mapping.
4. When the implementation process finishes, run `review`. It captures the result, Git evidence, and one read-only GitHub/CI snapshot; it never invokes watch mode or polls.
5. Independently review the stored baseline-to-HEAD diff, uncommitted changes, Issue scope, add/modify/delete gates, affected docs, validation evidence, risk, rollback, and PR metadata.
6. Interpret the one-shot CI snapshot:
   - pending/queued → record `awaiting_ci`, end the active turn, and resume later from an event, scheduled follow-up, or another single snapshot;
   - deterministic failure → record `changes_requested` and dispatch a repair on the same branch/worktree/PR;
   - infrastructure failure → preserve evidence and restore/escalate the executor rather than changing correct product code;
   - passed → complete independent review and continue to the merge gate.
7. Persist the Lead conclusion with `checkpoint --decision changes-requested|merge-ready|blocked`. `merge-ready` requires a completed session, clean worktree, exact reviewed commit matching the PR head, a matching open/non-draft/mergeable PR, and passing required CI evidence. Any project-authorized CI exception must be explicit and durable; it cannot replace PR/commit identity evidence. The checkpoint performs no merge.
8. With explicit user authorization or a project workflow profile that pre-authorizes merge after stated gates, the Lead Agent may merge. Without that authority, report `merge-ready` and stop.
9. Only after GitHub confirms `MERGED` may the Lead Agent mark Engineering Done, verify Issue closure, clean the exact worktree/branch, and update the main workspace.
10. Observe default-branch CI/CD separately. Verify the deployed commit/environment and required smoke or migration checks before reporting Released; perform product acceptance separately when required.

## Durable review artifacts

The implementation process writes `session.json`, `status.json`, `execution.log`, `result.md`, and Git snapshots. `review` adds `git-review.json`, `github-review.json`, and `review.md`. `checkpoint` captures `git-checkpoint.json`, adds the current `lead-review.json`, and preserves history in `lead-review-history.json`.

- `result.md`: what the external CLI claims it did;
- Git snapshots/diff: what changed;
- `github-review.json`: what the PR and CI reported at one moment;
- `lead-review.json`: what the Lead Agent concluded and why.

No single artifact is acceptance. Do not put credentials in them, and do not copy large CI logs into the session unless diagnosing a failure; retain links and concise evidence instead.

Every later `review` makes the prior Lead checkpoint stale until the Lead records a new conclusion against the refreshed Git/PR/CI evidence.

## Completion layers

- External CLI completed successfully: implementation session finished; not Engineering Done.
- PR open and CI pending: `awaiting_ci`; not merge-ready.
- PR open, required CI passed, independent review passed: `merge_ready`; still not Engineering Done.
- PR merged and Delivery Issue closed: Engineering Done.
- Controlled scenarios accepted by the responsible user: Accepted.
- Deployment/release checks passed in the target environment: Released.

Product acceptance can fail after a correct merge. Create a linked fix Delivery Issue rather than pretending the original executor session was still running or reopening its scope indefinitely.

## Independent review evidence

At minimum, the Lead Agent should retain:

- Issue, branch, linked worktree, PR URL/state, and base commit.
- Full `base_commit...HEAD` name/status and stat, plus uncommitted/staged changes.
- Exact local validation commands and results.
- Required CI jobs from a terminal snapshot, or a recorded pending/failure state.
- Add/modify/delete gate results and module-boundary exceptions.
- Lead review decision and any explicit gate exception.
- Engineering, product acceptance, and release statuses reported separately.
