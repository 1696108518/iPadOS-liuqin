#!/usr/bin/env python3
"""Build the minimal Apple Device Tree (ADT) that the LIUQIN xnu needs to boot.

Only what the early boot path actually reads (see NOTES.md). Every hardware
constant below is for the Xiaomi Pad 6 Pro (liuqin, Qualcomm SM8475) and is
taken from the board's mainline device tree (`sm8475-xiaomi-liuqin.dts`,
`sm8450.dtsi`); `--dtb` can instead read them from the stock DTB shipped in
the tablet's own boot.img, which is the only way to get the exact memory map
of an 8 GB vs 12 GB unit.

Usage:
  mkadt.py OUT.adt [--dump] [--qemu] [--ram-gib N] [--dtb PATH]
                   [--dram-base HEX] [--dram-size HEX]
"""
import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt      # noqa: E402
import fdtread  # noqa: E402

GiB = 1 << 30

# ---------------------------------------------------------------- liuqin SoC
# Framebuffer left by ABL: reserved-memory splash_region@b8000000, 43 MiB,
# 1800x2880 a8r8g8b8 (portrait, the panel's native scan direction).
FB, FB_SIZE = 0xB8000000, 0x2B00000
FB_W, FB_H, FB_STRIDE = 1800, 2880, 1800 * 4

# GICv3: GICD@17100000, 8 redistributors at 0x40000 stride (@17180000).
GICD, GICD_SIZE = 0x17100000, 0x10000
GICR, GICR_SIZE = 0x17180000, 0x200000
GICR_STRIDE = 0x40000

# Internal UFS host ("ufs_mem" @1d84000), same offset as sweet.
UFS, UFS_SIZE = 0x01D84000, 0x3000

TIMEBASE = 19200000
VTIMER_INTID = 27                 # arch timer PPI 11 -> INTID 16 + 11
SOC_BASE = 0x01000000             # /arm-io children are relative to this

# Adreno 730 (SM8475). Windows are the ones the OpenGL/Vulkan KMD and the GMU
# driver use on this SoC: gpu@3d00000 (control), cx_misc@3d9e000, gmu@3d68000
# (the 0x37000 span that contains the 0x3d69000 window the bring-up driver
# programs), rscc@3d50000 and the AO/PDC block@b290000. Note these are the
# board's own values, not the sheng (SM8550) ones: see NOTES.md.
GPU, GPU_SIZE = 0x03D00000, 0x40000
GPU_CX_MISC, GPU_CX_MISC_SIZE = 0x03D9E000, 0x1000
GMU, GMU_SIZE = 0x03D68000, 0x37000
GMU_RSCC, GMU_RSCC_SIZE = 0x03D50000, 0x10000
GMU_PDC, GMU_PDC_SIZE = 0x0B290000, 0x10000
SMMU, SMMU_SIZE = 0x03DA0000, 0x40000

# Kernel-managed RAM: the whole high DDR bank, above Qualcomm's carve-outs
# (adsp@9fd00000, ramoops@a7000000 and splash@b8000000 all live in the low
# 2 GiB). 8 GB = 2 GiB low + 6 GiB high; 12 GB units have 10 GiB high.
HIGH_PHYS_BASE = 0x100000000
LOW_BANK_SIZE = 0x80000000

PANIC_LOG_SIZE = 0x100000         # carved from the top of the dram range, /pram
LIUQIN_ECID = 0x4C495551494E0001  # 'LIUQIN' + 1, synthetic (not the tablet's)
CHIP_ID = 0x8027                  # t8027 = A12X, the identity we present
BOOT_MANIFEST_HASH = hashlib.sha384(b'liuqin boot manifest').digest()

