# `factory/services`

Internal factory services, workers, and control-plane daemons live here.
Other `factory/` packages may depend on `factory/services`; products depend on `lib/` only, never on factory internals, and the factory never depends on a product.
