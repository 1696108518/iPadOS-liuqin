#!/bin/bash
# Apply the liuqin kernel patch series to the exact xnu tag the CI builds.
#
#   tools/verify-patches.sh [WORKDIR]
#
# This is the check to run before spending a GitHub Actions run: it reproduces
# what .github/workflows/xnu.yml does to the source tree (darwin-xnu-build's
# base patches, Apple's libpthread/hfs/BearSSL staged, then our 78 patches in
# order) and fails on the first patch that does not apply.
#
# Needs git and curl. Downloads xnu-8796.141.3 (~15 MB) and the three Apple
# open-source trees; nothing is written outside WORKDIR (default: a temp dir,
# kept on failure so the offending tree can be inspected).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
XNU_TAG=xnu-8796.141.3
DXB_SHA=663906528d1cd5ea8fde7de6f16de73db9511549
WORK=${1:-}
KEEP=${KEEP:-}

if [ -z "$WORK" ]; then
	WORK=$(mktemp -d "${TMPDIR:-/tmp}/liuqin-verify.XXXXXX")
	KEEP=1
fi
echo "work tree: $WORK"

if [ ! -f "$WORK/xnu/.applied-base" ]; then
	mkdir -p "$WORK/xnu"
	echo "== fetching $XNU_TAG"
	curl -fsSL "https://opensource.apple.com/tarballs/xnu/${XNU_TAG}.tar.gz" |
		tar xz -C "$WORK/xnu" --strip-components=1
	cd "$WORK/xnu"
	git init -q .
	git config user.email verify@liuqin.local
	git config user.name liuqin-verify
	git add -A
	git commit -qm "xnu ${XNU_TAG}"
	touch .applied-base

	echo "== fetching darwin-xnu-build $DXB_SHA"
	mkdir -p "$WORK/dxb"
	cd "$WORK/dxb"
	git init -q .
	git fetch -q --depth 1 https://github.com/blacktop/darwin-xnu-build "$DXB_SHA"
	git checkout -q FETCH_HEAD
	for p in "$WORK"/dxb/patches/*.patch; do
		git -C "$WORK/xnu" apply "$p" 2>/dev/null || true
	done
fi

cd "$WORK/xnu"
export MACHINE_CONFIG=LIUQIN
export MACOS_VERSION=13.5
if [ ! -f .applied-stage ]; then
	echo "== staging libpthread / BearSSL / hfs"
	for s in "$REPO"/xnu/patches/*.sh; do
		bash "$s"
	done
	touch .applied-stage
fi

echo "== applying $REPO/xnu/patches/*.patch"
fails=0
for p in "$REPO"/xnu/patches/*.patch; do
	if git apply --check "$p" 2>"$WORK/err"; then
		git apply "$p"
	else
		echo "FAIL $(basename "$p")"
		sed -n '1,3p' "$WORK/err"
		fails=$((fails + 1))
	fi
done

if [ "$fails" -ne 0 ]; then
	echo "$fails patch(es) failed; tree kept at $WORK" >&2
	exit 1
fi

echo "all $(ls "$REPO"/xnu/patches/*.patch | wc -l | tr -d ' ') patches applied cleanly"
[ -n "$KEEP" ] || rm -rf "$WORK"
