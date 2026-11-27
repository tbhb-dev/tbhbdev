# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Go lint tests using the github-metrics configuration."""

load("@tbhb_rules_go//:defs.bzl", "golangci_lint_aspect_factory", "golangci_lint_test_factory")

golangci_lint = golangci_lint_aspect_factory(
    config = Label("//factory/github-metrics:.golangci.yml"),
    go_mod = Label("//factory/github-metrics:go.mod"),
)

golangci_lint_test = golangci_lint_test_factory(golangci_lint)
