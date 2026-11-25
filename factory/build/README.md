# `factory/build`

Bazel macros, Gazelle checks, visibility checks, and language build hooks live here.
`factory/` packages use these build definitions; products depend on `lib/` only, never on factory internals, and the factory never depends on a product.
