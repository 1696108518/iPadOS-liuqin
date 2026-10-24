# GPU milestone: Adreno 730 up on liuqin

Date: 2026-10-23.  Kernel `xnu-8796.141.3`, image `ba5a683a...` (31.6 MB, firmware area
inside the boot image).  Boot line: `... liuqinadreno=22 liuqinfb_noconsole=1 wdt=-1`.

## What works

- Adreno bring-up driver `bsd/dev/liuqin_adreno.c` (ported from the negroni A730 series
  of UniXNU), all 22 steps, no panic:
  GCC GPU branches -> GPUCC read -> GMU read -> CX GDSC on -> GPUCC clocks (GPLL0 taps,
  200 MHz core / 150 MHz hub) -> GMU first words (AO counter moves) -> RSCC -> SMMU ->
  GMU memory -> TrustZone PAS support -> zap signature -> GMU space -> GMU firmware
  (`gmu_gen70000.bin`) -> GMU start -> HFI -> core power/space/start -> zap and secure ->
  first frame.
- The first frame is a 1080x2400 picture of three bands (orange / white / blue) filled by
  the 2D engine, read back and compared on the CPU, then copied into the boot framebuffer.
  Seen on the panel; log: `22 THE GPU DREW A PICTURE` + `picture copied to the framebuffer`.
- Firmware handling: `tools/mkfwarea.py` builds the 8 MiB area (`LIUQINFW` header + files +
  SHA-256), `tools/mkpayload.py --fwarea` appends it to the boot image and maps it as
  memory-map entry `LIUQINFW` (never `RAMDisk`: BSD init would create md0 and kalloc the
  whole 8 MiB).  `liuqin_fw_load()` copies it into normal kernel memory with 4-byte reads
  (Device memory + memcpy faults with an unaligned data abort) and exposes
  `liuqin_fw_get()`.
- SM8475 values differ from SM8450 only where noted: GMU 0x3d69000 (SM8450 0x3d6a000);
  GPU 0x3d00000, GPUCC 0x3d90000, GDSC 0x905c/0x9108, SMMU 0x3da0000 and the zap
  microcode region 0x8b91a000 match.  `IO_MAP_SIZE` raised 8 -> 64 MiB (arm/io_map.c):
  the 8 MiB firmware mapping failed with KERN_RESOURCE_SHORTAGE.

## Patches

- `xnu/patches/0085-liuqin-log-tail-dump.patch` - UFS driver, log ring, firmware loader.
- `xnu/patches/0087-liuqin-adreno.patch` - the Adreno driver (2013 lines) + conf/files.
- `xnu/patches/0088-liuqin-io-map-size.patch` - IO_MAP_SIZE 64 MiB.
- `xnu/patches/0089-liuqin-native-adreno.patch` - the NativeAdreno IOKit services, the
  kits, `LiuqinStubs.cpp`, the board glue and the service call in the UFS init path.
- `xnu/patches/0082-liuqin-exec-failure-print.patch` lost the hunk that `0042` already
  adds.  Two stale claims were hiding this: the README still said "78/78" while the
  series had grown to 87 files, and the local build tree predates `0042`, so nothing
  here ever re-applied the series.  A fresh `tools/verify-patches.sh` stopped at 0082;
  it is 88/88 again now, and the counts in README/LOCAL_BUILD are fixed.

## NativeAdreno IOKit services wired to the device tree (2026-10-23)

The UniXNU `NativeAdreno` port (62 files) plus the AdrenoPM4/AdrenoFirmware/ArmSmmu4K
kits now live in `iokit/NativeAdreno/` **and are captured in the repository** as
`xnu/patches/0089-liuqin-native-adreno.patch` (180 files, 17.5k lines).  Before this
patch the integration existed only in a `/tmp` build tree; a fresh
`tools/verify-patches.sh` tree now reproduces it.

Board wiring, all in `0089` except the ADT generator:

