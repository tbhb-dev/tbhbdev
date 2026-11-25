# `factory/`

Internal factory projects live directly under `factory/`, one folder per project (`factory/<project>/`, such as `factory/tbhbdev/`), with each project recording its role in its own `README.md` and `OWNERS` rather than in the path.
Only `factory/` packages depend on `factory/` internals (`lib/` is also allowed); products depend on `lib/` only, never on `factory/` internals, and the factory never depends on a product.
