#!/usr/bin/env python3
"""Host-side tests for the liuqin boot tools. No device, no XNU needed.

  python3 tests/test_tools.py

Covers the parts of the chain that must be right before anything is flashed:
the FDT reader against a tree shaped like liuqin's stock DTB, mkadt.py's
memory/GIC/timer/framebuffer selection, the generated ADT, and the boot image
packer (round-tripped through tools/unpack_bootv2.py).
"""
import io
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import adt        # noqa: E402
import fdtread    # noqa: E402

FAILS = []


def child(node, name):
    for c in node.children:
        if any(k == 'name' and v.split(b'\0')[0] == name.encode() for k, _f, v in c.props):
            return c
    raise KeyError(name)


def check(cond, what):
    if cond:
        print(f'  ok   {what}')
    else:
        print(f'  FAIL {what}')
        FAILS.append(what)


# --------------------------------------------------------------- FDT writer
def _pad4(b):
    return b + b'\0' * (-len(b) % 4)


class FdtBuilder:
    def __init__(self):
        self.strings = bytearray()
        self.strings_off = {}

    def name(self, s):
        if s not in self.strings_off:
            self.strings_off[s] = len(self.strings)
            self.strings += s.encode() + b'\0'
        return self.strings_off[s]

    def prop(self, name, value):
        return struct.pack('>III', 3, len(value), self.name(name)) + _pad4(value)

    def begin(self, name):
        return struct.pack('>I', 1) + _pad4(name.encode() + b'\0')

    def end(self):
        return struct.pack('>I', 2)

    def finish(self, struct_blob, mem_rsv=()):
        rsv = b''.join(struct.pack('>QQ', a, s) for a, s in mem_rsv) + b'\0' * 16
        off_rsv = 0x28
        off_struct = off_rsv + len(rsv)
        off_strings = off_struct + len(struct_blob)
        total = off_strings + len(self.strings)
        hdr = struct.pack('>10I', 0xD00DFEED, total, off_struct, off_strings,
                          off_rsv, 17, 16, 0, len(self.strings), len(struct_blob))
        return hdr + rsv + struct_blob + bytes(self.strings)


def reg2(addr, size):
    """<addr size> with 2 address cells and 2 size cells."""
    return struct.pack('>IIII', addr >> 32, addr & 0xFFFFFFFF, size >> 32, size & 0xFFFFFFFF)


def liuqin_like_dtb(ram_gib=8, empty_memory=False):
    """The nodes mkadt.py reads, with liuqin's real addresses."""
    b = FdtBuilder()
    soc = b''
    soc += b.begin('soc@0')
    soc += b.prop('#address-cells', struct.pack('>I', 2))
    soc += b.prop('#size-cells', struct.pack('>I', 2))
    soc += b.prop('ranges', b'\0' * 16)
    soc += b.begin('interrupt-controller@17100000')
    soc += b.prop('compatible', b'arm,gic-v3\0')
    soc += b.prop('reg', reg2(0x17100000, 0x10000) + reg2(0x17180000, 0x200000))
    soc += b.prop('redistributor-stride', struct.pack('>I', 0x40000))
    soc += b.end()
    soc += b.begin('ufshc@1d84000')
    soc += b.prop('compatible', b'qcom,sm8475-ufshc\0qcom,ufshc\0')
    soc += b.prop('reg', reg2(0x1d84000, 0x3000))
    soc += b.end()
    soc += b.end()

    t = b''
    t += b.begin('timer')
    t += b.prop('compatible', b'arm,armv8-timer\0')
    t += b.prop('interrupts',
                struct.pack('>III', 1, 13, 0) + struct.pack('>III', 1, 14, 0) +
                struct.pack('>III', 1, 11, 0) + struct.pack('>III', 1, 10, 0))
    t += b.prop('clock-frequency', struct.pack('>I', 19200000))
    t += b.end()

    m = b''
    m += b.begin(f'memory@{0x80000000:x}')
    m += b.prop('device_type', b'memory\0')
    m += b.prop('reg', reg2(0, 0) if empty_memory else reg2(0x80000000, 0x80000000))
    m += b.end()
    high = ram_gib * (1 << 30) - 0x80000000
    m += b.begin(f'memory@{0x100000000:x}')
    m += b.prop('device_type', b'memory\0')
    m += b.prop('reg', reg2(0, 0) if empty_memory else reg2(0x100000000, high))
    m += b.end()

    ch = b''
    ch += b.begin('chosen')
    ch += b.prop('#address-cells', struct.pack('>I', 2))
    ch += b.prop('#size-cells', struct.pack('>I', 2))
    ch += b.begin('framebuffer@b8000000')
    ch += b.prop('compatible', b'simple-framebuffer\0')
    ch += b.prop('reg', reg2(0xb8000000, 0x2b00000))
    ch += b.prop('width', struct.pack('>I', 1800))
    ch += b.prop('height', struct.pack('>I', 2880))
    ch += b.prop('stride', struct.pack('>I', 1800 * 4))
    ch += b.end()
    ch += b.end()

    root = b.begin('') + b.prop('#address-cells', struct.pack('>I', 2)) + \
        b.prop('#size-cells', struct.pack('>I', 2)) + soc + t + m + ch + b.end()
    return b.finish(root + struct.pack('>I', 9))


