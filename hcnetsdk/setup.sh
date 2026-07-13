#!/usr/bin/env bash
# Set up the Hikvision HCNetSDK from its Linux64 zip. See README.md in this directory.
#
# Usage: paste EN-HCNetSDK*_linux64.zip into this folder, then run ./hcnetsdk/setup.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"   # ./hcnetsdk/
cd "$DIR"

# 1. Locate the zip.
shopt -s nullglob
zips=(EN-HCNetSDK*_linux64.zip)
shopt -u nullglob
if [ ${#zips[@]} -eq 0 ]; then
  echo "error: no EN-HCNetSDK*_linux64.zip found in $DIR" >&2
  echo "See ./hcnetsdk/README.md for instructions." >&2
  exit 1
fi
if [ ${#zips[@]} -gt 1 ]; then
  echo "error: multiple EN-HCNetSDK*_linux64.zip files found; keep only one:" >&2
  printf '  %s\n' "${zips[@]}" >&2
  exit 1
fi
ZIP="${zips[0]}"

# 2. Extract (a zip; use python's stdlib so no `unzip` dependency).
echo "Extracting ${ZIP} ..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
python3 -m zipfile -e "$ZIP" "$TMP"

# The zip has several lib/ dirs (demos etc.); find the one holding libhcnetsdk.so.
LIBSO="$(find "$TMP" -type f -name libhcnetsdk.so -print -quit)"
if [ -z "$LIBSO" ]; then
  echo "error: libhcnetsdk.so not found inside the zip (unexpected layout)" >&2
  exit 1
fi
LIBDIR="$(dirname "$LIBSO")"      # .../lib
TOP="$(dirname "$LIBDIR")"        # .../ (contains lib/ and incEn/)

# 3. Copy the runtime libs up into this directory, plus the header for reference.
cp -r "$LIBDIR/." "$DIR/"
if [ -f "$TOP/incEn/HCNetSDK.h" ]; then
  cp "$TOP/incEn/HCNetSDK.h" "$DIR/"
fi

# 4. Verify.
if [ -f "$DIR/libhcnetsdk.so" ] && [ -d "$DIR/HCNetSDKCom" ]; then
  n=$(find "$DIR" -maxdepth 2 -name '*.so*' | wc -l)
  echo "✓ HCNetSDK ready: libhcnetsdk.so + HCNetSDKCom/ + ${n} shared libs in place."
  echo "  You can delete the .zip now if you like (it is git-ignored either way)."
else
  echo "✗ setup incomplete — expected libhcnetsdk.so and HCNetSDKCom/" >&2
  exit 1
fi
