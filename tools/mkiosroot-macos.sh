#!/bin/bash
# Build the LIUQIN iPadOS root on macOS: one case-sensitive HFS+ image holding
# an iPadOS OS root plus its cryptexes, for the userdata partition
# (rd=ufsroot0). macOS counterpart of tools/mkiosroot.sh, which needs Linux
# (apfs-fuse + hfsprogs).
#
#   sudo tools/mkiosroot-macos.sh OS.dmg SYSTEMOS.dmg APPOS.dmg OUT.img [SIZE]
#
# The three DMGs come from an iPad8,1 (D321AP) iPadOS 16.x IPSW:
#   OS.dmg        the largest one   (the read-only system root)
#   SYSTEMOS.dmg  ~2.9 GB, volume name ending in "SystemCryptex"
#   APPOS.dmg     ~10 MB,  volume name "AppCryptex"
# The ~100 MB DMGs in the IPSW are encrypted restore images and are not used.
#
# Owners, modes, symlinks and hard links are preserved (rsync -aH --numeric-ids
# as root). The volume is journaled so XNU can replay it after a panic.
# LIUQIN additions, same as the Linux script:
#   - /System/Library/Caches/com.apple.dyld hard-links the SystemOS cryptex
#     shared cache (ignition_level=1 looks there);
#   - every cryptex file with no counterpart in the OS root is hard-linked to
#     its root path (dyld does not search the cryptexes at ignition level 1);
#   - the cryptexes live in /private/preboot/Cryptexes/{OS,App}, which is where
#     the OS root's /System/Cryptexes symlinks point;
#   - logd's stores get owner _logd (272) and .migrated markers, otherwise
#     libtrace panics on first boot;
#   - /private/var/keybags/systembag.kb is the plain-plist system keybag that
#     the built-in AppleKeyStore reads (no passcode).
set -euo pipefail

[[ $# -ge 4 ]] || { sed -n '2,14p' "$0"; exit 2; }
os=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
sysos=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
appos=$(cd "$(dirname "$3")" && pwd)/$(basename "$3")
out=$4
size=${5:-16G}

[[ $(id -u) -eq 0 ]] || { echo "error: run this with sudo (owner/mode preservation)" >&2; exit 1; }
[[ ! -e $out ]] || { echo "refusing to overwrite $out" >&2; exit 1; }
for f in "$os" "$sysos" "$appos"; do
	[[ -f $f ]] || { echo "error: $f missing" >&2; exit 1; }
done

work=$(mktemp -d /tmp/liuqin-root.XXXXXX)
rootdev=""
cleanup() {
	local m
	for m in "$work/root" "$work/os" "$work/sysos" "$work/appos"; do
		if mount | grep -q " on $m "; then
			umount "$m" 2>/dev/null || true
		fi
	done
	[[ -n $rootdev ]] && hdiutil detach "$rootdev" >/dev/null 2>&1 || true
	rmdir "$work"/os "$work"/sysos "$work"/appos "$work"/root "$work" 2>/dev/null || true
}
trap cleanup EXIT

attach_ro() { # DMG MOUNTPOINT
	mkdir -p "$2"
	hdiutil attach -readonly -nobrowse -noverify -mountpoint "$2" "$1" >/dev/null
	echo "mounted $1 -> $2"
}

echo "== mounting IPSW images"
attach_ro "$os" "$work/os"
attach_ro "$sysos" "$work/sysos"
attach_ro "$appos" "$work/appos"

echo "== creating $size HFS+ image $out"
mkfile -n "$size" "$out"
rootdev=$(hdiutil attach -nomount -imagekey diskimage-class=CRawDiskImage "$out" |
	awk '/^\/dev\/disk/{print $1; exit}')
[[ -n $rootdev ]] || { echo "error: could not attach $out" >&2; exit 1; }
newfs_hfs -s -J -v iOSRoot "$rootdev"
mkdir "$work/root"
mount -t hfs "$rootdev" "$work/root"

copy() { # SRC DST
	echo "copying $1 -> ${2#"$work/root"}"
	mkdir -p "$2"
	rsync -aH --numeric-ids "$1/" "$2/"
}

echo "== copying root filesystems"
copy "$work/os" "$work/root"
copy "$work/sysos" "$work/root/private/preboot/Cryptexes/OS"
copy "$work/appos" "$work/root/private/preboot/Cryptexes/App"

# Shared cache where dyld looks at ignition level 1: hard links, no extra space.
dsc="private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld"
mkdir -p "$work/root/System/Library/Caches/com.apple.dyld"
if [[ -d $work/root/$dsc ]]; then
	for f in "$work/root/$dsc"/*; do
		[[ -e $f ]] || continue
		ln "$f" "$work/root/System/Library/Caches/com.apple.dyld/${f##*/}"
	done
fi

# dyld does not search the cryptexes at ignition level 1: hard-link every
# cryptex file that has no counterpart in the OS root.
for c in OS App; do
	echo "== linking $c cryptex files"
	(cd "$work/root/private/preboot/Cryptexes/$c" &&
	 find . ! -type d ! -path "./System/Library/Caches/com.apple.dyld/*") |
	while IFS= read -r f; do
		f=${f#./}
		if [[ ! -e $work/root/$f && ! -L $work/root/$f ]]; then
			mkdir -p "$work/root/$(dirname "$f")"
			src="$work/root/private/preboot/Cryptexes/$c/$f"
			if [[ -L $src ]]; then
				cp -P "$src" "$work/root/$f"
			else
				ln "$src" "$work/root/$f"
			fi
		fi
	done
done

# logd's stores, normally made at restore: owner _logd (272), mode, .migrated.
db="$work/root/private/var/db"
mkdir -p "$db/diagnostics" "$db/uuidtext" "$db/diagnostics/timesync"
chown 272:272 "$db/diagnostics" "$db/uuidtext" "$db/diagnostics/timesync"
chmod 0751 "$db/diagnostics"
chmod 0755 "$db/uuidtext" "$db/diagnostics/timesync"
for d in diagnostics uuidtext diagnostics/timesync; do
	: > "$db/$d/.migrated"
	chown 272:272 "$db/$d/.migrated"
	chmod 0644 "$db/$d/.migrated"
done

# System keybag for the built-in AppleKeyStore (no passcode).
mkdir -p "$work/root/private/var/keybags"
chown 0:0 "$work/root/private/var/keybags"
chmod 0700 "$work/root/private/var/keybags"
cat > "$work/root/private/var/keybags/systembag.kb" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>KeyBagKeys</key><data>U1dFRVQ=</data></dict></plist>
EOF
chown 0:0 "$work/root/private/var/keybags/systembag.kb"
chmod 0600 "$work/root/private/var/keybags/systembag.kb"

df -h "$work/root"
echo "== unmounting"
umount "$work/root"
hdiutil detach "$rootdev" >/dev/null
rootdev=""

# hand the image back to the invoking user so fastboot can read it
if [[ -n ${SUDO_UID:-} ]]; then
	chown "$SUDO_UID:$SUDO_GID" "$out"
	chmod 0644 "$out"
fi

# fsck_hfs needs a device, not a regular file: attach the raw image again.
echo "== fsck"
fsckdev=$(hdiutil attach -nomount -imagekey diskimage-class=CRawDiskImage "$out" |
	awk '/^\/dev\/disk/{print $1; exit}')
if [[ -n $fsckdev ]]; then
	fsck_hfs -f -n "$fsckdev" | tail -3 || true
	hdiutil detach "$fsckdev" >/dev/null 2>&1 || true
else
	echo "warning: could not attach $out for fsck" >&2
fi
echo "done: $out"