# -------------------------------------------------------------------- tests
def test_fdtread(tmp):
    print('fdtread')
    root = fdtread.parse(liuqin_like_dtb(8))
    banks = fdtread.memory_banks(root)
    check(banks == [(0x80000000, 0x80000000), (0x100000000, 0x180000000)],
          f'memory banks {[(hex(a), hex(s)) for a, s in banks]}')
    g = fdtread.gic(root)
    check(g and g['gicd'] == (0x17100000, 0x10000), 'GICD 0x17100000')
    check(g and g['gicr'] == (0x17180000, 0x200000), 'GICR 0x17180000 +0x200000')
    t = fdtread.timer(root)
    check(t['freq'] == 19200000 and t['vtimer_intid'] == 27,
          f"timer {t['freq']} Hz, vtimer INTID {t['vtimer_intid']}")
    check(fdtread.qcom_ufshc(root) == (0x1d84000, 0x3000), 'UFS 0x1d84000')
    fb = fdtread.simple_framebuffer(root)
    check(fb['base'] == 0xb8000000 and fb['width'] == 1800 and fb['height'] == 2880,
          'framebuffer 0xb8000000 1800x2880')
    check(fdtread.find(root, '/soc@0/interrupt-controller@17100000') is not None,
          'path lookup')


def test_mkadt_defaults(tmp):
    print('mkadt defaults (no DTB, 8 GB)')
    out = os.path.join(tmp, 'liuqin.adt')
    subprocess.run([sys.executable, os.path.join(ROOT, 'tools/mkadt.py'), out],
                   check=True, cwd=ROOT, stderr=subprocess.DEVNULL)
    root, _ = adt.parse(open(out, 'rb').read())
    chosen = dict((k, v) for k, _f, v in child(root, 'chosen').props)
    base = struct.unpack('<Q', chosen['dram-base'])[0]
    size = struct.unpack('<Q', chosen['dram-size'])[0]
    check((base, size) == (0x100000000, 0x180000000 - 0x100000),
          f'dram {base:#x}+{size:#x} (panic log carved off)')
    gic = child(child(root, 'arm-io'), 'gic')
    regs = struct.unpack('<4Q', dict((k, v) for k, _f, v in gic.props)['reg'])
    check(regs[0] == 0x17100000 - 0x01000000 and regs[1] == 0x10000,
          'gic reg is SOC_BASE-relative (XNU divides by it)')
    vt = struct.unpack('<I', dict((k, v) for k, _f, v in gic.props)['virtual-timer-intid'])[0]
    check(vt == 27, f'virtual-timer-intid {vt}')
    disp = child(child(root, 'arm-io'), 'disp0')
    dp = dict((k, v) for k, _f, v in disp.props)
    check(struct.unpack('<I', dp['canvas-width'])[0] == 1800 and
          struct.unpack('<I', dp['canvas-height'])[0] == 2880, 'canvas 1800x2880')
    compat = [s for s in dict((k, v) for k, _f, v in root.props)['compatible'].split(b'\0') if s]
    check(compat[:2] == [b'D321AP', b'iPad8,1'], f'identity {compat[:2]}')


