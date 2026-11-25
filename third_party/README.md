# `third_party/`

Vendored third-party code, schemas, and external patches live under `third_party/`.
Both `lib/` and `factory/` (and `products/` via `lib/` or explicit allowlists) may consume `third_party/`; products depend on `lib/` only, never on factory internals, and the factory never depends on a product.
