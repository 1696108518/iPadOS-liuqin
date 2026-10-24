# iPadOS-liuqin

**Apple's open-source XNU (xnu-8796, the iOS 16 generation) on the Xiaomi Pad 6 Pro (`liuqin`, Qualcomm SM8475 / Snapdragon 8+ Gen 1), with the unmodified iPadOS 16 userland on top.**

Fork of [dipa-studio/ios-sweet](https://github.com/dipa-studio/ios-sweet) (Redmi Note 10 Pro, SM7150),
retargeted to SM8475, with hardware data from
[yzddmr6/xiaomipad-6pro-mainline](https://github.com/yzddmr6/xiaomipad-6pro-mainline) and
[mytiantian001/iPadOS-nabu](https://github.com/mytiantian001/iPadOS-nabu).

[中文说明](README.zh-CN.md)

> **Status (2026-10-24): the tablet boots to the iPadOS lock screen.** XNU runs the unmodified
> iPadOS 16 userland: launchd, the system daemons, backboardd and SpringBoard come up, CoreAnimation
> renders in software into IOSurface and our IOMobileFramebuffer presents it to the panel. There is
> no touch or button driver yet, so the lock screen cannot be unlocked. On the GPU side the Adreno
> 730 is driven far enough to start its command processor and execute command streams, but nothing
> in the UI uses it yet. Experimental bring-up, not a usable tablet OS.

## Verified here (reproducible on the host)

| Check | Result |
| --- | --- |
| Kernel patch series | 146/146 apply cleanly to `xnu-8796.141.3` on top of darwin-xnu-build `6639065`, with Apple libpthread/hfs/BearSSL staged (`tools/verify-patches.sh`) |
| Kernel build | `tools/build-local.sh` (Xcode 15.4, no Homebrew/sudo/KDK) links `kernel.development.liuqin`, `MH_EXECUTE/ARM64/PIE`, sha256 `fae1a62d…` |
| ADT | `tools/mkadt.py` builds liuqin's tree: GIC 0x17100000, vtimer INTID 27, 1800x2880 canvas, 6 GiB high bank; `--dtb` reads the real memory map from the stock DTB |
| Shim | builds on macOS with clang `--target=aarch64-none-elf` + LLD; `tools/elf2bin.py` proves there are no absolute relocations |
| boot.img | packed with `tools/mkbootimg.py` (v2 header + DTB), round-tripped through `tools/unpack_bootv2.py` |
| Host tests | `python3 tests/test_tools.py` passes |

## Not done

- **Touch and buttons.** The HID event services are in the kernel
  (`iokit/Kernel/arm/hid/LiuqinHID.cpp`) but nothing feeds them: the NT36532
  touchscreen (GENI SPI4 at 0x990000, IRQ GPIO21) and the PMIC buttons have no
  drivers. `docs/TOUCH.md` has the hardware facts and the plan.
- **GPU in the UI.** The Adreno 730 cold-starts (GMU firmware, HFI, GX domain,
  SMMU context, command processor with a working timestamp fence) and the
  kernel verifies arbitrary PM4 execution, but CoreAnimation still renders in
  software; there is no Mesa/Turnip backend on top of the wire ABI yet.
- **Everything else conio-style:** no Wi-Fi, Bluetooth, cellular, audio, camera,
  sensors, battery gauge or SEP. Daemons for them start and fail.
- **Slow first boot:** LaunchServices seeds its database on the first boot
  (tens of minutes on this storage); later boots are much faster.

## Why liuqin differs from sweet

| | sweet (SM7150) | liuqin (SM8475) |
| --- | --- | --- |
| GICD / GICR / stride | 0x17a00000 / 0x17a60000 / 0x20000 | **0x17100000 / 0x17180000 / 0x40000** |
| Virtual timer INTID | 19 | **27** |
| Framebuffer | 0x9c000000, 1080x2400 | **0xb8000000, 1800x2880** |
| SoC | Kryo 470 (A76+A55), 2 clusters | **Kryo 780 (X2+A710+A510), 3 clusters** |
| Touch | Goodix GT9896 (I2C) | **Novatek NT36532 (SPI)** |
| PMIC | PM6150/PM6150L | **PM8350 family** |
| Identity | iPhone X (D22AP) | **iPad Pro 11" (D321AP / iPad8,1)** |

## Build

```sh
# 1. kernel: needs a macOS runner with Xcode 15
gh workflow run xnu.yml -R QPRS-6/iPadOS-liuqin
gh run download RUN_ID -R QPRS-6/iPadOS-liuqin -n xnu-kernel -D xnu/out

# 2. ADT + payload + boot image
python3 tools/mkadt.py shim/out/liuqin.adt --dtb dtb/boot_unpacked/dtb
python3 tools/mkpayload.py xnu/out/kernel.development.liuqin shim/out/liuqin.adt shim/out/payload.bin \
    --cmdline 'debug=0xc serial=3 cpus=1 keepsyms=1 rd=ufsroot0 ufsrw=1 rootrw=1 launchd_unsecure_cache=1 liuqinfb_noconsole=1 wdt=-1 ignition_level=1 ufslogsecs=1'
shim/build.sh shim/out/payload.bin          # LLD=... on macOS, see README.zh-CN.md

# 3. iPadOS root (Linux, iPad8,1 IPSW)
tools/mkiosroot.sh OS.dmg SystemOS.dmg AppOS.dmg ipados16-root.img 16G
# macOS host without apfs-fuse/hfsprogs: same recipe with hdiutil/newfs_hfs
sudo tools/mkiosroot-macos.sh OS.dmg SystemOS.dmg AppOS.dmg ipados16-root.img 16G

# 4. flash
fastboot -S 512M flash userdata ipados16-root.simg
fastboot boot shim/out/shim-boot.img
```

`NOTES.md` has the verification evidence, the hardware table with sources, the risks (memory map,
MPIDR, iPad identity) and the concrete next steps. `docs/PORTING.md` is the per-item checklist for
retargeting another Qualcomm tablet. iOS/iPadOS is Apple's and is not distributed here.
