#!/usr/bin/env python3
"""Guard v1.1 operational behavior while allowing visual-shell changes."""
from __future__ import annotations
import ast
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "cnmaestro_speed_manager.py"
BASELINE = "2468e85:cnmaestro_speed_manager.py"
MODULE_FUNCTIONS = ("initdb", "exactpkg", "nearest", "rates", "age", "save", "cached")
CONSTANTS = ("APP_VERSION", "APP_DIR", "UPDATE_CONFIG", "DATA", "DB", "SETTINGS", "OTHER", "PKGS", "CONCURRENCY", "CACHE_HOURS", "PHRASE")
APP_BEHAVIOR_METHODS = (
    "bg", "notice", "connect", "row", "setbusy", "scan_one", "finish_one",
    "scan_all", "stream", "finish_all", "clear_cache", "suggestion",
    "displaypkg", "filt", "sortkey", "sort", "tree_click", "select_visible",
    "deselect_visible", "clear_selection", "show", "preview_rows",
    "publish_status", "execute", "progress_done", "finish_publish", "audit",
    "apply_theme", "load_settings", "vt", "check_updates", "update_result",
)

def dumped(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)

def class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)

def function_nodes(nodes: list[ast.stmt]):
    return {n.name: n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

def assignment_nodes(tree: ast.Module):
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node
    return result

def main() -> int:
    baseline_text = subprocess.check_output(["git", "-C", str(ROOT), "show", BASELINE], text=True)
    current = ast.parse(SOURCE.read_text(encoding="utf-8"));baseline = ast.parse(baseline_text);failures = []
    current_functions = function_nodes(current.body);baseline_functions = function_nodes(baseline.body)
    for name in MODULE_FUNCTIONS:
        if dumped(current_functions[name]) != dumped(baseline_functions[name]):failures.append(f"module function changed: {name}")
    if dumped(class_node(current, "API")) != dumped(class_node(baseline, "API")):failures.append("API class changed")
    current_app = function_nodes(class_node(current, "App").body);baseline_app = function_nodes(class_node(baseline, "App").body)
    for name in APP_BEHAVIOR_METHODS:
        if dumped(current_app[name]) != dumped(baseline_app[name]):failures.append(f"App behavior method changed: {name}")
    current_assigns = assignment_nodes(current);baseline_assigns = assignment_nodes(baseline)
    for name in CONSTANTS:
        if dumped(current_assigns[name]) != dumped(baseline_assigns[name]):failures.append(f"constant/package definition changed: {name}")
    if failures:
        print("v1.1 behavior guard FAILED");print("\n".join(f"- {failure}" for failure in failures));return 1
    print("v1.1 behavior guard PASSED")
    print(f"- API class: exact AST match")
    print(f"- Module behavior functions: {len(MODULE_FUNCTIONS)} exact AST matches")
    print(f"- App operational methods: {len(APP_BEHAVIOR_METHODS)} exact AST matches")
    print(f"- Constants/package definitions: {len(CONSTANTS)} exact AST matches")
    print("- Visual-only methods and render presentation adapter intentionally excluded")
    return 0

if __name__ == "__main__":raise SystemExit(main())
