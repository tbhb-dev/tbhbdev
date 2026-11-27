# Appendix B: permissions, manifest keys, and events

> Today versus design: this page describes the design. The code has a webhook receiver, a request budget, and a GitHub client, and nothing in it creates, installs, or checks a GitHub App. The original design says what follows, and this repo can't verify the live App, its installed permissions or subscriptions, or the webhook route.

This appendix moves the App permissions, the manifest keys, and the excluded events out of the original design. Section 3 and decisions 0001 and 0011 give the short forms.

## Permissions (design)

Planned: the operator's App has every read-only permission GitHub offers for repositories and the organization. The design never grants write.

| Permission | Access | Why |
| --- | --- | --- |
| Metadata | Read | GitHub requires it of every App, and the reconciler needs repository metadata |
| Pull requests | Read | It covers the two subscribed events and every REST call the reconciler makes |
| Every other repository and organization permission that offers read | Read | The operator wants broad read access for later telemetry, and no permission grants write |

GitHub's permission tables put the list endpoints for pull requests (PRs), commits, reviews, and requested reviewers under Pull requests read. They list the issue timeline endpoint under either Issues read or Pull requests read. That means the timeline works without the Issues permission.

Today only the first two rows matter to the design. Each installation token request narrows to Metadata and Pull requests, and a later release may use the rest. Read access is broad, and it includes source code through Contents read and PR diffs through the PR and PR files endpoints. The design never calls those endpoints, as decision 0011 records. Decision 0012 covers what the service persists.

## Webhook events (design)

| Event | Actions used | Needs | Why |
| --- | --- | --- | --- |
| `pull_request` | `opened`, `closed`, `reopened`, `synchronize`, `review_requested`, `review_request_removed`, `ready_for_review`, `converted_to_draft` | Pull requests read | Lifecycle changes and review requests, as a prompt to reconcile |
| `pull_request_review` | `submitted`, `dismissed` | Pull requests read | First review and approval milestones, as a prompt to reconcile |

The planned worker ignores every other action of these events, such as `edited` or `labeled`. Each App also receives `installation` and `installation_repositories` by default without subscribing (unverified: the original design states it without a GitHub citation). The design uses an `installation_repositories` body that lists `repositories_added` to start a backfill of each newly added repository, as section 6 shows.

## Deliberately excluded events (design)

| Event | Reason |
| --- | --- |
| `pull_request_review_thread` | REST doesn't expose a thread's resolved state, so reconciliation couldn't heal a missed delivery. The design takes telemetry content only from REST, never from webhook bodies. Its payloads would also bring review comment text and diff hunks into the queue |
| `pull_request_review_comment` | Each review comment belongs to a review, and the `pull_request_review` event already reports the submission |
| `issue_comment` | Conversation comments don't affect the timing metrics, and the service has no use for it |
| `check_run` and `check_suite` | This design doesn't cover continuous integration (CI) today. These events also need Checks read, and they arrive once per job |
| `push` | The App can read Contents, but the PR commits endpoint already gives the same commits |
| Issues, Actions, and workflow events | Later releases may add them |

A later subscription to an excluded event doesn't need a permission change, because the permission is already in place.

## Manifest keys (design)

The App manifest sets two keys. `default_permissions` lists every read-only permission for repositories and the organization, and `default_events` lists the two subscribed events. The original design gives these 47 key names but no source page, and nobody has checked them against GitHub's "Permissions required for GitHub Apps" reference yet, so treat them as unverified. The manifest also sets `public` to `false` and puts the webhook URL of the planned public tunnel route under `hook_attributes`.

```json
{
  "default_permissions": {
    "actions": "read",
    "administration": "read",
    "artifact_metadata": "read",
    "attestations": "read",
    "checks": "read",
    "code_quality": "read",
    "codespaces": "read",
    "contents": "read",
    "dependabot_secrets": "read",
    "deployments": "read",
    "discussions": "read",
    "environments": "read",
    "issues": "read",
    "merge_queues": "read",
    "metadata": "read",
    "packages": "read",
    "pages": "read",
    "pull_requests": "read",
    "repository_custom_properties": "read",
    "repository_hooks": "read",
    "repository_projects": "read",
    "secret_scanning_alerts": "read",
    "secrets": "read",
    "security_events": "read",
    "statuses": "read",
    "vulnerability_alerts": "read",
    "custom_properties_for_organizations": "read",
    "members": "read",
    "organization_administration": "read",
    "organization_custom_roles": "read",
    "organization_custom_org_roles": "read",
    "organization_custom_properties": "read",
    "organization_copilot_seat_management": "read",
    "organization_copilot_agent_settings": "read",
    "organization_copilot_requests": "read",
    "organization_external_properties_for_repos": "read",
    "organization_announcement_banners": "read",
    "organization_events": "read",
    "organization_hooks": "read",
    "organization_personal_access_tokens": "read",
    "organization_personal_access_token_requests": "read",
    "organization_plan": "read",
    "organization_projects": "read",
    "organization_packages": "read",
    "organization_secrets": "read",
    "organization_self_hosted_runners": "read",
    "organization_user_blocking": "read"
  },
  "default_events": [
    "pull_request",
    "pull_request_review"
  ]
}
```

## Installation tokens (design)

To authenticate, the service signs a JSON Web Token (JWT) with the App's private key. Signing uses RS256, with the App's client ID as `iss`, `iat` 60 seconds in the past, and `exp` 9 minutes ahead, inside GitHub's 10-minute limit.

Next, the service exchanges the JWT for an installation token that expires after one hour, and asks for read access to metadata and pull requests only.

In memory only, the service keeps the token until less than 5 minutes remain, and then renews it. Neither disk nor a log ever receives the token.

Plan step 6 on [page 11](11-risks-and-technical-debt.md), a threat-model follow-up, covers retiring a previous webhook secret. It also covers rotating the App's private key.

## Related pages

Section 8 covers the shared request budget and the renewal after a `401`. Section 6 covers the reconciliation schedule.