- `tools/mkadt.py` publishes three `/arm-io` children, with the board's own addresses
  (stock DTB `/soc/qcom,kgsl-3d0@3d00000`, `/soc/qcom,gmu@3d69000`,
  `/soc/kgsl-smmu@3da0000`) and 2 address + 2 size cells to agree with `/arm-io`:
  - `gpu`: `qcom,adreno-gpu-gen7-4-0`/`qcom,adreno`, GPU 0x3d00000/0x40000 and CX misc
    0x3d9e000/0x1000, `iommus` = streams 0 and 1 on `gpu-smmu`;
  - `gmu`: `qcom,adreno-gmu`/`qcom,gen7-gmu`, 0x3d68000/0x37000 (contains the
    0x3d69000 window the ladder programs), RSCC 0x3d50000/0x10000, AO/PDC
    0xb290000/0x10000 - the three `ShengGmu` windows the GPU service expects;
  - `gpu-smmu`: `qcom,adreno-smmu`/`qcom,qsmmu-v500`, 0x3da0000/0x40000,
    `#iommu-cells = 2`.
- `iokit/NativeAdreno/LiuqinAdrenoGlue.cpp` makes the services exist at all: they are
  plain IOService subclasses with no kernel matching personality, so nothing would ever
  instantiate them.  It finds the three nubs by `compatible`, calls `getResources()`
  (nothing does that for a nub no kext matched, and it is what turns `reg` into
  `IODeviceMemory`), then attaches and starts SMMU, GMU and GPU in that order - the GPU
  service looks the other two up by class name.  `liuqin_ufs_init()` calls it through a
  weak symbol, gated on boot argument `liuqinsvc=1`.
- Three integration bugs fixed in the port: the services asked `this` instead of
  `provider` for device memory; `NativeAdrenoBoard` parsed `reg`/`iommus` as big-endian
  FDT cells while this port's ADT is host-order (XNU's `IODTResolveAddressCell` reads it
  that way); and `NativeAdrenoGpuService::start()` now publishes the node, with
  `native-adreno-state = discovered`, once discovery succeeded even though no CP was
  started - `newUserClient` still refuses until then.

This image (`liuqinsvc=1` added to the usual line) should log:

```
NativeAdreno: nubs smmu=1 gmu=1 gpu=1
NativeAdrenoSmmu: 1 window mapped, no register access
NativeAdreno: SMMU started on gpu-smmu
NativeAdrenoGmu: 3 windows mapped, no register access
NativeAdreno: GMU started on gmu
NativeAdreno: 2 windows, GPU window at 0x3d00000/0x40000 mapped
NativeAdreno: GMU node present with 3 windows
NativeAdreno: GMU five-window I/O adapter bound
NativeAdreno: 2 stream IDs from iommus, first 0
NativeAdreno: publishing the GPU node without a started CP
```

and `ioreg -l` should list `NativeAdrenoGpuService`, `NativeAdrenoGmuService` and
`NativeAdrenoSmmuService` under the platform expert, the GPU one with
`native-adreno-state = discovered`.

**Verified on the host only**: `0082` was carrying a hunk duplicated from `0042`, which
made a fresh `tools/verify-patches.sh` fail; with that hunk removed all 88 patches
apply, `0089` lands on that tree unchanged, the kernel builds and links
(`d294adbb…`, 16,816,456 B), and the ADT dump shows the three nodes with the values
above.  None of this has run on hardware.  If the first line logs `nubs … = 0`, the
platform expert did not publish the children; if a service logs "has no device memory",
`getResources()` or the cell counts are wrong.

## First on-device round (2026-10-23, image `cd8ee3e6…`)

Flashed.  The kernel reached userspace and executed launchd.  The console (photo;
the persistent log was not pulled) showed, in order:

```
LIUQIN ADRENO: 22 picture copied to the framebuffer (0xb8000000, 2400 rows)
LIUQIN ADRENO: done (asked 22, reached 22)
NativeAdreno: nubs smmu=1 gmu=1 gpu=1
NativeAdrenoSmmu: 1 window mapped, no register access
NativeAdreno: SMMU started on gpu-smmu
NativeAdrenoGmu: 3 windows mapped, no register access
NativeAdreno: GMU started on gmu
NativeAdreno: 2 windows, GPU window at 0x2d0000000000/0x4000000000 mapped
NativeAdreno: GMU node present with 3 windows
NativeAdreno: GMU windows are not usable
NativeAdreno: 2 stream IDs from iommus, first 0
NativeAdreno: publishing the GPU node without a started CP
NativeAdreno: GPU started on gpu
BSD root: ufsroot0, major 1, minor 0
... journal replay ... load_init_program ... exec launchd
```

