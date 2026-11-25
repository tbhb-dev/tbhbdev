# `products/`

User-facing products and applications live under `products/`, one subdirectory per product.
Products depend on `lib/` only, never on `factory/` internals; the factory never depends on a product.
