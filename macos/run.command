#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ "$(uname -s)" != Darwin ]]; then echo 'This launcher requires macOS.' >&2; exit 1; fi
ZIP="$ROOT/dist/CodexUsageCollector-macOS-$(uname -m).zip"
if [[ -f "$ZIP" ]]; then
  RUN_DIR=$(mktemp -d /tmp/codex-quota-macos-run.XXXXXX)
  trap '/bin/rm -rf "$RUN_DIR"' EXIT
  /usr/bin/ditto -x -k "$ZIP" "$RUN_DIR"
  /usr/bin/codesign --verify --deep --strict "$RUN_DIR/CodexUsageCollector.app"
  /usr/bin/open -n -W "$RUN_DIR/CodexUsageCollector.app"
  exit 0
fi
echo 'Build the application ZIP first: bash macos/build.command' >&2
exit 1
