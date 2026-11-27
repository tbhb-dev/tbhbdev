# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Go lint tests using the event-gateway configuration."""

load("@tbhb_rules_go//:defs.bzl", "golangci_lint_aspect_factory", "golangci_lint_test_factory")

golangci_lint = golangci_lint_aspect_factory(
    config = Label("//factory/event-gateway:.golangci.yml"),
    go_mod = Label("//factory/event-gateway:go.mod"),
)

golangci_lint_test = golangci_lint_test_factory(golangci_lint)
