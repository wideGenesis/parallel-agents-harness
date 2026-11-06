#!/usr/bin/env bash
# The project setup step of the harness CI template: runs before `h check`.
# A real project installs here what its tests need beyond the stack toolchains.
set -euo pipefail
echo "POLYGON_SETUP=done" >> "$GITHUB_ENV"
echo "polygon setup ran"
