# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# cspell:words environb getenvb

import ast
import io
import re
import tokenize
from collections import Counter
from typing import TYPE_CHECKING, TypedDict

from . import graph

if TYPE_CHECKING:
    from collections.abc import Iterator

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
NOQA = re.compile(r"(?i)#\s*noqa\b(?:\s*:\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*))?")
EXIT = "sys.exit or SystemExit"
CALLS = {
    "cast": {"cast", "typing.cast"},
    "NewType": {"NewType", "typing.NewType", "typing_extensions.NewType"},
    "print": {"print", "builtins.print"},
    "stderr_writes": {"sys.stderr.write", "sys.stderr.writelines"},
    EXIT: {"sys.exit", "SystemExit", "builtins.SystemExit"},
}
ENVIRONMENT = re.compile(r"os\.(environb?|getenvb?|(?:put|unset)env)$")
PATCHES = {
    "monkeypatch.setattr", "unittest.mock.patch", "unittest.mock.patch.object", "patch",
}
SOURCE_METRICS = (
    "Any", "noqa bare", "environment_accesses", "subprocess_calls", "socket_calls",
    "except_Exception", "dict_str_object_annotations", "modules_over_300_lines",
    "initializers_with_statements", "test_patches_of_own_modules",
)
TEST_EFFECTS = {
    "subprocess": r"subprocess\..*",
    "network": r"(socket|socketserver|http\.(client|server)|urllib\.request|"
    r"httpx|requests|aiohttp)\..*",
    "sleep": r"(time|asyncio)\.sleep",
    "clock": r"time\.(time|monotonic|perf_counter|process_time|thread_time)(_ns)?"
    r"|datetime\.(datetime\.(now|utcnow|today)|date\.today)",
}
EXCEPTIONS = {"Exception", "builtins.Exception"}
ANY = {"Any", "typing.Any", "typing_extensions.Any"}
TYPE_ALIASES = {"typing.TypeAlias", "typing_extensions.TypeAlias"}
LITERALS = {"typing.Literal", "typing_extensions.Literal"}
ANNOTATED = {"typing.Annotated", "typing_extensions.Annotated"}
MAPPING_VALUES = ANY | {"object", "builtins.object"}
MAPPINGS = {"dict", "builtins.dict", "typing.Dict"} | {
    f"{module}.{name}"
    for module in ("typing", "collections.abc")
    for name in ("Mapping", "MutableMapping")
}


class Report(TypedDict):
    totals: dict[str, int]
    modules: dict[str, graph.ModuleMetrics]


def assigned_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {
        item.id for target in targets for item in ast.walk(target)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
    }


def public_names(tree: ast.Module) -> int:
    public: set[str] = set()
    for node in tree.body:
        match node:
            case ast.TypeAlias(name=ast.Name(id=name)):
                public.add(name)
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                public.add(node.name)
            case ast.Assign() | ast.AnnAssign():
                names = assigned_names(node)
                if "__all__" in names and isinstance(node.value, (ast.List, ast.Tuple)):
                    elts = node.value.elts
                    return len({x.value for x in elts if isinstance(x, ast.Constant)})
                public.update(names)
            case _:
                continue
    return sum(not name.startswith("_") for name in public)


def annotation_roots(node: ast.AST, names: dict[str, str]) -> Iterator[ast.AST]:
    for field in ("annotation", "returns", "bound", "default_value"):
        value: object = getattr(node, field, None)
        if isinstance(value, ast.expr):
            yield value
    if isinstance(node, ast.TypeAlias):
        yield node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None and (
        graph.qualified(node.annotation, names) in TYPE_ALIASES
    ):
        yield node.value


def unquote(node: ast.AST) -> ast.AST:
    while isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value.strip(), mode="eval").body
        except SyntaxError:
            break
    return node


def count_type(node: ast.AST, names: dict[str, str], counts: Counter[str]) -> None:
    node = unquote(node)
    counts["Any"] += graph.qualified(node, names) in ANY
    if isinstance(node, (ast.Attribute, ast.Call)):
        return
    children = ast.iter_child_nodes(node)
    if isinstance(node, ast.Subscript):
        container = graph.qualified(node.value, names)
        if container in LITERALS:
            return
        if isinstance(node.slice, ast.Tuple):
            if container in ANNOTATED:
                children = iter(node.slice.elts[:1])
            elif container in MAPPINGS:
                count_mapping(node.slice, names, counts)
    for child in children:
        count_type(child, names, counts)


