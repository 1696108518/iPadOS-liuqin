#!/usr/bin/env python3
"""Flatten a linked AArch64 ELF image into the raw bytes ABL loads.

Replaces `aarch64-linux-gnu-objcopy -O binary` so the shim builds on any host
with a cross clang and a linker (see shim/build.sh); .bss is materialised as
zeros, exactly like the old `truncate -s _end` step did, but derived from the
program headers instead of a symbol.

  elf2bin.py IMAGE.elf IMAGE.bin [--reloc-check]
"""
import struct, sys

EM_AARCH64 = 183
PT_LOAD = 1
R_AARCH64_ABS64 = 257


def sections(blob):
    (e_shoff,) = struct.unpack_from('<Q', blob, 0x28)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from('<HHH', blob, 0x3A)
    out = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        name, typ, flags, addr, offset, size, link, info, align, entsize = \
            struct.unpack_from('<IIQQQQIIQQ', blob, off)
        out.append(dict(name=name, type=typ, addr=addr, offset=offset, size=size,
                        entsize=entsize, link=link))
    return out


def main(argv):
    src, dst = argv[1], argv[2]
    blob = open(src, 'rb').read()
    if blob[:4] != b'\x7fELF' or blob[4] != 2 or blob[5] != 1:
        raise SystemExit(f'{src}: not a little-endian ELF64 file')
    (e_type, e_machine) = struct.unpack_from('<HH', blob, 16)
    if e_machine != EM_AARCH64:
        raise SystemExit(f'{src}: not AArch64 (e_machine={e_machine})')
    if e_type != 2:
        raise SystemExit(f'{src}: not an executable (e_type={e_type})')

    (e_phoff,) = struct.unpack_from('<Q', blob, 0x20)
    e_phentsize, e_phnum = struct.unpack_from('<HH', blob, 0x36)

    loads = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align = \
            struct.unpack_from('<IIQQQQQQ', blob, off)
        if p_type == PT_LOAD and p_memsz:
            loads.append((p_vaddr, p_offset, p_filesz, p_memsz))
    if not loads:
        raise SystemExit(f'{src}: no PT_LOAD segments')

    base = min(v for v, _, _, _ in loads)
    end = max(v + m for v, _, _, m in loads)
    image = bytearray(end - base)
    for vaddr, offset, filesz, memsz in loads:
        image[vaddr - base:vaddr - base + filesz] = blob[offset:offset + filesz]
        # memsz > filesz is .bss: already zero

    if '--reloc-check' in argv:
        shdrs = sections(blob)
        text = None
        # .shstrtab is the section header string table
        bad = 0
        for s in shdrs:
            if s['type'] == 4:          # SHT_RELA
                for j in range(s['size'] // 24):
                    (_off, info, _add) = struct.unpack_from('<QQq', blob, s['offset'] + j * 24)
                    if (info & 0xFFFFFFFF) == R_AARCH64_ABS64:
                        bad += 1
        if bad:
            raise SystemExit(f'{src}: {bad} absolute (R_AARCH64_ABS64) relocations; '
                             'the image is not position independent')
        print(f'{src}: no absolute relocations', file=sys.stderr)
        del text

    open(dst, 'wb').write(bytes(image))
    print(f'{dst}: {len(image)} bytes (base {base:#x})', file=sys.stderr)


if __name__ == '__main__':
    main(sys.argv)
