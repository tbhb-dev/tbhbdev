# D1. May tbhb-dev create a GitHub App without write permissions and install it on all repositories?

- **Status:** accepted by the operator, 2026-11-14.
- **Rule:** the operator decided that the App has every read permission for repositories and the organization, and no write permission. It can read source code, issues, checks, and PR data from every repository, private ones included, and its private key is a new credential. The service requests installation tokens narrowed to metadata and pull requests.
- **Changed afterwards:** the App subscribes to two PR events instead of three, because D11 drops `pull_request_review_thread`.
- **Undo:** uninstall or delete the App, which revokes every token at once. To narrow it, edit the App's permissions, and every installation must accept the change.
