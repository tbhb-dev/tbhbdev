# `factory/`

Internal factory projects live directly under `factory/`, one folder per project (`factory/<project>/`, such as `factory/tbhbdev/`) with no grouping folders. Each project's `README.md` and `OWNERS` describe what the project is, what it depends on, and name its owners.
Only `factory/` packages depend on `factory/` internals (`lib/` is also allowed); products depend on `lib/` only, never on `factory/` internals, and the factory never depends on a product.
