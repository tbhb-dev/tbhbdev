# go.tbhb.dev

The Go vanity import server for `go.tbhb.dev`, a Cloudflare Worker. A module path `go.tbhb.dev/<module>` resolves to `github.com/tbhb-dev/<module>` for the modules listed in [src/vanity.ts](src/vanity.ts).

## Build and test

From the monorepo root, run:

```sh
bazelisk test --config=ci //factory/go-vanity/...
```

The tests use the TypeScript and Cloudflare rules in `lib/rules`. The
[end-to-end test](e2e/check.test.mjs) serves the Worker on a local port.
