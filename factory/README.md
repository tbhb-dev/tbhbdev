# `factory/`

Internal factory projects live directly under `factory/`, one folder per project (`factory/<project>/`, such as `factory/tbhbdev/`) with no grouping folders. Each project's `README.md` and `OWNERS` describe what the project is, what it depends on, and list its owners.
Only `factory/` packages depend on `factory/` internals. `lib/` must not depend on `factory/`. Products depend on `lib/` only and must not use `factory/` internals. The factory must not depend on a product.