# QEMU virt (-M virt,gic-version=3 -cpu cortex-a76), see tools/qemu-liuqin.py
QEMU_GICD, QEMU_GICR = 0x08000000, 0x080A0000
QEMU_GICR_SIZE = 0x00F60000
QEMU_TIMEBASE = 62500000
QEMU_VTIMER_INTID = 27
QEMU_FB, QEMU_FB_SIZE, QEMU_FB_W, QEMU_FB_H = 0xB8000000, 0x2B00000, 1800, 2880

# Identity presented to the iPadOS userland. The root "compatible" strings are
# hw.target / hw.product and "model" is hw.machine, which MobileGestalt uses
# for product type, screen class and artwork. iPad8,1 is the 11-inch iPad Pro
# (A12X, no Home button, ProMotion 120 Hz) -- the closest match to liuqin.
IDENTITY_MODEL = 'iPad8,1'
TARGET_TYPE, TARGET_SUB_TYPE = 'D321', 'D321AP'
PRODUCT_ADT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '../research/ipad-d321ap/d321ap.adt')
DISPLAY_SCALE = 2                 # 1800x2880 @2x = 900x1440 points

u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


def product_props(path=PRODUCT_ADT):
    """/product properties of a real Apple device tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and node bookkeeping.

    Apple's tree is not redistributable; extract it once from the IPSW with
    tools/remotezip.py + `ipsw img4 im4p extract` (see README)."""
    if not os.path.exists(path):
        print(f'warning: {path} missing, no /product node', file=sys.stderr)
        return None
    root, _ = adt.parse(open(path, 'rb').read())
    for c in root.children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props
                    if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')


def memory_layout(ram_gib=8, base=None, size=None, dtb=None):
    """(phys_base, mem_size, panic_base) for the kernel-managed bank."""
    if dtb is not None:
        banks = [b for b in fdtread.memory_banks(dtb) if b[0] and b[1]]
        if banks:
            high = [b for b in banks if b[0] >= 0x100000000]
            pool = high or banks
            base, size = max(pool, key=lambda b: b[0])
        elif base is None or size is None:
            # The DTB that ships in vendor_boot has /memory reg = <0 0>;
            # ABL fills in the real bank when it boots, so an offline build
            # has to fall back to the --ram-gib assumption.
            print('warning: DTB /memory is empty (ABL fills it at boot); '
                  'using the --ram-gib default', file=sys.stderr)
    if base is None:
        base = HIGH_PHYS_BASE
    if size is None:
        size = ram_gib * GiB - LOW_BANK_SIZE
    panic_base = base + size - PANIC_LOG_SIZE
    return base, size - PANIC_LOG_SIZE, panic_base


