from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def load_build_release():
    spec = importlib.util.spec_from_file_location(
        "build_release", PROJECT_ROOT / "scripts" / "build_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_version_carrier_matches_the_version_file():
    version = read("VERSION").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    carriers = {
        "LiveSegmentationLib/version.py": re.search(
            r'^PLUGIN_VERSION = "([^"]+)"',
            read("LiveSegmentation/LiveSegmentationLib/version.py"),
            re.MULTILINE,
        ),
        "server/app/main.py": re.search(
            r'^SERVER_VERSION = "([^"]+)"', read("server/app/main.py"), re.MULTILINE
        ),
        "CITATION.cff": re.search(
            r'^version: "?([^"\s]+)"?', read("CITATION.cff"), re.MULTILINE
        ),
        "CMakeLists.txt": re.search(
            r'set\(EXTENSION_VERSION "([^"]+)"\)', read("CMakeLists.txt")
        ),
    }
    found = {name: match.group(1) if match else None for name, match in carriers.items()}
    assert found == {name: version for name in carriers}


def test_module_self_test_does_not_pin_a_version_literal():
    # "assertEqual(PLUGIN_VERSION, '0.14.6')" stayed in three releases after
    # 0.14.6 and made Slicer's "Reload and Test" fail every time.
    tree = ast.parse(read("LiveSegmentation/LiveSegmentation.py"))
    test_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LiveSegmentationTest"
    )
    literals = [
        node.value
        for node in ast.walk(test_class)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.fullmatch(r"\d+\.\d+\.\d+", node.value)
    ]
    assert literals == []


def test_release_manifest_separates_measured_from_asserted_results(tmp_path):
    build_release = load_build_release()
    manifest = build_release.build_release(tmp_path, "2026-01-01T00:00:00+00:00")
    # Nothing in the manifest may present a check this script never ran as a result.
    assert "validation" not in manifest
    asserted = manifest["maintainer_asserted_validation"]
    assert "not run or verified by build_release.py" in asserted["note"]
    measured = manifest["measured_by_this_script"]
    assert measured["archives_verified"] is True
    assert measured["version_carriers_consistent"] is True
    test_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((PROJECT_ROOT / "server" / "tests").glob("test_*.py"))
    )
    assert measured["test_functions_in_source"] == len(
        re.findall(r"^def test_", test_sources, re.MULTILINE)
    )
