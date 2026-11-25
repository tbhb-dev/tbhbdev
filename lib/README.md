# `lib/`

Shared libraries reusable across the repository live under `lib/`.
Both `products/` and `factory/` may depend on `lib/`; `lib/` never depends on `factory/` or `products/`, products depend on `lib/` only (never on factory internals), and the factory never depends on a product.