Observed on hardware, therefore: the three ADT nodes do become IOPlatformDevice nubs
(`nubs ... = 1 1 1`), all three services attach, start and register, the iommus parse
is right, and the GPU node is published.  The ladder still reaches step 22 with the
picture on the panel, and the boot goes on to launchd as before.

The one failure is mine: `NativeAdrenoBoard::parseReg` composed a two-cell value with
the first cell as the high word, while the ADT - and XNU's own `CellsValue` - puts the
low word first.  Every `reg` window therefore came out as `0x2d00000 << 32`, and the
GMU bind rejected the sizes (`bytes > UINT32_MAX`, "GMU windows are not usable").
Mapping was unaffected because it comes from XNU's resolution of the same `reg`; only
the service's window array and the GMU adapter were wrong.  Fixed by composing cells
low-word-first and checked against the ADT bytes (gpu 0x2d00000/0x40000,
gmu 0x2d68000/0x37000 + 0x2d50000/0x10000 + 0xa290000/0x10000, smmu 0x2da0000/0x40000;
these printed bases are relative to SOC_BASE - the `/arm-io` ranges add 0x1000000).

The rebuild should add `NativeAdreno: GMU five-window I/O adapter bound` between
"GMU node present" and the iommus line.  Everything after `exec launchd` is still the
old stopping point, not something this work changed.

## Image (rebuilt after the first on-device round)

The image that was flashed is `cd8ee3e6…` (see the round above); this is the rebuild with
the cell-order fix in it.  Boot line (`tools/mkpayload.py --cmdline`):

```
debug=0xc serial=3 cpus=8 keepsyms=1 rd=ufsroot0 ufsrw=1 rootrw=1 launchd_unsecure_cache=1
cs_enforcement_disable=1 amfi_get_out_of_my_way=1 unentitled_ios_sim_launch=1
liuqinfb_noconsole=1 wdt=-1 ignition_level=1 ufslogsecs=30 liuqinadreno=22 liuqinsvc=1
```

| artifact | bytes | sha256 |
| --- | --- | --- |
| `kernel.development.liuqin` | 16,816,456 | `d294adbb3c01e58245e355f81bede82d85019988c2f99662736f47b7842397a5` |
| `shim/out/payload.bin` | 25,217,296 | `71877b24bdc80c6cf9766e2f6b4568ef58b1f1c1a6876145a592d18342393b19` |
| `shim/out/shim-boot.img` | 31,793,152 | `693b02270de43db2e1b513db6cabbdaca9ec30a8d94cebf67eb8bd378f4e7826` |

Rebuild, from the repository:

```sh
WORK=/tmp/liuqin-build2 tools/build-local.sh
python3 tools/mkadt.py shim/out/liuqin.adt --dtb dtb/boot_unpacked/dtb \
    --dram-base 0x840000000 --dram-size 0x140000000
python3 tools/mkpayload.py /tmp/liuqin-build2/fakeroot/System/Library/Kernels/kernel.development.liuqin \
    shim/out/liuqin.adt shim/out/payload.bin --cmdline "<the line above>" \
    --fwarea ~/liuqin-root/fwarea.bin
LLD=/tmp/ld.lld shim/build.sh out/payload.bin       # zig: `zig ld.lld` wrapped in a script
```

## Still open

- The system still stops before SpringBoard: userspace has no Metal device.
- Remaining path (unixnu + hackios-xnu-userland, cloned in ~/liuqin-root/refs):
  1. Kernel: the UniXNU `NativeAdreno` service and its user client are in the kernel
     and published; what is missing is the live GPU behind them (CP started, SMMU
     contexts programmed, boards powered) - the ladder in `bsd/dev/liuqin_adreno.c`
     already does all of that from `liuqinadreno=1..22`, so the next kernel step is
     handing the ladder's state to the service instead of the current discovery-only
     publish.
  2. Userspace: cross-build Mesa (tools/mk-mesa.sh of hackios-xnu-userland; needs
     meson/ninja/bison/flex, their xnu-clang, iPhoneOS SDK) for arm64 iOS, Turnip
     (freedreno) + the Air/Metal frontend; plus `drivers/NativeMetal` (ObjC++, Metal
     framework) and the injection libraries (`userland/src/inject*.c`).
  3. Hook backboardd/SpringBoard so they discover the device (their `publish`/`clients`
     plugin data, sandbox grant patch).
