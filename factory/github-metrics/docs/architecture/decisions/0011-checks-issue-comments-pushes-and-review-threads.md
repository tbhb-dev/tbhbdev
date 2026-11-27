# D11. Checks, issue comments, pushes, and review threads stay out of the first release

- **Status:** accepted, 2026-11-14.
- **Rule:** the planned App has read access to Checks, Issues, and Contents but the service doesn't call them. It doesn't subscribe to `check_run`, `check_suite`, `issue_comment`, or `push`, nor to `pull_request_review_comment`, which a submitted review already signals, nor to `pull_request_review_thread`, whose resolution REST can't confirm.
- **Undo:** subscribe to the event, and the permission is already in place.
