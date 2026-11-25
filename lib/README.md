# `lib/`

Shared libraries reusable across the repository live under `lib/`.
Both `products/` and `factory/` may depend on `lib/`. `lib/` must not depend on `factory/` or `products/`. Products depend on `lib/` only and must not use `factory/` internals. The factory must not depend on a product.
