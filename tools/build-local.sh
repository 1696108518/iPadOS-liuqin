#!/bin/bash
# Build kernel.<config>.liuqin locally with Xcode 15 only.
#
#   tools/build-local.sh [--clean]
#
# Unlike blacktop/darwin-xnu-build this needs no Homebrew, no sudo and no KDK:
# everything it builds (AvailabilityVersions, libplatform, libfirehose_kernel)
# is built from Apple's open sources into a private fakeroot. Xcode 15 is the
# one hard requirement: its iig generates the DriverKit IORPC dispatch tables
# that xnu-8796 links against (Xcode 16's iig emits an incompatible ABI).
#
# Environment:
#   WORK            build directory (default /tmp/liuqin-build)
#   JOBS            parallel compiles (default: all cores)
#   KERNEL_CONFIG   DEVELOPMENT (default), RELEASE, DEBUG
#   MACHINE_CONFIG  LIUQIN (default)
#   SEED_XNU        copy an already patched tree instead of fetching one
#
# The kernel lands in $WORK/fakeroot/System/Library/Kernels/. Re-running is
# incremental; `--clean` drops build objects and the fakeroot (sources stay).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
XNU_TAG=xnu-8796.141.3
DXB_SHA=663906528d1cd5ea8fde7de6f16de73db9511549
MACOS_VERSION=${MACOS_VERSION:-13.5}
KERNEL_CONFIG=${KERNEL_CONFIG:-DEVELOPMENT}
MACHINE_CONFIG=${MACHINE_CONFIG:-LIUQIN}
ARCH_CONFIG=${ARCH_CONFIG:-ARM64}
WORK=${WORK:-/tmp/liuqin-build}
JOBS=${JOBS:-$(sysctl -n hw.ncpu)}
FAKEROOT="$WORK/fakeroot"
SRC="$WORK/xnu"

