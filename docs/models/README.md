# Models

`docs/models/` holds formal and semi-formal models of the factory's own systems. Each model has machine-checked verification and lives directly in `docs/models/<model>/`, without grouping folders. Like the rest of `docs/`, a model takes its dependencies from rules in `lib/` and stays independent of `factory/` and `products/` code.

The rules modules and first model files are still arriving. This README sets the layout and targets for that work.

## Model layout

Each model uses the parts it needs.

| Path | Contents |
| --- | --- |
| `README.md` | Scope, method, limits, and status. |
| `tree/` | A goal-function tree with one JSON file per phase or shared fragment. The goal-function tree rules check each file against a CUE schema. |
| `fault-tree.json`, `state-variables.json` | The fault tree and the state variable registry, if the model uses them. |
| `specs/tla/` | A TLA+ specification and one `.cfg` file per configuration. |
| `specs/alloy/` | Alloy models. |
| `analyses/` | System-Theoretic Process Analysis (STPA), Failure Mode and Effects Analysis (FMEA), adversarial cases, the assurance case, and the findings log as JSON. |
| `testdata/` | Inputs reserved for tests, such as sample event logs. |
| `BUILD.bazel` | One test per check. A test for a seeded weakness requires the checker to find the expected counterexample. |

The data files are the source for every generated view. Bazel targets produce reports, test plans, and a readable HTML page from those files. Keep generated views out of Git.

## Add a model

Create `docs/models/<name>/` with a `README.md` and the parts the model uses. Add its `BUILD.bazel`. When the model has a Bazel package, wire its `repo_files` into the parent package. Add the model to the index.

## Index

| Model | System |
| --- | --- |
| [`pr-loop/`](pr-loop/README.md) | Autonomous pull request loop |
