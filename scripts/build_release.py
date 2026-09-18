from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_ZIP_TIME = (2026, 8, 28, 0, 0, 0)
FORBIDDEN_PARTS = {
    ".codex-docs",
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "data",
    "dist",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".nii",
    ".nrrd",
    ".pyc",
    ".sqlite",
    ".sqlite3",
}
ROOT_FILES = (
    ".dockerignore",
    ".editorconfig",
    ".gitignore",
    "CHANGELOG.md",
    "CMakeLists.txt",
    "CITATION.cff",
    "CONTRIBUTING.md",
    "Install-LiveSegmentation.cmd",
    "Install-LiveSegmentation.ps1",
    "LICENSE",
    "README.md",
    "RELEASE_NOTES.md",
    "SECURITY.md",
    "VERSION",
    "docker-compose.yml",
    "pyproject.toml",
)
FULL_DIRECTORIES = (
    ".github",
    "deploy",
    "LiveSegmentation",
    "docs",
    "scripts",
    "server",
)
MODULE_FILES = (
    "docs/SSH_FOLDER.md",
    "CMakeLists.txt",
    "CITATION.cff",
    "Install-LiveSegmentation.cmd",
    "Install-LiveSegmentation.ps1",
    "LICENSE",
    "README.md",
    "RELEASE_NOTES.md",
    "SECURITY.md",
    "VERSION",
)


def version() -> str:
    return (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()


VERSION_CARRIERS = (
    ("LiveSegmentation/LiveSegmentationLib/version.py", r'^PLUGIN_VERSION = "([^"]+)"'),
    ("server/app/main.py", r'^SERVER_VERSION = "([^"]+)"'),
    ("CITATION.cff", r'^version: "?([^"\s]+)"?'),
    ("CMakeLists.txt", r'set\(EXTENSION_VERSION "([^"]+)"\)'),
)


def check_version_consistency() -> None:
    """Refuse to build a release whose version carriers disagree."""
    expected = version()
    mismatches = []
    for relative, pattern in VERSION_CARRIERS:
        match = re.search(
            pattern, (PROJECT_ROOT / relative).read_text(encoding="utf-8"), re.MULTILINE
        )
        found = match.group(1) if match else None
        if found != expected:
            mismatches.append(f"{relative}: {found!r}")
    # On a tag build the tag itself is a version carrier.
    if os.getenv("GITHUB_REF_TYPE") == "tag":
        tag = os.getenv("GITHUB_REF_NAME", "")
        if tag != f"v{expected}":
            mismatches.append(f"git tag: {tag!r}")
    if mismatches:
        raise RuntimeError(
            f"VERSION says {expected!r} but these carriers disagree: " + ", ".join(mismatches)
        )


def count_test_functions() -> int:
    return sum(
        len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), re.MULTILINE))
        for path in sorted((PROJECT_ROOT / "server" / "tests").glob("test_*.py"))
    )


def is_allowed(path: Path) -> bool:
    relative = path.relative_to(PROJECT_ROOT)
    if any(part in FORBIDDEN_PARTS for part in relative.parts):
        return False
    lower_name = path.name.lower()
    if lower_name == ".env" or lower_name.endswith((".seg.nrrd", ".nii.gz", ".result.json", ".log")):
        return False
    return path.suffix.lower() not in FORBIDDEN_SUFFIXES


def collect_tree(directory: str) -> list[Path]:
    root = PROJECT_ROOT / directory
    return sorted(path for path in root.rglob("*") if path.is_file() and is_allowed(path))


def archive_entries(module_only: bool) -> list[Path]:
    files = [PROJECT_ROOT / name for name in (MODULE_FILES if module_only else ROOT_FILES)]
    directories = ("LiveSegmentation",) if module_only else FULL_DIRECTORIES
    for directory in directories:
        files.extend(collect_tree(directory))
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise RuntimeError(f"Release allowlist contains missing files: {missing}")
    return sorted(set(files))


