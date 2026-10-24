#!/bin/bash
# Build the shim and wrap it in a boot.img v2 that mirrors the stock liuqin header.
#
# Toolchain selection, in order:
#   1. $CROSS_COMPILE / $CC / $LD from the environment (aarch64-linux-gnu-* on
#      Debian/Ubuntu, or any cross LLVM)
#   2. aarch64-linux-gnu-* if it is on PATH
#   3. clang -target aarch64-none-elf to compile plus any LLD found in
#      $LLD / `zig ld.lld` (pip install ziglang) to link -- this is the macOS
#      path, since Apple's Command Line Tools ship no ELF linker or objcopy.
set -euo pipefail
cd "$(dirname "$0")"
TOOLS=$(cd ../tools && pwd)

CROSS="${CROSS_COMPILE:-}"
if [ -z "$CROSS" ] && command -v aarch64-linux-gnu-gcc >/dev/null; then
	CROSS=aarch64-linux-gnu-
fi

# -fno-jump-tables: clang otherwise emits switch jump tables full of link-time
# absolute addresses, which are wrong once ABL loads the image anywhere.
CFLAGS="-O2 -ffreestanding -nostdlib -fno-pic -fno-jump-tables -mgeneral-regs-only -mstrict-align -fno-stack-protector -Wall -Wextra"
OBJS="start shim console mmu fdt loader ufs libc"
mkdir -p out

compile() { # FILE
	if [ -n "$CROSS" ]; then
		${CC:-${CROSS}gcc} $CFLAGS -c "$1" -o "out/${1%.*}.o"
	else
		clang --target=aarch64-none-elf $CFLAGS -c "$1" -o "out/${1%.*}.o"
	fi
}

link() { # -> out/shim.elf
	if [ -n "$CROSS" ]; then
		${CC:-${CROSS}gcc} $CFLAGS -static -T shim.ld -Wl,--build-id=none -Wl,-N \
			$(for o in $OBJS; do echo "out/$o.o"; done) -o out/shim.elf
	elif [ -n "${LLD:-}" ]; then
		"$LLD" -m aarch64elf -static -T shim.ld --build-id=none \
			$(for o in $OBJS; do echo "out/$o.o"; done) -o out/shim.elf
	else
		echo "error: no ELF linker. Set LLD=/path/to/ld.lld or install zig" \
			"(python3 -m pip install --target ../.tools/ziglang ziglang)." >&2
		echo "       On Linux, install gcc-aarch64-linux-gnu instead." >&2
		exit 1
	fi
}

for f in start.S shim.c console.c mmu.c fdt.c loader.c ufs.c libc.c; do
	compile "$f"
done

link
python3 "$TOOLS/elf2bin.py" out/shim.elf out/Image --reloc-check

# Ramdisk: an xnu payload (tools/mkpayload.py) if given as $1, else an empty
# newc cpio (ABL dislikes a zero-size ramdisk on some builds) -> diagnostics.
if [ -n "${1:-}" ]; then
	cp "$1" out/ramdisk.bin
else
	(cd out && rm -rf rd && mkdir rd && cd rd && find . | cpio -o -H newc --quiet | gzip -9 > ../ramdisk.bin)
fi

MKBOOTIMG=${MKBOOTIMG:-}
if [ -z "$MKBOOTIMG" ] && command -v mkbootimg >/dev/null; then
	MKBOOTIMG=mkbootimg
fi
# The payload ramdisk is ~17 MB, so the stock 0x1f00000 dtb address would
# overlap it; 0x3000000 starts beyond it (both are offsets from the DDR base).
DTB=${DTB:-../dtb/boot_unpacked/dtb}
[ -f "$DTB" ] || { echo "error: $DTB missing; unpack the stock boot.img (README step 0)" >&2; exit 1; }

if [ -n "$MKBOOTIMG" ]; then
	$MKBOOTIMG --header_version 2 --kernel out/Image --ramdisk out/ramdisk.bin \
		--dtb "$DTB" --base 0 --kernel_offset 0x8000 --ramdisk_offset 0x1000000 \
		--tags_offset 0x100 --dtb_offset 0x3000000 --pagesize 4096 \
		--os_version 17.0.0 --os_patch_level 2035-06 \
		--cmdline "console=null" -o out/shim-boot.img
else
	python3 "$TOOLS/mkbootimg.py" --header_version 2 --kernel out/Image --ramdisk out/ramdisk.bin \
		--dtb "$DTB" --base 0 --kernel_offset 0x8000 --ramdisk_offset 0x1000000 \
		--tags_offset 0x100 --dtb_offset 0x3000000 --pagesize 4096 \
		--os_version 17.0.0 --os_patch_level 2035-06 \
		--cmdline "console=null" -o out/shim-boot.img
fi

# Host render of the console layout (needs a native compiler + sizeof(void*)
# independent font math; it only exercises console.c's geometry).
gcc -O1 -Wall -o out/host_test console.c host_test.c 2>/dev/null && ./out/host_test || \
	echo "note: host_test skipped (no native gcc)" >&2
ls -l out/Image out/shim-boot.img
