# `products/`

User-facing products and applications live directly under `products/`, one folder per product with no grouping folders. Each product's `README.md` and `OWNERS` describe what the product is, what it depends on, and list its owners.
Products depend on `lib/` only and must not use `factory/` internals. The factory must not depend on a product.