def write_deterministic_zip(destination: Path, files: list[Path], root_name: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            info = zipfile.ZipInfo(f"{root_name}/{relative}", FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.suffix in {".ps1", ".py"} else 0o644) << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise RuntimeError(f"Corrupt ZIP member: {bad_member}")
        names = archive.namelist()
        if not names or any(name.startswith(("/", "\\")) or ".." in Path(name).parts for name in names):
            raise RuntimeError(f"Unsafe or empty ZIP archive: {path.name}")
        lowered = [name.lower() for name in names]
        forbidden_markers = ("/.git/", "/.venv/", "/data/", "__pycache__", ".sqlite3")
        if any(marker in name for name in lowered for marker in forbidden_markers):
            raise RuntimeError(f"Forbidden content detected in {path.name}")


def build_release(output_dir: Path, generated_at: str | None = None) -> dict:
    check_version_consistency()
    release_version = version()
    output_dir.mkdir(parents=True, exist_ok=True)
    root_name = f"SlicerLiveSegmentation-{release_version}"
    module_zip = output_dir / f"SlicerLiveSegmentation-module-{release_version}.zip"
    source_zip = output_dir / f"SlicerLiveSegmentation-source-{release_version}.zip"
    write_deterministic_zip(module_zip, archive_entries(module_only=True), root_name)
    write_deterministic_zip(source_zip, archive_entries(module_only=False), root_name)
    verify_archive(module_zip)
    verify_archive(source_zip)

    artifacts = []
    for path, purpose in (
        (module_zip, "Slicer extension module and installation documentation"),
        (source_zip, "Complete source including server, tests, Docker and documentation"),
    ):
        checksum = sha256(path)
        checksum_path = path.with_name(path.name + ".sha256")
        checksum_path.write_text(f"{checksum}  {path.name}\n", encoding="utf-8", newline="\n")
        artifacts.append(
            {
                "file": path.name,
                "purpose": purpose,
                "size_bytes": path.stat().st_size,
                "sha256": checksum,
                "checksum_file": checksum_path.name,
            }
        )

    release_notes = output_dir / f"RELEASE_NOTES-{release_version}.md"
    shutil.copyfile(PROJECT_ROOT / "RELEASE_NOTES.md", release_notes)
    manifest = {
        "schema_version": 1,
        "name": "Slicer Live Segmentation",
        "version": release_version,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "repository_url": "https://github.com/heinjenny95/SlicerLiveSegmentation",
        "artifacts": artifacts,
        "measured_by_this_script": {
            "archives_verified": True,
            "version_carriers_consistent": True,
            "test_functions_in_source": count_test_functions(),
        },
        "maintainer_asserted_validation": {
            "note": (
                "Statements written by the maintainer for this release. They are "
                "not run or verified by build_release.py; the CI run of the release "
                "commit is the evidence for ruff, compileall and pytest."
            ),
            "ssh_transport": "passed-isolated-local-worker-stalled-import-cancellation-password-first-stage-diagnostics-encrypted-two-clients-slicer-safe-start-rejoin; real-deployment-login-pending",
            "live_server_health": "passed",
            "slicer_5_12_3_smoke_test": (
                "passed-realtime-lanes-optimistic-chat-explicit-label-selection-editable-"
                "backups-history-chunked-snapshot-conflict-review-diagnostics-"
                "fast-connection-reset-label-deletion-exact-rejoin-"
                "nonblocking-startup-shutdown-two-stage-latency-watchdog-"
                "safe-recent-folder-history-direct-lan-fallback-spatial-comments-"
                "quality-benchmark-review-comparison-collaborative-undo-metrics-"
                "two-computer-preflight-public-https-policy-nas-presence-stale-cache-recovery-"
                "global-label-identity-explicit-metadata-own-activity-visible-address-controls-"
                "deferred-vtk-events-native-global-id-preservation-editor-switch-stability-"
                "native-source-event-id-overlap-safe-labelmaps-consecutive-label-add-"
                "participant-registry-throttled-recovery-scans-large-backup-safety-"
                "recovery-journal-file-lock-resilience-event-driven-revision-probes-"
                "revision-aware-settle-verification-journal-write-coalescing-bounded-catchup-"
                "exclusive-voxel-ownership-lock-selector-rename-compact-sequence-watermark-"
                "bounded-writer-history-cooperative-64-cubed-remote-apply-"
                "background-packed-decode-bounded-local-encode-async-journal-"
                "384-cubed-qt-heartbeat-stalled-storage-immediate-delete-echo"
            ),
            "two_process_slicer_live_sync_test": (
                "passed-two-slicer-256-cubed-bidirectional-rapid-three-component-"
                "same-label-convergence-cross-label-name-color-id-isolation-"
                "exclusive-overlap-convergence-bidirectional-presence"
            ),
            "docker_runtime_test": "pending-no-local-docker-installation",
            "contains_patient_data": False,
            "contains_local_database": False,
            "contains_api_keys": False,
        },
    }
    manifest_path = output_dir / f"SlicerLiveSegmentation-{release_version}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic Live Segmentation releases")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "dist")
    parser.add_argument("--generated-at", default=os.getenv("SOURCE_DATE_ISO"))
    args = parser.parse_args()
    manifest = build_release(args.output.resolve(), args.generated_at)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
