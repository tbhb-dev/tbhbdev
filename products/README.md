# `products/`

User-facing products and applications live directly under `products/`, one folder per product with no grouping folders. Each product's `README.md` and `OWNERS` describe what the product is, what it depends on, and name its owners.
Products depend on `lib/` only, never on `factory/` internals; the factory never depends on a product.
