#!/usr/bin/env python3
"""Pack a shim + payload into an Android boot image v2, without mkbootimg.

The stock liuqin boot.img is a v2 image (header_version 2, a DTB appended at
dtb_offset); ABL only accepts that shape, and `fastboot boot` uses it as-is.
This is a drop-in replacement for the Android `mkbootimg` invocation in
shim/build.sh, so the boot image can be built on macOS where the AOSP tools
are not installed.

  mkbootimg.py --kernel Image --ramdisk ramdisk.bin --dtb stock.dtb \
               --base 0 --kernel_offset 0x8000 --ramdisk_offset 0x1000000 \
               --tags_offset 0x100 --dtb_offset 0x1f00000 --pagesize 4096 \
               --os_version 17.0.0 --os_patch_level 2035-06 \
               --cmdline "console=null" -o shim-boot.img
"""
import argparse, hashlib, os, struct, sys

BOOT_MAGIC = b'ANDROID!'
BOOT_NAME_SIZE = 16
BOOT_ARGS_SIZE = 512
BOOT_EXTRA_ARGS_SIZE = 1024
# boot_img_hdr_v2 is boot_img_hdr (1632) + recovery_dtbo_size/offset + header_size
# (v1, 1648) + dtb_size/dtb_addr (v2): 1660 bytes, as AOSP mkbootimg writes it.
V2_HEADER_SIZE = 1660
PAGE = 4096


def os_version(s):
    major, minor, patch = (int(x) for x in s.split('.'))
    return (major << 14) | (minor << 7) | patch


def os_patch_level(s):
    year, month = (int(x) for x in s.split('-'))
    return ((year - 2000) << 4) | month


def pad(data, page):
    return data + b'\0' * (-len(data) % page)


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument('--kernel')
    p.add_argument('--ramdisk')
    p.add_argument('--second')
    p.add_argument('--dtb')
    p.add_argument('--output', '-o')
    p.add_argument('--base', type=lambda s: int(s, 0), default=0)
    p.add_argument('--kernel_offset', type=lambda s: int(s, 0), default=0x8000)
    p.add_argument('--ramdisk_offset', type=lambda s: int(s, 0), default=0x1000000)
    p.add_argument('--second_offset', type=lambda s: int(s, 0), default=0xf00000)
    p.add_argument('--tags_offset', type=lambda s: int(s, 0), default=0x100)
    p.add_argument('--dtb_offset', type=lambda s: int(s, 0), default=0x1f00000)
    p.add_argument('--pagesize', type=lambda s: int(s, 0), default=PAGE)
    p.add_argument('--header_version', type=int, default=2)
    p.add_argument('--os_version', default='17.0.0')
    p.add_argument('--os_patch_level', default='2035-06')
    p.add_argument('--cmdline', default='')
    p.add_argument('--name', default='')
    return p.parse_args(argv)


def main(argv):
    a = parse_args(argv)
    page = a.pagesize
    kernel = open(a.kernel, 'rb').read()
    ramdisk = open(a.ramdisk, 'rb').read() if a.ramdisk else b''
    second = open(a.second, 'rb').read() if a.second else b''
    dtb = open(a.dtb, 'rb').read() if a.dtb else b''

    if a.header_version != 2:
        raise SystemExit('only header_version 2 is implemented (liuqin stock image)')
    if not kernel:
        raise SystemExit('empty kernel')
    if len(a.cmdline) > BOOT_ARGS_SIZE - 1:
        raise SystemExit(f'cmdline longer than {BOOT_ARGS_SIZE - 1} bytes')

    kernel_addr = a.base + a.kernel_offset
    ramdisk_addr = a.base + a.ramdisk_offset
    second_addr = a.base + a.second_offset
    tags_addr = a.base + a.tags_offset
    dtb_addr = a.base + a.dtb_offset

    # bootloader's image id: sha1 over the payloads and their sizes
    sha = hashlib.sha1()
    sha.update(kernel)
    sha.update(struct.pack('<I', len(kernel)))
    sha.update(ramdisk)
    sha.update(struct.pack('<I', len(ramdisk)))
    sha.update(second)
    sha.update(struct.pack('<I', len(second)))
    sha.update(dtb)
    sha.update(struct.pack('<I', len(dtb)))
    image_id = sha.digest()   # boot_img_hdr.id is 32 bytes, sha1 then zero padding

    hdr = bytearray()
    hdr += BOOT_MAGIC
    hdr += struct.pack('<9I', len(kernel), kernel_addr, len(ramdisk), ramdisk_addr,
                       len(second), second_addr, tags_addr, page, a.header_version)
    hdr += struct.pack('<I', os_version(a.os_version))
    hdr += a.name.encode()[:BOOT_NAME_SIZE].ljust(BOOT_NAME_SIZE, b'\0')
    hdr += a.cmdline.encode()[:BOOT_ARGS_SIZE].ljust(BOOT_ARGS_SIZE, b'\0')
    hdr += image_id.ljust(32, b'\0')
    hdr += b'\0' * BOOT_EXTRA_ARGS_SIZE
    hdr += struct.pack('<I', 0)             # recovery_dtbo_size
    hdr += struct.pack('<Q', 0)             # recovery_dtbo_offset
    hdr += struct.pack('<I', V2_HEADER_SIZE)
    hdr += struct.pack('<I', len(dtb))
    hdr += struct.pack('<Q', dtb_addr)
    assert len(hdr) == V2_HEADER_SIZE, len(hdr)

    out = pad(bytes(hdr), page)
    out += pad(kernel, page)
    out += pad(ramdisk, page)
    out += pad(second, page)
    out += pad(dtb, page)
    with open(a.output, 'wb') as f:
        f.write(out)
    print(f'{a.output}: {len(out)} bytes (kernel {len(kernel)}, ramdisk {len(ramdisk)}, '
          f'dtb {len(dtb)}, page {page})', file=sys.stderr)


if __name__ == '__main__':
    main(sys.argv[1:])
