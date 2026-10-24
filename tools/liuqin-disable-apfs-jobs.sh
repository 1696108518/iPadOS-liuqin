#!/bin/bash
# Move the APFS/disk-arbitration launchd jobs out of the way in a LIUQIN root
# image, so an HFS-only root can get past the early userspace mount attempts.
#   sudo tools/liuqin-disable-apfs-jobs.sh ROOT.img
set -euo pipefail
[[ $# -eq 1 ]] || { echo "usage: sudo $0 ROOT.img" >&2; exit 2; }
img=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
[[ $(id -u) -eq 0 ]] || { echo "error: run with sudo" >&2; exit 1; }
dev=$(hdiutil attach -nomount -imagekey diskimage-class=CRawDiskImage "$img" | awk '/^\/dev\/disk/{print $1; exit}')
[[ -n $dev ]] || { echo "error: cannot attach $img" >&2; exit 1; }
mnt=$(mktemp -d /tmp/liuqin-edit.XXXXXX)
cleanup() { mount | grep -q " on $mnt " && umount "$mnt" 2>/dev/null; hdiutil detach "$dev" >/dev/null 2>&1 || true; rmdir "$mnt" 2>/dev/null || true; }
trap cleanup EXIT
mount -t hfs -o rw "$dev" "$mnt"
mkdir -p "$mnt/private/var/disabled-jobs"
for j in com.apple.filesystems.apfs_iosd com.apple.mobile.storage_mounter \
         com.apple.mobile.storage_mounter_proxy com.apple.diskarbitrationd \
         com.apple.diskimagesiod com.apple.diskimagesiod.ram; do
	f="$mnt/System/Library/LaunchDaemons/$j.plist"
	[[ -f $f ]] && mv "$f" "$mnt/private/var/disabled-jobs/$j.plist" && echo "disabled $j"
done
sync
echo "done; jobs moved to /private/var/disabled-jobs"
