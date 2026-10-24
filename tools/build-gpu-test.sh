#!/bin/bash
# Build the userspace GPU smoke test for the iOS root image.
#
#   WORK=/tmp/liuqin-build3 tools/build-gpu-test.sh [OUT]
#
# The wire ABI header lives in the patched kernel tree (it is added by the patch
# series, not stored twice), so WORK must point at a tree that has already been
# built by tools/build-local.sh.  OUT defaults to shim/out/liuqin-gpu-test.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WORK:-/tmp/liuqin-build3}
ABI=$WORK/xnu/iokit/NativeAdreno
OUT=${1:-$REPO/shim/out/liuqin-gpu-test}

[[ -f $ABI/NativeAdrenoABI.h ]] || {
	echo "error: $ABI/NativeAdrenoABI.h missing; run tools/build-local.sh first (or set WORK)" >&2
	exit 1
}

mkdir -p "$(dirname "$OUT")"
xcrun --sdk iphoneos clang++ -arch arm64 -miphoneos-version-min=14.0 \
	-O2 -Wall -Wextra -fno-common \
	-I "$ABI" \
	-o "$OUT" "$REPO/tools/liuqin-gpu-test.cpp" \
	-framework IOKit -framework CoreFoundation
echo "built $OUT"
file "$OUT"
