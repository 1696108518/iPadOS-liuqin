# Retargeting this tree to another Qualcomm tablet

Derived from the liuqin port; every item below is a place where a hardware
assumption is written down. Work top to bottom — the early boot items first,
because nothing else can be debugged until the kernel reaches its console.

## 1. Read the board's device tree, not the vendor kernel

`tools/fdtread.py` parses a stock `boot.img` DTB with no `dtc` dependency:

```sh
python3 tools/unpack_bootv2.py backup/boot.img dtb/boot_unpacked
python3 tools/fdtread.py dtb/boot_unpacked/dtb
```

It prints the memory banks, GICD/GICR, timer frequency and virtual-timer
INTID, the UFS controller and the `simple-framebuffer` geometry. Those are the
values that differ between devices (and between RAM sizes of one device).

## 2. Early console

| File | What |
| --- | --- |
| `shim/console.h` | `FB_BASE`, `FB_W`, `FB_H`, `FB_STRIDE` — the splash memory ABL leaves mapped |
| `shim/shim.c` | `GICD_BASE`, `GICR_BASE`, `GICR_PE_SIZE` (stride!), `VTIMER_INTID`, `PTIMER_INTID` |
| `shim/mmu.h` | `FB_PHYS` (same as `FB_BASE`) |
| `xnu/patches/0010-*.patch` | the assembly console that runs before C |
| `xnu/patches/0012-*.patch` | the framebuffer alias that survives TTBR0 teardown |

The virtual timer INTID is the classic trap: Linux DT `GIC_PPI 11` means
INTID 27, but some Qualcomm DTs number the PPIs 1/2/3/0 instead (sweet: 19).

## 3. Machine config

`xnu/patches/0004-*.patch` adds `MACHINE_CONFIG`, `pexpert/pexpert/arm64/<BOARD>.h`
and the GIC setup. Change:

- `-march=`: use the oldest ISA that all cores implement (liuqin: armv8.2-a+crypto on ARMv9 cores).
- `MAX_CPUS`, cluster layout, `MMU_CLINE` (cache line size).
- `GICR_PE_SIZE`: 0x20000 normally, 0x40000 when `redistributor-stride` says so.
- PAC: these ports run the non-PAC arm64 ABI; keep `ARCH_STRING` plain `arm64`.

## 4. Memory and ADT

`tools/mkadt.py`: `HIGH_PHYS_BASE`, `LOW_BANK_SIZE`, `PANIC_LOG_SIZE`, the
`/chosen` security status, display scale, chip identity and the `/product`
node. Prefer `--dtb` over hardcoded numbers: the high bank size depends on
whether the unit has 8 or 12 GB, and carve-outs move between firmware builds.

The panic log is carved from the top of the exposed range (`/pram`); without it
XNU hangs right after `kdp_core zlib memory` (found on sweet and reproduced on
QEMU).

## 5. Storage

`bsd/dev/liuqin_ufs.c` looks the root partition up by GPT name
(`ufs_find_partition`), so it does not care about LBA offsets. What does matter:
the UFS host base (`ufshc@...`), the DMA aperture, and whether ABL leaves the
controller running (it does on both devices here).

## 6. Display and input

`iokit/Kernel/arm/LiuqinFramebuffer.cpp` presents the splash buffer as the
main display; its geometry is in `mkadt.py` (`disp0` canvas) and the logical
size in patch `0061`. Input is a separate driver per controller:
`iokit/Kernel/arm/hid/LiuqinHID.cpp` (HID event service, applied) plus a
hardware driver (not ported for liuqin; see `xnu/patches/reference/`).

## 7. Identity

The userland is Apple's and expects a real device: root `compatible` strings
are `hw.target`/`hw.product`, `model` is `hw.machine`, `chip-id` selects the
SoC generation, and `/product` must come from that device's IPSW. liuqin
presents `D321AP` / `iPad8,1` / chip `0x8027` (A12X). Anything that changes
identity changes what MobileGestalt, mobileactivationd and UIKit believe.

## 8. Verify before flashing

```sh
python3 tests/test_tools.py       # FDT/ADT/boot.img
tools/verify-patches.sh           # patch series against real xnu
shim/build.sh <payload>           # link + boot.img round trip
```
