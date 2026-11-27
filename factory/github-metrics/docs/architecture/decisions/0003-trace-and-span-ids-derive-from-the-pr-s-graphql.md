# D3. Trace and span IDs derive from the PR's GraphQL node ID

- **Status:** accepted, 2026-11-14.
- **Rule:** the trace ID is the first 16 bytes of a Secure Hash Algorithm (SHA) SHA-256 digest over a versioned prefix and the node ID. Span IDs come from hashing the node ID with the span kind and a key unique within the PR. The service accepts the collision odds in appendix C of the original design and doesn't check for collisions.
- **Undo:** change the version prefix, which moves every later span to new IDs.
