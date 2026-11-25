# `factory/`

Internal factory automation, build tooling, services, work routing, evaluations, and infrastructure live under `factory/`.
Only `factory/` packages depend on `factory/` internals (`lib/` is also allowed); products depend on `lib/` only, never on factory internals, and the factory never depends on a product.
