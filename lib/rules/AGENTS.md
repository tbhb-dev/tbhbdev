# Agent instructions

Follow the monorepo root's agent instructions. Keep each rules module in its
own directory and run its tests from the root using its Bazel repository
name. Workspaces under `e2e/` have their own module configuration.
