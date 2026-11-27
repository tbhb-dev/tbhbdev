---
base: main
draft: true
labels:
  - enhancement
reviewers:
  - tbhb
assignees:
  - "@me"
---

# feat(cli): add the stamp subcommand

## Why

Running `tool stamp` on a tree with a write-protected file stops partway. The command leaves some files stamped and others not, and prints nothing that says which. See [the issue](https://github.com/tbhb-dev/tbhbdev/issues/12).

## Behavior change

The command now checks every target before it writes one. A write-protected file fails the whole run with an exit code of 1 and a list of the blocked paths. A run that passes the check stamps every file.

## Verification

Ran the new unit tests, then stamped a tree with one write-protected file:

```console
$ go test ./internal/stamp/...
ok  	github.com/tbhb-dev/tbhbdev/internal/stamp	0.412s
$ tool stamp ./fixture
error: 1 file is read-only: fixture/a.txt
```

No file in the tree changed after the failure.
