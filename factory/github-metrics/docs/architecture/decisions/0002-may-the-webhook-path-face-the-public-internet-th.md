# D2. May the webhook path face the public internet through a Cloudflare Tunnel at `github-metrics.tbhb.dev`, and may the image be public?

- **Status:** accepted by the operator, 2026-11-14.
- **Rule:** the operator decided that the tunnel routes `POST /webhook`. The edge answers `404` to every other path. The tunnel makes outbound connections and doesn’t accept inbound connections. The service checks each request’s signature before it parses the body.
- **Undo:** remove the tunnel route and the DNS record, and unpublish or delete the image package.
