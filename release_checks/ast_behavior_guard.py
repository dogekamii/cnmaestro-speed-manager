"""Verify the v1.3 visual source preserves the original v1.1 behavior AST."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "cnmaestro_speed_manager.py"
REFERENCE = ROOT / "release_checks" / "cnmaestro_speed_manager_v1.1.0.py"
TOP_LEVEL_FUNCTIONS = ("initdb", "exactpkg", "nearest", "rates", "age", "save", "cached")
CORE_APP_METHODS = (
    "bg", "notice", "connect", "row", "setbusy", "scan_one", "finish_one",
    "scan_all", "stream", "finish_all", "clear_cache", "suggestion",
    "displaypkg", "filt", "sortkey", "sort", "tree_click", "select_visible",
    "deselect_visible", "clear_selection", "show", "preview_rows",
    "publish_status", "execute", "progress_done", "finish_publish",
    # App.audit now performs visual in-app navigation; its unchanged query/table data
    # behavior is covered by tests/test_inline_tool_views.py.
    # check_updates intentionally differs and is covered by tests/test_updater.py.
    "apply_theme", "load_settings", "vt", "update_result",
)
BEHAVIOR_CONSTANTS = ("PKGS", "OTHER", "PHRASE", "CONCURRENCY", "CACHE_HOURS")


def index(tree: ast.Module) -> tuple[dict[str, ast.AST], dict[str, ast.ClassDef]]:
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    return functions, classes


def methods(node: ast.ClassDef) -> dict[str, ast.AST]:
    return {child.name: child for child in node.body if isinstance(child, ast.FunctionDef)}


def assignments(tree: ast.Module) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value
    return found


def same(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def require_equal(label: str, current: ast.AST | None, reference: ast.AST | None) -> None:
    if current is None or reference is None or not same(current, reference):
        raise AssertionError(f"v1.1 AST behavior guard failed: {label}")


def main() -> None:
    current_tree = ast.parse(CURRENT.read_text(encoding="utf-8"), filename=str(CURRENT))
    reference_tree = ast.parse(REFERENCE.read_text(encoding="utf-8"), filename=str(REFERENCE))
    current_functions, current_classes = index(current_tree)
    reference_functions, reference_classes = index(reference_tree)

    for name in TOP_LEVEL_FUNCTIONS:
        require_equal(f"function {name}", current_functions.get(name), reference_functions.get(name))

    require_equal("API class", current_classes.get("API"), reference_classes.get("API"))

    current_app = methods(current_classes["App"])
    reference_app = methods(reference_classes["App"])
    for name in CORE_APP_METHODS:
        require_equal(f"App.{name}", current_app.get(name), reference_app.get(name))

    current_constants = assignments(current_tree)
    reference_constants = assignments(reference_tree)
    for name in BEHAVIOR_CONSTANTS:
        require_equal(f"constant {name}", current_constants.get(name), reference_constants.get(name))

    print(
        "AST behavior guard passed: original v1.1 API class, "
        f"{len(TOP_LEVEL_FUNCTIONS)} core functions, {len(CORE_APP_METHODS)} App methods, "
        f"and {len(BEHAVIOR_CONSTANTS)} behavior constants are unchanged."
    )


if __name__ == "__main__":
    main()