def build(seed=None, qemu=False, ram_gib=8, dtb=None, dram_base=None, dram_size=None):
    """qemu=True: QEMU virt's GIC/CNTFRQ/framebuffer instead of liuqin's."""
    if qemu:
        # Keep the tablet's framebuffer address but move the kernel above it,
        # so the hardcoded FB addresses in the kernel are exercised in QEMU.
        phys_base, mem_size = 0xC0000000, 0x3D000000
        panic_base = phys_base + mem_size - PANIC_LOG_SIZE
        mem_size -= PANIC_LOG_SIZE
        gicd, gicd_size, gicr, gicr_size = QEMU_GICD, GICD_SIZE, QEMU_GICR, QEMU_GICR_SIZE
        timebase, vtimer = QEMU_TIMEBASE, QEMU_VTIMER_INTID
        fb, fb_size, fb_w, fb_h = QEMU_FB, QEMU_FB_SIZE, QEMU_FB_W, QEMU_FB_H
    else:
        phys_base, mem_size, panic_base = memory_layout(ram_gib, dram_base, dram_size, dtb)
        gicd, gicd_size, gicr, gicr_size = GICD, GICD_SIZE, GICR, GICR_SIZE
        timebase, vtimer = TIMEBASE, VTIMER_INTID
        fb, fb_size, fb_w, fb_h = FB, FB_SIZE, FB_W, FB_H
        if dtb is not None:
            g = fdtread.gic(dtb)
            if g:
                (gicd, gicd_size), (gicr, gicr_size) = g['gicd'], g['gicr']
            t = fdtread.timer(dtb)
            if t and t['freq']:
                timebase = t['freq']
            if t and t['vtimer_intid']:
                vtimer = t['vtimer_intid']
            f = fdtread.simple_framebuffer(dtb)
            if f and f['width'] and f['height']:
                fb, fb_size = f['base'], f['size']
                fb_w, fb_h = f['width'], f['height']

    seed = seed if seed is not None else os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('embedded-panic-log-size', u32(PANIC_LOG_SIZE)),
        ('firmware-version', cstr('liuqin-shim')),
        ('system-firmware-version', cstr('liuqin-shim')),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits unless
        # it exists. Fixed stand-in value.
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        # libimg4 (cryptexd) aborts "failed to get crypto algorithm" without it.
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt's screen class: the pixel size comes from
        # IOMobileFramebuffer, the scale from here. 1800x2880 @2x.
        ('display-scale', u32(DISPLAY_SCALE)),
        # XNU derives IOPlatformUUID from these; without them CFPreferences
        # cannot use kCFPreferencesCurrentHost and DataMigration re-runs.
        ('unique-chip-id', struct.pack('<Q', LIUQIN_ECID)),
        ('chip-id', u32(CHIP_ID)),
        # Dev-fused, undemoted board (tuple 0,0,1,1): mobileactivationd's
        # DeviceType takes its native "Hactivation" branch.
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
    ], [node('memory-map', [ph()]),
        # Local boot policy: SIP relaxed like `csrutil disable`; lp-sip1
        # unauthenticated root for the unsealed shared cache.
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))])])

    cpu_nodes = []
    for core in range(1 if qemu else 8):
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', cstr('qcom,kryo780') + cstr('ARM,v8')),
            ('reg', u32(core << 8)),   # MPIDR Aff1 = core id (as on sweet)
            ('cpu-id', u32(core)),
            ('cluster-id', u32(0)),
            ('cluster-type', u32(0)),
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(core)),
            ('l2-cache-id', u32(core)),
            ('l3-cache-id', u32(0)),
            ('state', cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', u32(timebase)),
            ('clock-frequency', u32(2188800000 if core == 7 else 1996800000)),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(3200000000)),
            ('peripheral-frequency', u32(timebase)),
            ('fixed-frequency', u32(timebase)),
        ]))
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], cpu_nodes)

    gic = node('gic', [
        ph(),
        ('compatible', cstr('arm,gic-v3')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(vtimer)),
        ('reg', u64(gicd - SOC_BASE, gicd_size, gicr - SOC_BASE, gicr_size)),
    ])

    # Adreno 730 nodes for the native Adreno IOKit services
    # (iokit/NativeAdreno). They match by compatible, take their windows from
    # reg (2 address cells to agree with /arm-io) and expect the GPU node to
    # carry the register and CX-misc windows and the GMU node its three
    # ShengGmu windows. iommus entries are <&gpu-smmu sid flags>: stream 0 is
    # the GPU's own, 1 is LPAC. Nothing writes a register here.
    gpu_smmu = node('gpu-smmu', [
        ph(),
        ('compatible', cstr('qcom,adreno-smmu') + cstr('qcom,qsmmu-v500')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('#iommu-cells', u32(2)),
        ('reg', u64(SMMU - SOC_BASE, SMMU_SIZE)),
    ])
    smmu_phandle = struct.unpack(
        '<I', next(v for k, _f, v in gpu_smmu.props if k == 'AAPL,phandle'))[0]
    gpu = node('gpu', [
        ph(),
        ('compatible', cstr('qcom,adreno-gpu-gen7-4-0') + cstr('qcom,adreno')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('reg', u64(GPU - SOC_BASE, GPU_SIZE,
                    GPU_CX_MISC - SOC_BASE, GPU_CX_MISC_SIZE)),
        ('iommus', u32(smmu_phandle, 0, 0, smmu_phandle, 1, 0)),
    ])
    gmu = node('gmu', [
        ph(),
        ('compatible', cstr('qcom,adreno-gmu') + cstr('qcom,gen7-gmu')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('reg', u64(GMU - SOC_BASE, GMU_SIZE,
                    GMU_RSCC - SOC_BASE, GMU_RSCC_SIZE,
                    GMU_PDC - SOC_BASE, GMU_PDC_SIZE)),
    ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('liuqin-io')),
        ('compatible', cstr('liuqin-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [gic,
        # watchdogd checks IODeviceTree:/arm-io/wdt for no-pmu.
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # IOMobileFramebuffer counts /arm-io children with this device_type
        # and reads the canvas size from the node.
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('liuqin,display')),
            ('canvas-width', u32(fb_w)),
            ('canvas-height', u32(fb_h)),
        ]),
        gpu, gmu, gpu_smmu])

    fbcon = node('liuqin-fbcon', [
        ph(),
        ('reg', u64(fb, fb_size)),
        ('width', u32(fb_w)),
        ('height', u32(fb_h)),
        ('stride', u32(fb_w * 4)),
    ])

    # bsd/dev/liuqin_ufs.c: the UFS host ABL leaves running.
    ufs = node('liuqin-ufs', [
        ph(),
        ('compatible', cstr('qcom,ufshc')),
        ('reg', u64(UFS, UFS_SIZE)),
    ])

    # An absent pmap-max-asids property selects XNU's default. No effaceable
    # storage: keybagd reads the plain plist keybag instead of unwrapping it.
    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    # MobileGestalt HomeButtonType = IODeviceTree:/buttons home-button-type
    # (no Home button on an iPad Pro 11 nor on liuqin).
    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    pp = product_props()
    product = [node('product', [ph()] + pp)] if pp else []

    # Panic log region: /pram reg = <phys size>, just above the exposed dram.
    pram = node('pram', [ph(), ('reg', u64(panic_base) + u64(PANIC_LOG_SIZE))])

    return node('device-tree', [
        ph(),
        ('compatible', cstr(TARGET_SUB_TYPE) + cstr(IDENTITY_MODEL) +
                       cstr('xiaomi,liuqin') + cstr('qcom,sm8475') + cstr('AppleARM')),
        ('model', cstr(IDENTITY_MODEL)),
        ('target-type', cstr(TARGET_TYPE)),
        ('target-sub-type', cstr(TARGET_SUB_TYPE)),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons, pram] + product +
       ([] if qemu else [ufs]))


def main(argv):
    out = argv[1]
    qemu = '--qemu' in argv
    ram_gib = 8
    if '--ram-gib' in argv:
        ram_gib = int(argv[argv.index('--ram-gib') + 1])
    dtb = None
    if '--dtb' in argv:
        dtb = fdtread.parse(open(argv[argv.index('--dtb') + 1], 'rb').read())
    dram_base = int(argv[argv.index('--dram-base') + 1], 0) if '--dram-base' in argv else None
    dram_size = int(argv[argv.index('--dram-size') + 1], 0) if '--dram-size' in argv else None

    root = build(qemu=qemu, ram_gib=ram_gib, dtb=dtb,
                 dram_base=dram_base, dram_size=dram_size)
    blob = adt.serialize(root)
    open(out, 'wb').write(blob)
    if '--dump' in argv:
        adt.dump(adt.parse(blob)[0])
    base, size, _panic = memory_layout(ram_gib, dram_base, dram_size, dtb)
    print(f'{out}: {len(blob)} bytes, dram {base:#x}+{size:#x} '
          f'({"qemu" if qemu else "liuqin"})', file=sys.stderr)


if __name__ == '__main__':
    main(sys.argv)
