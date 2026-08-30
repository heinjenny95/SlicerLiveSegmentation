# Contributing

Live Segmentation is an independent scripted 3D Slicer extension. Contributions
must preserve interoperability with Slicer's standard Segmentation node and must
not introduce a dependency on a particular segmentation algorithm or UI.

## Development checks

1. Run `scripts/setup.ps1` once.
2. Run `scripts/test.ps1` before every change is proposed.
3. Run the real Slicer smoke test described in `docs/MANUAL_TEST.md` for changes
   to MRML, Segment Editor, presence, backup, or session lifecycle behavior.
4. Never add clinical images, room data, API keys, databases, `.mrb` files, or
   shared-folder test output to the repository.

Changes to a shared-folder or HTTP protocol record must remain additive or include
an explicit migration and compatibility test. Security-sensitive changes require
an entry in `docs/SECURITY.md`.

## Public collaboration

- Report reproducible bugs at
  https://github.com/heinjenny95/SlicerLiveSegmentation/issues.
- Discuss usage and larger design proposals at
  https://github.com/heinjenny95/SlicerLiveSegmentation/discussions.
- Submit changes through a focused pull request against `main`.
- Follow `SECURITY.md` for vulnerabilities; do not disclose sensitive room,
  server, or patient information in a public issue.