def test_mkadt_dtb(tmp):
    print('mkadt --dtb (12 GB unit)')
    dtb = os.path.join(tmp, 'liuqin.dtb')
    open(dtb, 'wb').write(liuqin_like_dtb(12))
    out = os.path.join(tmp, 'liuqin12.adt')
    subprocess.run([sys.executable, os.path.join(ROOT, 'tools/mkadt.py'), out, '--dtb', dtb],
                   check=True, cwd=ROOT, stderr=subprocess.DEVNULL)
    root, _ = adt.parse(open(out, 'rb').read())
    chosen = dict((k, v) for k, _f, v in child(root, 'chosen').props)
    base = struct.unpack('<Q', chosen['dram-base'])[0]
    size = struct.unpack('<Q', chosen['dram-size'])[0]
    check(base == 0x100000000 and size == 12 * (1 << 30) - 0x80000000 - 0x100000,
          f'12 GB dram {base:#x}+{size:#x}')


def test_mkadt_empty_memory(tmp):
    print('mkadt --dtb with ABL-zeroed /memory')
    dtb = os.path.join(tmp, 'zeroed.dtb')
    open(dtb, 'wb').write(liuqin_like_dtb(8, empty_memory=True))
    out = os.path.join(tmp, 'zeroed.adt')
    subprocess.run([sys.executable, os.path.join(ROOT, 'tools/mkadt.py'), out, '--dtb', dtb],
                   check=True, cwd=ROOT, stderr=subprocess.DEVNULL)
    root, _ = adt.parse(open(out, 'rb').read())
    chosen = dict((k, v) for k, _f, v in child(root, 'chosen').props)
    base = struct.unpack('<Q', chosen['dram-base'])[0]
    size = struct.unpack('<Q', chosen['dram-size'])[0]
    check(base == 0x100000000 and size == 8 * (1 << 30) - 0x80000000 - 0x100000,
          f'fallback to --ram-gib: {base:#x}+{size:#x}')


def test_bootimg(tmp):
    print('mkbootimg + unpack_bootv2 round trip')
    kernel = os.path.join(tmp, 'kernel.bin')
    ramdisk = os.path.join(tmp, 'ramdisk.bin')
    dtb = os.path.join(tmp, 'stock.dtb')
    img = os.path.join(tmp, 'boot.img')
    open(kernel, 'wb').write(b'\x00' * 4096 + b'KERNEL' * 128)
    open(ramdisk, 'wb').write(b'RAMDISK' * 64)
    open(dtb, 'wb').write(liuqin_like_dtb(8))
    subprocess.run([sys.executable, os.path.join(ROOT, 'tools/mkbootimg.py'),
                    '--kernel', kernel, '--ramdisk', ramdisk, '--dtb', dtb,
                    '--base', '0', '--kernel_offset', '0x8000',
                    '--ramdisk_offset', '0x1000000', '--tags_offset', '0x100',
                    '--dtb_offset', '0x1f00000', '--pagesize', '4096',
                    '--os_version', '17.0.0', '--os_patch_level', '2035-06',
                    '--cmdline', 'console=null', '-o', img],
                   check=True, stderr=subprocess.DEVNULL)
    blob = open(img, 'rb').read()
    check(blob[:8] == b'ANDROID!', 'ANDROID! magic')
    check(len(blob) % 4096 == 0, 'page aligned')
    hdr = struct.unpack_from('<9I', blob, 8)
    check(hdr[7] == 4096 and hdr[8] == 2, 'page size 4096, header v2')
    dtb_size, dtb_addr = struct.unpack_from('<IQ', blob, 1648)
    check(dtb_size == len(open(dtb, 'rb').read()) and dtb_addr == 0x1f00000,
          f'dtb {dtb_size} bytes at {dtb_addr:#x}')
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools/unpack_bootv2.py'),
                        img, os.path.join(tmp, 'unpacked')],
                       capture_output=True, text=True)
    check(r.returncode == 0, 'unpack_bootv2 accepts the image')
    if r.returncode != 0:
        print(r.stdout[-500:], r.stderr[-500:])


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_fdtread(tmp)
        test_mkadt_defaults(tmp)
        test_mkadt_dtb(tmp)
        test_mkadt_empty_memory(tmp)
        test_bootimg(tmp)
    print()
    if FAILS:
        print(f'{len(FAILS)} FAILED: {FAILS}')
        return 1
    print('all host tool tests passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