log() { printf '\n\033[35m== %s\033[0m\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--clean" ]; then
	rm -rf "$WORK/build" "$FAKEROOT"
	shift
fi

mkdir -p "$WORK" "$FAKEROOT"

# ---------------------------------------------------------------- toolchain
log "toolchain"
[ -n "${DEVELOPER_DIR:-}" ] || DEVELOPER_DIR=$(xcode-select -p 2>/dev/null || true)
[ -d "$DEVELOPER_DIR" ] || die "no Xcode selected; run: sudo xcode-select -s /Applications/Xcode_15.4.app/Contents/Developer"
export DEVELOPER_DIR
export PATH="$DEVELOPER_DIR/Toolchains/XcodeDefault.xctoolchain/usr/bin:$DEVELOPER_DIR/usr/bin:$PATH"
IIG=$(xcrun -find iig 2>/dev/null || true)
[ -x "${IIG:-/nonexistent}" ] || die "iig not found in $DEVELOPER_DIR - Xcode 15.x is required"
MIG=$(xcrun -find mig 2>/dev/null || true)
[ -x "${MIG:-/nonexistent}" ] || die "mig not found in $DEVELOPER_DIR"
SDK=$(xcrun --sdk macosx --show-sdk-path)
# xnu's lldbmacros install runs a `python` shebang; macOS only ships python3
mkdir -p "$WORK/bin"
[ -e "$WORK/bin/python" ] || ln -s "$(xcrun -f python3)" "$WORK/bin/python"
export PATH="$WORK/bin:$PATH"
echo "xcode: $(xcodebuild -version | head -1)"
echo "sdk:   $SDK"
echo "iig:   $IIG"
echo "work:  $WORK"

# ------------------------------------------------------------------- source
if [ ! -f "$SRC/.liuqin-ready" ]; then
	log "fetching $XNU_TAG"
	if [ -n "${SEED_XNU:-}" ]; then
		echo "seeding from $SEED_XNU"
		rm -rf "$SRC"
		cp -a "$SEED_XNU" "$SRC"
	else
		rm -rf "$SRC"
		git clone -q --depth 1 --branch "$XNU_TAG" \
			https://github.com/apple-oss-distributions/xnu.git "$SRC"
		mkdir -p "$WORK/dxb"
		git -C "$WORK/dxb" init -q .
		git -C "$WORK/dxb" fetch -q --depth 1 \
			https://github.com/blacktop/darwin-xnu-build "$DXB_SHA"
		git -C "$WORK/dxb" checkout -q FETCH_HEAD
		for p in "$WORK"/dxb/patches/*.patch; do
			# the base patches are context-sensitive; a stale one is skipped
			git -C "$SRC" apply "$p" 2>/dev/null || true
		done
		( cd "$SRC"
		  export MACHINE_CONFIG MACOS_VERSION
		  for s in "$REPO"/xnu/patches/*.sh; do bash "$s"; done )
		for p in "$REPO"/xnu/patches/*.patch; do
			git -C "$SRC" apply "$p" || die "patch $(basename "$p") failed"
		done
	fi
	touch "$SRC/.liuqin-ready"
fi

# make the tree find the pieces that live in the fakeroot instead of in a KDK
sed -i '' "s|^AVAILABILITY_PL=.*|AVAILABILITY_PL=\"$FAKEROOT/usr/local/libexec/availability.pl\"|" \
	"$SRC/bsd/sys/make_symbol_aliasing.sh"
sed -i '' "s|^LDFLAGS_KERNEL_SDK.*|LDFLAGS_KERNEL_SDK = -L$FAKEROOT/usr/local/lib/kernel|" \
	"$SRC/makedefs/MakeInc.def"
sed -i '' "s|^INCFLAGS_SDK.*= -I\$(SDKROOT)/usr/local/include/kernel|INCFLAGS_SDK = -I$FAKEROOT/usr/local/include/kernel|" \
	"$SRC/makedefs/MakeInc.def"
grep -q "INCFLAGS_SDK = -I$FAKEROOT" "$SRC/makedefs/MakeInc.def" || die "could not redirect INCFLAGS_SDK"

# ------------------------------------------------------- host build helpers
if [ ! -x "$FAKEROOT/usr/local/libexec/availability.pl" ]; then
	log "AvailabilityVersions"
	AV="$WORK/AvailabilityVersions"
	[ -d "$AV" ] || git clone -q --depth 1 --branch AvailabilityVersions-121 \
		https://github.com/apple-oss-distributions/AvailabilityVersions.git "$AV"
	make -C "$AV" install DSTROOT="$FAKEROOT" >/dev/null
fi

if [ ! -f "$FAKEROOT/usr/local/include/os/base.h" ]; then
	log "libplatform headers"
	LP="$WORK/libplatform"
	[ -d "$LP" ] || git clone -q --depth 1 --branch libplatform-292.120.1.0.1 \
		https://github.com/apple-oss-distributions/libplatform.git "$LP"
	ditto "$LP/include" "$FAKEROOT/usr/local/include"
	ditto "$LP/private" "$FAKEROOT/usr/local/include"
fi

# ------------------------------------------------------------ xnu headers
if [ ! -f "$FAKEROOT/.xnu-hdrs" ]; then
	log "xnu installhdrs"
	make -C "$SRC" installhdrs -j"$JOBS" \
		SDKROOT=macosx ARCH_CONFIGS="$ARCH_CONFIG" \
		TARGET_CONFIGS="$KERNEL_CONFIG $ARCH_CONFIG $MACHINE_CONFIG" \
		OBJROOT="$WORK/build/hdrs.obj" SYMROOT="$WORK/build/hdrs.sym" \
		DSTROOT="$FAKEROOT" FAKEROOT_DIR="$FAKEROOT" BUILD_WERROR=0
	touch "$FAKEROOT/.xnu-hdrs"
fi

# --------------------------------------------------- libfirehose_kernel.a
# xcodebuild is deliberately not used: a fresh Xcode install that never ran
# `xcodebuild -runFirstLaunch` (which needs sudo) cannot load its IDE plugins.
# libfirehose_kernel is a single C file, so compile it the way its xcconfig
# would: kernel headers from the fakeroot, plain arm64 (it used to ship arm64e).
if [ ! -f "$FAKEROOT/usr/local/lib/kernel/libfirehose_kernel.a" ]; then
	log "libfirehose_kernel (libdispatch)"
	LD="$WORK/libdispatch"
	[ -d "$LD" ] || git clone -q --depth 1 --branch libdispatch-1415.140.2 \
		https://github.com/apple-oss-distributions/libdispatch.git "$LD"
	mkdir -p "$FAKEROOT/usr/local/include/kernel/os" \
		"$FAKEROOT/usr/local/lib/kernel" "$WORK/build/firehose.obj"
	KFW="$FAKEROOT/System/Library/Frameworks/Kernel.framework/Versions/A"
	# libdispatch's xcconfig installs this header unifdefed with OS_FIREHOSE_SPI=1
	# unifdef exits 1 when it removed conditionals - that is the expected result
	set +e
	"$(xcrun -find unifdef)" -DKERNEL=1 -DOS_FIREHOSE_SPI=1 \
		-DOS_VOUCHER_ACTIVITY_SPI_TYPES=1 -UOS_VOUCHER_ACTIVITY_SPI \
		"$LD/os/firehose_buffer_private.h" \
		> "$FAKEROOT/usr/local/include/kernel/os/firehose_buffer_private.h"
	unifdef_rc=$?
	set -e
	[ "$unifdef_rc" -le 1 ] || die "unifdef failed ($unifdef_rc)"
	# -mkernel does not define KERNEL, and panic() lives behind #ifdef KERNEL
	clang -c "$LD/src/firehose/firehose_buffer.c" \
		-o "$WORK/build/firehose.obj/firehose_buffer.o" \
		-target arm64-apple-macos11 -mkernel -O2 -nostdinc -Wno-packed -fno-builtin \
		-DKERNEL=1 -DDISPATCH_USE_DTRACE=0 \
		-DOS_ATOMIC_CONFIG_MEMORY_ORDER_DEPENDENCY=1 \
		-DOS_ATOMIC_CONFIG_STARVATION_FREE_ONLY=0 \
		-I"$LD" -I"$KFW/PrivateHeaders" -I"$KFW/Headers" \
		-I"$FAKEROOT/usr/local/include" -I"$FAKEROOT/usr/local/include/firehose" \
		-isystem "$SDK/usr/include" -isysroot "$SDK"
	ar rcs "$FAKEROOT/usr/local/lib/kernel/libfirehose_kernel.a" \
		"$WORK/build/firehose.obj/firehose_buffer.o"
fi

# ------------------------------------------------------------------ kernel
# Pin the date and object path newvers.pl stamps into `uname -v` so the banner
# names the source tag instead of the build account and wall clock.
KERNEL_BUILD_DATE=${KERNEL_BUILD_DATE:-$(TZ=UTC git -C "$SRC" log -1 --format=%cd --date=format-local:'%a %b %e %T %Z %Y')}
KERNEL_BUILD_OBJROOT=${KERNEL_BUILD_OBJROOT:-$XNU_TAG}
export KERNEL_BUILD_DATE KERNEL_BUILD_OBJROOT
log "building kernel.$KERNEL_CONFIG.$(echo "$MACHINE_CONFIG" | tr 'A-Z' 'a-z')"
( cd "$SRC"
  make install -j"$JOBS" \
	SDKROOT=macosx \
	TARGET_CONFIGS="$KERNEL_CONFIG $ARCH_CONFIG $MACHINE_CONFIG" \
	CONCISE=1 LOGCOLORS=y BUILD_WERROR=0 BUILD_LTO=0 DO_CTFMERGE=0 BUILD_DSYM=0 \
	OBJROOT="$WORK/build/xnu.obj" SYMROOT="$WORK/build/xnu.sym" \
	DSTROOT="$FAKEROOT" FAKEROOT_DIR="$FAKEROOT" )

KERNEL="$FAKEROOT/System/Library/Kernels/kernel.$(echo "$KERNEL_CONFIG" | tr 'A-Z' 'a-z').$(echo "$MACHINE_CONFIG" | tr 'A-Z' 'a-z')"
[ -f "$KERNEL" ] || die "build finished but $KERNEL is missing"
echo
ls -lh "$KERNEL"
file "$KERNEL"
