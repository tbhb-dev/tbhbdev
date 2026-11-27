# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import ast
import io
import re
import tokenize
from collections import Counter
from typing import TypedDict

import graph
from graph import aliases, prefixes, qualified

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITIONS = (*FUNCTIONS, ast.ClassDef)
NOQA = re.compile(r"#\s*noqa\b(?:\s*:\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*))?", re.I)
EXIT = "sys.exit or SystemExit"
CALLS = {
    "cast": {"cast", "typing.cast"},
    "NewType": {"NewType", "typing.NewType", "typing_extensions.NewType"},
    "print": {"print", "builtins.print"},
    EXIT: {"sys.exit", "SystemExit", "builtins.SystemExit"},
}
ENVIRONMENT = re.compile(r"os\.(environb?|getenvb?|putenv|unsetenv)$")
CLOCK = re.compile(
    r"time\.(time|monotonic|perf_counter|process_time|thread_time)(_ns)?"
    r"|datetime\.(datetime\.(now|utcnow|today)|date\.today)"
)
NETWORK = set(
    "socket socketserver http.client http.server urllib.request httpx requests aiohttp".split()
)
PATCHES = {"monkeypatch.setattr", "unittest.mock.patch", "unittest.mock.patch.object", "patch"}
SOURCE_METRICS = (
    "Any",
    "noqa bare",
    "environment_accesses",
    "subprocess_calls",
    "socket_calls",
    "stderr_writes",
    "except_Exception",
    "dict_str_object_annotations",
    "modules_over_300_lines",
    "initializers_with_statements",
)
TEST_EFFECTS = ("subprocess", "network", "sleep", "clock")


class Report(TypedDict):
    totals: graph.Counts
    modules: dict[str, graph.ModuleMetrics]


def public_names(tree: ast.Module) -> int:
    """Count public definitions and bindings, honoring a literal export list."""
    names = set()
    for node in tree.body:
        if isinstance(node, DEFINITIONS):
            names.add(node.name)
        elif isinstance(node, ast.TypeAlias):
            names.add(node.name.id)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if ast.unparse(target) == "__all__" and isinstance(
                    node.value, (ast.List, ast.Tuple)
                ):
                    return len(
                        {item.value for item in node.value.elts if isinstance(item, ast.Constant)}
                    )
                names.update(
                    item.id
                    for item in ast.walk(target)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
                )
    return sum(not name.startswith("_") for name in names)


def source_counts(text: str, tree: ast.Module, totals: Counter[str]) -> None:
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and (match := NOQA.match(token.string)):
            codes = match[1].upper().split(",") if match[1] else ["bare"]
            totals.update({f"noqa {code.strip()}" for code in codes})

    names = aliases(tree)
    for node in ast.walk(tree):
        name = qualified(node, names)
        totals["Any"] += name in {"Any", "typing.Any", "typing_extensions.Any"}
        totals["environment_accesses"] += ENVIRONMENT.fullmatch(name) is not None
        if isinstance(node, ast.Raise):
            totals[EXIT] += qualified(node.exc, names) in CALLS[EXIT]
        if isinstance(node, ast.Call):
            call = qualified(node.func, names)
            for metric, symbols in CALLS.items():
                totals[metric] += call in symbols
            totals["subprocess_calls"] += call.startswith("subprocess.")
            totals["socket_calls"] += call.startswith("socket.")
            if call in CALLS["print"]:
                file = next((arg.value for arg in node.keywords if arg.arg == "file"), None)
                totals["stderr_writes"] += qualified(file, names) == "sys.stderr"
            totals["stderr_writes"] += call in {"sys.stderr.write", "sys.stderr.writelines"}
        if isinstance(node, ast.ExceptHandler):
            types = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            names_caught = {qualified(item, names) for item in types}
            totals["except_Exception"] += bool(names_caught & {"Exception", "builtins.Exception"})
        if isinstance(node, ast.Subscript) and qualified(node.value, names) in {
            "dict",
            "typing.Dict",
        }:
            if isinstance(node.slice, ast.Tuple):
                types = [qualified(arg, names).rpartition(".")[2] for arg in node.slice.elts]
                totals["dict_str_object_annotations"] += types in (
                    ["str", "object"],
                    ["str", "Any"],
                )


def test_counts(tree: ast.Module, own: set[str], totals: Counter[str]) -> None:
    names = aliases(tree)
    effects = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = qualified(node.func, names)
        if call.startswith("subprocess."):
            effects.add("subprocess")
        if NETWORK.intersection(prefixes(call.rpartition(".")[0])):
            effects.add("network")
        if call in {"time.sleep", "asyncio.sleep"}:
            effects.add("sleep")
        if CLOCK.fullmatch(call):
            effects.add("clock")
        if call in PATCHES and node.args:
            target = node.args[0]
            name = qualified(target, names)
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                name = target.value
            totals["test_patches_of_own_modules"] += bool(own.intersection(prefixes(name)))
    totals.update(f"test_files_calling_{effect}" for effect in effects)


def measure(sources: dict[str, str], tests: dict[str, str]) -> Report:
    trees = {name: ast.parse(text, filename=name) for name, text in sources.items()}
    test_trees = {name: ast.parse(text, filename=name) for name, text in tests.items()}
    modules, cumulative = graph.structure(trees, test_trees)
    totals: Counter[str] = Counter(dict.fromkeys((*CALLS, *SOURCE_METRICS), 0))
    totals.update({f"test_files_calling_{effect}": 0 for effect in TEST_EFFECTS})
    totals["test_patches_of_own_modules"] = 0
    totals["cumulative_component_dependency"] = cumulative
    for name, tree in trees.items():
        lines = len(sources[name].splitlines())
        statements = 0
        if name.endswith(graph.INIT):
            statements = len(tree.body) - (ast.get_docstring(tree) is not None)
        modules[name].update(
            lines=lines,
            functions=sum(isinstance(node, FUNCTIONS) for node in ast.walk(tree)),
            classes=sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree)),
            public_names=public_names(tree),
            initializer_statements=statements,
        )
        totals["modules_over_300_lines"] += lines > graph.LINE_LIMIT
        totals["initializers_with_statements"] += statements > 0
        source_counts(sources[name], tree, totals)
    own = {name.removesuffix(graph.INIT) for name in trees}
    for tree in test_trees.values():
        test_counts(tree, own, totals)
    return {"totals": dict(totals), "modules": modules}
