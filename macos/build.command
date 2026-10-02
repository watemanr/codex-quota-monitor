#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ "$(uname -s)" != Darwin ]]; then echo 'This build must run on macOS.' >&2; exit 1; fi
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo 'Install Python 3.12 or later from https://www.python.org/downloads/macos/ and try again.' >&2
  exit 1
fi
"$PYTHON_BIN" -c 'import sys,tkinter; assert (3,12) <= sys.version_info < (3,16), "Python 3.12–3.15 is required"' || {
  echo 'Use a Python 3.12+ installation with Tk, such as the python.org macOS installer.' >&2
  exit 1
}
ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in arm64|x86_64|universal2) ;; *) echo "Unsupported architecture: $ARCH" >&2; exit 1;; esac
SIGN_IDENTITY="${CODE_SIGN_IDENTITY:--}"
VENV="$ROOT/.build-env"
"$PYTHON_BIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install -r "$ROOT/requirements-build.txt"
"$VENV/bin/python" -m unittest discover -s "$ROOT/source" -p 'test_*.py'
"$VENV/bin/python" -m unittest discover -s "$ROOT/../windows/source" -p 'test_*.py'
mkdir -p "$ROOT/dist"
# Building directly in a cloud/FileProvider-synchronised checkout can attach
# FinderInfo to bundle directories and make codesign reject the app. Assemble,
# sign and smoke-test in a local temporary directory, then move verified outputs.
STAGE_ROOT=$(mktemp -d /tmp/codex-quota-macos-build.XXXXXX)
trap '/bin/rm -rf "$STAGE_ROOT"' EXIT
"$VENV/bin/python" "$ROOT/collect_licenses.py" "$STAGE_ROOT/licenses"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean --windowed --onedir \
  --name CodexUsageCollector --osx-bundle-identifier xyz.814305.codex-quota-collector \
  --target-architecture "$ARCH" --codesign-identity "$SIGN_IDENTITY" \
  --paths "$ROOT/../windows/source" --paths "$ROOT/source" \
  --hidden-import AppKit --hidden-import Security --hidden-import UserNotifications \
  --icon "$ROOT/app.icns" \
  --add-data "$STAGE_ROOT/licenses:licenses" \
  --add-data "$ROOT/../windows/source/app.ico:." \
  --distpath "$STAGE_ROOT/dist" --workpath "$STAGE_ROOT/work" --specpath "$STAGE_ROOT/spec" \
  "$ROOT/source/mac_app.py"
APP="$STAGE_ROOT/dist/CodexUsageCollector.app"
"$VENV/bin/python" "$ROOT/finish_bundle.py" "$APP"
# Remove resource forks/Finder metadata copied from source files before the
# final inside-out signature. A real identity must retain hardened runtime and
# a trusted timestamp for later notarization.
/usr/bin/xattr -cr "$APP"
if [[ "$SIGN_IDENTITY" == '-' ]]; then
  /usr/bin/codesign --force --deep --sign - "$APP"
else
  /usr/bin/codesign --force --deep --sign "$SIGN_IDENTITY" --options runtime --timestamp "$APP"
fi
/usr/bin/codesign --verify --deep --strict "$APP"
TEST_DIR="$STAGE_ROOT/smoke-data"
mkdir -p "$TEST_DIR"
# Run the actual .app from Launch Services in the logged-in desktop session.
"$VENV/bin/python" - "$APP" "$TEST_DIR" <<'PY'
import subprocess,sys
subprocess.run(['/usr/bin/open','-n','-W',sys.argv[1],'--args','--smoke-test','--data-dir',sys.argv[2]],check=True,timeout=30)
PY
"$VENV/bin/python" - "$TEST_DIR/smoke-result.json" <<'PY'
import json,sys
with open(sys.argv[1]) as f: result=json.load(f)
assert result['window_created'] and result['menu_bar_created'], result
print('Offline window/menu-bar smoke test passed; no account read and no upload.')
PY
STAGED_ZIP="$STAGE_ROOT/CodexUsageCollector-macOS-$ARCH.zip"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$STAGED_ZIP"
OUT="$ROOT/dist/CodexUsageCollector-macOS-$ARCH.zip"
/bin/mv -f "$STAGED_ZIP" "$OUT"
# A bare .app can acquire FinderInfo after it is placed in a FileProvider
# checkout, invalidating its signature. Publish only the archive created from
# the verified staging app; run.command extracts it to a local temporary path.
/bin/rm -rf "$ROOT/dist/CodexUsageCollector.app"
OUT_NAME="$(basename -- "$OUT")"
(cd "$ROOT/dist" && /usr/bin/shasum -a 256 "$OUT_NAME" > "$OUT_NAME.sha256")
printf '\nReady: %s\nKeep the ZIP intact when transferring the app.\n' "$OUT"