def count_mapping(args: ast.Tuple, names: dict[str, str], counts: Counter[str]) -> None:
    match [graph.qualified(unquote(arg), names) for arg in args.elts]:
        case ["str" | "builtins.str", value]:
            counts["dict_str_object_annotations"] += value in MAPPING_VALUES
        case _:
            return


def call_counts(node: ast.Call, names: dict[str, str], totals: Counter[str]) -> None:
    call = graph.qualified(node.func, names)
    for metric, symbols in CALLS.items():
        totals[metric] += call in symbols
    totals["subprocess_calls"] += call.startswith("subprocess.")
    totals["socket_calls"] += call.startswith("socket.")
    if call in CALLS["print"]:
        file = next((arg.value for arg in node.keywords if arg.arg == "file"), None)
        totals["stderr_writes"] += graph.qualified(file, names) == "sys.stderr"


def source_counts(text: str, tree: ast.Module, totals: Counter[str]) -> None:
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and (match := NOQA.match(token.string)):
            codes = match[1].upper().split(",") if match[1] else ["bare"]
            totals.update({f"noqa {code.strip()}" for code in codes})

    names, type_names = graph.aliases(tree.body), graph.aliases(ast.walk(tree))
    for node in ast.walk(tree):
        for root in annotation_roots(node, type_names):
            count_type(root, type_names, totals)
        runtime_counts(node, names, totals)


def runtime_counts(node: ast.AST, names: dict[str, str], totals: Counter[str]) -> None:
    symbol = graph.qualified(node, names)
    totals["environment_accesses"] += bool(ENVIRONMENT.fullmatch(symbol))
    if isinstance(node, ast.Raise):
        totals[EXIT] += graph.qualified(node.exc, names) in CALLS[EXIT]
    elif isinstance(node, ast.Call):
        call_counts(node, names, totals)
    elif isinstance(node, ast.ExceptHandler):
        types = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        symbols = {graph.qualified(item, names) for item in types}
        totals["except_Exception"] += not symbols.isdisjoint(EXCEPTIONS)


def test_counts(tree: ast.Module, own: set[str], totals: Counter[str]) -> None:
    names = graph.aliases(tree.body)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    symbols = {graph.qualified(node.func, names) for node in calls}
    for effect, pattern in TEST_EFFECTS.items():
        key = f"test_files_calling_{effect}"
        totals[key] += any(re.fullmatch(pattern, symbol) for symbol in symbols)
    for node in calls:
        if not node.args or graph.qualified(node.func, names) not in PATCHES:
            continue
        target = node.args[0]
        symbol = graph.qualified(target, names)
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            symbol = target.value
        matched = not own.isdisjoint(graph.prefixes(symbol))
        totals["test_patches_of_own_modules"] += matched


def module_counts(name: str, text: str, tree: ast.Module) -> graph.ModuleMetrics:
    statements = len(tree.body) - (ast.get_docstring(tree) is not None)
    return {
        "lines": len(text.splitlines()),
        "functions": sum(isinstance(node, FUNCTIONS) for node in ast.walk(tree)),
        "classes": sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree)),
        "public_names": public_names(tree),
        "initializer_statements": statements if name.endswith(graph.INIT) else 0,
    }


def measure(sources: dict[str, str], tests: dict[str, str]) -> Report:
    trees = {name: ast.parse(text, filename=name) for name, text in sources.items()}
    test_trees = {name: ast.parse(text, filename=name) for name, text in tests.items()}
    modules, cumulative = graph.structure(trees, test_trees)
    effects = [f"test_files_calling_{effect}" for effect in TEST_EFFECTS]
    totals = Counter(dict.fromkeys((*CALLS, *SOURCE_METRICS, *effects), 0))
    totals["cumulative_component_dependency"] = cumulative
    for name, tree in trees.items():
        metrics = module_counts(name, sources[name], tree)
        modules[name].update(metrics)
        totals["modules_over_300_lines"] += metrics["lines"] > graph.LINE_LIMIT
        totals["initializers_with_statements"] += metrics["initializer_statements"] > 0
        source_counts(sources[name], tree, totals)
    own = {name.removesuffix(graph.INIT) for name in trees}
    for tree in test_trees.values():
        test_counts(tree, own, totals)
    return {"totals": dict(totals), "modules": modules}
