# GPU next: making the NativeAdreno service own the GPU (route A)

Status 2026-10-23 night.  The GMU adapter fix is confirmed on hardware: with
the `b0a98ee5…` kernel the boot log has

```
NativeAdreno: GMU node present with 3 windows
NativeAdreno: GMU five-window I/O adapter bound
NativeAdreno: 2 stream IDs from iommus, first 0
NativeAdreno: publishing the GPU node without a started CP
```

so the service's `reg` parsing and the five-window GMU adapter are right.  It
still publishes `native-adreno-state = discovered` because **this build has no
generated Sheng tables**: `NativeAdrenoService.cpp` includes
`ShengGmuSource.h`, `ShengGmuVotesSource.h` and `ShengGmuCommandDbSource.h`
behind `__has_include`, and without them the whole cold-start path is compiled
out.  The kernel contains only:

```
NativeAdreno: GMU rules not generated for this build
NativeAdreno: command-db reservation not generated for this build
```

Only the bring-up ladder can start the GPU today, and its state is not
reachable from the user client (`NativeAdrenoUserClient` is built on
`NativeAdrenoVm`/`NativeAdrenoSubmit`, the ladder programs its own page
tables).  Hence route A: generate the three tables for liuqin, let the service
do GMU cold start + GPU context + CP + ring itself, and retire the ladder to a
diagnostic.

## Data we already have

### GMU register offsets and the day-one gotcha

`ShengGmu.c` addresses GMU registers as

```
offset = (word << 2) - SHENG_GMU_CORE_GPU_DELTA
```

where `word` is the A6XX XML register number in **dwords**.  The ladder uses
exactly the same convention:

```
#define GMU_REG(dw) ((vm_offset_t)(dw) * 4 - (GMU_PHYS - GPU_PHYS))
```

with `GMU_PHYS = 0x3d69000`, `GPU_PHYS = 0x3d00000`, i.e.
`SHENG_GMU_CORE_GPU_DELTA = 0x69000`.  The ladder's constants
(`GMU_CM3_ITCM_START` 0x1b400, `GMU_CM3_DTCM_START` 0x1c400,
`GMU_CM3_SYSRESET` 0x1f800, `GMU_HFI_QTBL_ADDR` 0x1f985, …) can be copied
verbatim as the generated `REG_*` defines.

### RSC/PDC setup (the 19 writes)

`ShengGmuSource.h` carries `sheng_gmu_rsc_setup[19]` as
`{window, byte_offset, value}` with the window order

```
0 ShengGmuCore = gmu, 1 ShengGmuRscc = rscc, 2 ShengGmuPdc = pdc,
3 ShengGmuGpu = gpu, 4 ShengGmuCxMisc = cx-misc
```

The ladder's step 15 (`a6xx_gmu_rpmh_init`) has exactly 19 writes: 12 RSCC,
5 `SEQ_MEM`, 2 PDC.  Byte offsets come from the ladder's `RSCC_*` and `PDC_*`
macros (`RSCC_PDC_SLAVE_ID = 4*0x00b`, `PDC_GPU_ENABLE_PDC = 4*0x1140`, …).

### Windows

From the ADT (`tools/mkadt.py`) and the ladder:

| index | window | ADT base | bytes | SOC_BASE-relative physical |
| --- | --- | --- | --- | --- |
| 0 | gmu | 0x2d68000 | 0x37000 | 0x3d68000 |
| 1 | rscc | 0x2d50000 | 0x10000 | 0x3d50000 |
| 2 | gmu_pdc | 0xa290000 | 0x10000 | 0xb290000 |
| 3 | gpu | 0x2d00000 | 0x40000 | 0x3d00000 |
| 4 | cx_misc | 0x2d9e000 | 0x1000 | 0x3d9e000 |

**Open detail**: `ShengGmuBindMmio()` compares each window's `physical` and
`bytes` against the generated arrays exactly.  The service currently fills the
GMU adapter from the ADT values (0x2d68000…) and the GPU/CX windows from its
mapped VAs.  The generator must therefore emit whatever the service will pass
(`NativeAdrenoGmuService::windowAddress`, and the GPU/CX mapping): decide
ADT-relative or SOC_BASE-relative before generating, or the bind fails with
`ShengGmuArgument` and the log says "GMU windows are not usable".

### Chip ids, firmware, command DB

| item | value | source |
| --- | --- | --- |
| `SHENG_GMU_CHIP_ID` (votes) | 0x07030000 | A730 `ADRENO_CHIP_IDS` |
| `SHENG_GMU_FW_CHIP_ID` | 0x07030100 | ladder `A730_GMU_CHIPID` |
| GMU firmware | `gmu_gen70000.bin` | 8 MiB fwarea / `~/liuqin-root/fw` |
| SQE firmware | `a730_sqe.fw` | same |
| command DB | 0x80860000, 0x20000 | ladder `CMD_DB_PHYS`, `CMD_DB_SIZE` |

The firmware is **not embedded** in the kernel: the weak
`native_adreno_firmware_image` is empty.  The generator wrote its sha256 and
size for the A740; for liuqin it must hash `gmu_gen70000.bin` instead, and the
service needs a way to see that image (the fwarea is already loaded by
`liuqin_fw_get()`, so the service can copy it from there rather than embedding
8 MiB in the kernel).

### Votes: OPPs, bandwidths, BCM

The vendor DTB has the data the mainline DTS lacks.  `/soc/qcom,kgsl-3d0@3d00000`
carries `qcom,bus-table-ddr` (the bandwidth table) and 13
`qcom,gpu-pwrlevels` children with `qcom,gpu-freq`, `qcom,level` (ARC),
`qcom,bus-freq` / `qcom,bus-min` / `qcom,bus-max` (indices into the DDR table)
and `qcom,acd-level`; the node also has `qcom,chipid = 0x07030002` and
`qcom,ubwc-mode = 4`.  So:

- GPU OPPs: the 13 pwrlevels -> `hz`, `level`, `acd`; `kbps` =
  `qcom,bus-table-ddr[qcom,bus-freq]` (byte/s -> kB/s).
- GMU OPPs: three levels from the ladder's `gmu_khz[]`/`gmu_level[]` (the GMU
  node's own pwrlevels may not be in the base DTB; check the liuqin overlay).
- A730 BCM policy (SH0/MC0/ACV buswidths, ACV perfmode) is in Linux's
  `a6xx_catalog.c`; fetch it (the sheng generator pins a commit) and take the
  A730 record.  `SHENG_GMU_VOTES_CHIP_ID` stays 0x07030000 (the GPU chip id);
  the DTB's 0x07030002 is the revision, not the HFI id.
- AO/CM3 fatal masks: the Sheng header uses `WDOG_BITE`, `FENCE_ERR`,
  `HOST_AHB_BUS_ERROR` and `CM3_FAULT` bits; derive them from the A6XX XML.

## Checkpoints

1. **Generator + headers** — `tools/prepare-liuqin-gmu.py` emits the three
   `*Source.h` from: ladder constants, fwarea firmware, ADT windows, A730
   catalog/XML.  Add a `--check` mode that re-derives the ladder's 19 writes
   and the register offsets and fails on mismatch.  Expected kernel strings:
   `GMU five-window I/O adapter bound` stays, and the "not generated"
   messages disappear.
2. **GPU context** — call `NativeAdrenoGpuContextBringup()` after the GMU
   context in `NativeAdrenoGpuService::start()`, with a second bank/ASID and
   two SMR slots; set `gpu_context_ready_`.  Use
   `NativeAdrenoGpuContextBuffers()` for ring/fence/SQE/pwrup instead of the
   GMU `vm_`, and `NativeAdrenoGpuContextVm()` for the user client.
3. **Boot arguments** — `sheng-gpu-bringup=1`, GMU and GPU bank/asid/slot/sid
   pairs, `sheng-gpu-cmddb-version`, firmware from fwarea; run with
   `liuqinadreno=0` so the ladder does not touch the same hardware.
4. **Hardware** — expected order: GMU validated → ColdStart `fw_ready=1
   hfi_ready=1` → GPU context Ready → ring attached → CP started →
   `native-adreno-state = ready`; then `newUserClient` stops refusing.
5. Only then userspace (Mesa/Turnip/NativeMetal).

## Why not route B

Adopting the ladder's state would need a `NativeAdrenoVm` over the ladder's
`gpu_space` page tables (the kit's `attach()` wants an `ArmSmmu4K` config
handle, not raw tables) or a second implementation of the user-client ABI on
top of the ladder ring.  Both touch the ABI userspace already speaks; route A
reuses the service's existing, tested-in-other-trees path and keeps the ABI.

## Stage 1 landed (2026-10-23): the cold-start path is compiled in

- `ShengGmu.c`, `ShengGmuVotes.c` and `ShengGmuCommandDb.c` are ported from
  `~/liuqin-root/refs/UniXNU-sheng/package` (BSD-2) into `iokit/NativeAdreno`
  and listed in `iokit/conf/files.arm64`.
- `tools/prepare-liuqin-gmu.py` generates the three headers from data that is
  in this repository or on the tablet:
  - `ShengGmuSource.h`: 25 register values (window-0 relative dwords,
    `SHENG_GMU_CORE_GPU_DELTA = 0`), the 19 RSC/PDC writes, the five windows,
    the GMU firmware size/sha256 (**57,272 B**, `b2d3f0e2…`), and the AO/CM3
    fatal masks;
  - `ShengGmuVotesSource.h`: 13 GPU OPPs from the vendor DTB's
    `qcom,gpu-pwrlevels`, 3 GMU OPPs from the ladder, 6 bandwidth levels from
    `qcom,bus-table-ddr`, and the BCM policy - **SH0 16 / MC0 4 / ACV
    perfmode 32, 12,000 kB/s is the one value not in mainline; it is the
    first thing to revisit if ColdStart refuses the votes**;
  - `ShengGmuCommandDbSource.h`: 0x80860000/0x20000.
- `NativeAdrenoService.cpp`: the GMU firmware now comes from the boot image's
  firmware area via `liuqin_fw_get("gmu_gen70000.bin")` (the weak embedded
  image is empty), and the CX window is looked up by `reg-names = "cx_misc"`
  instead of assuming index 1 (index 1 is `cx_dbgc`).
- The "not generated" strings are gone; `ShengGmuColdStart` is in the kernel.

### The first cold-start image

`shim/out/shim-boot-gmu.img` (`d4c48f2e…`) appends to the usual command line:

```
liuqinadreno=5 liuqinsvc=1 sheng-gpu-bringup=1
sheng-gpu-bank=1 sheng-gpu-asid=1 sheng-gpu-sid=5 sheng-gpu-slot=0
sheng-gpu-cmddb-version=1
```

`liuqinadreno=5` lets the ladder switch the CX/GMU/SMMU clocks on and stop
before it touches the GMU; the bank/ASID/SID/slot are the ones the ladder
picked on hardware (context 1, SMR 0, stream 5).  Expected order:

```
NativeAdreno: ... CX window at 0x2d9e000/0x1000
NativeAdreno: GMU five-window I/O adapter bound
NativeAdreno: SMMU id0=... 
NativeAdreno: GMU payloads mapped, bank=1 asid=1 sid=5 slot=0 ttbr0=...
NativeAdreno: GMU firmware validated (57272 bytes)
NativeAdreno: ColdStart status=0 fw_ready=1 hfi_ready=1 stage=...
NativeAdreno: ring/SQE refused: the GPU context (iommus 0/1) is not built
```

`status=0 fw_ready=1 hfi_ready=1` is the milestone; the ring/CP refusal is
expected until the next stage.  Artifacts: kernel `db5577b1…` (16,834,656 B),
`shim-boot-gmu.img` `d4c48f2e…`, default image `56dc6823…`.

## Stage 1 on hardware (2026-10-23 night)

The `shim-boot-gmu.img` boot ran the new code: the CX window is now mapped at
0x2d9e000/0x1000, the SMMU reads back `id0=4c017e18 id1=40000016 id2=5511
scr0=002e0406`, the five-window GMU adapter binds, and the cold-start path is
reached.  It stops one allocation into it:

```
NativeAdreno: GMU context stopped at step 1 status 0xe00002c7; resources retained
```

`0xe00002c7` is `kIOReturnUnsupported`, and step 1 is Allocate: the GMU
allocator's first buffer is the 16 MiB ICACHE (`0xffc000`) requested
physically contiguous.  When the allocator cannot honour the contiguity flag,
`NativeAdrenoMemory::allocate()` ends up with scattered segments and returns
Unsupported.  The SMMU maps page by page, so contiguity is not required
(Linux's `adreno_gmu_allocate` uses IOMMU-mapped GEM objects too).

Patch 0094 retries such an allocation without the contiguous flag and logs the
fallback and the failing size, so the next boot either gets past Allocate or
names the buffer that cannot be had at all.

Rebuilt image: kernel `2787b458…` (16,835,552 B), `shim-boot-gmu.img`
`6d5b2dfb…`, default `shim-boot.img` `f7cb672a…`.  Expected next lines:

```
NativeAdrenoGmu: 16760832 bytes not contiguous, retrying scattered
NativeAdreno: GMU payloads mapped, bank=1 asid=1 sid=5 slot=0 ttbr0=0x...
NativeAdreno: GMU firmware validated (57272 bytes)
NativeAdreno: ColdStart status=0 fw_ready=1 hfi_ready=1 stage=...
```

If instead the new log says `16760832 bytes allocation failed 0x...`, the
buffer is failing for a different reason (address width, alignment, or no
memory at all) and that status is the next clue.

### The allocation was blocked by the 16 KiB kernel page

Second boot with 0094:

```
NativeAdrenoGmu: 16760832 bytes not contiguous, retrying scattered
NativeAdrenoGmu: 16760832 bytes allocation failed 0xe00002c7
```

Both the contiguous and the scattered attempt fail with the same
Unsupported, which means the failure is before the descriptor:
`NativeAdrenoMemory::allocate()` starts with `NativeAdrenoQueryCpu4K()`, and
that helper only accepts a 4 KiB-granule kernel
(`PAGE_SIZE == 4096`, `vm_map_page_size(kernel_map) == 4096`, TG0=0, TG1=2).
This port is built with **16 KiB** pages, so every allocation returned
Unsupported.

Patch 0095 accepts both granules (TG0/TG1 are 2/1 for 16 KiB) and keeps the
write-combine path off on 16 KiB pages: `NativeAdrenoSetPagesCacheMode()`
programs `pmap_set_cache_attributes()` by 4 KiB page number, which on a
16 KiB kernel would touch unrelated pages.  The driver's explicit cache
maintenance (`NativeAdrenoMaintainOwnedPages`) still covers every buffer.

Rebuilt: kernel `5e32e4b1…` (16,835,552 B), `shim-boot-gmu.img`
`267582ea…`, default `shim-boot.img` `a5c19869…`.  The next boot should get
past the GMU context's Allocate/Prepare/Program/Attach/VmAttach/Map steps and
into firmware load + ColdStart; `NativeAdrenoGmu: ... allocation failed` with
a different size would name the next buffer that does not fit.

### Third boot: GMU context completes, startup config refused

With 0095 the GMU allocator and SMMU context finally ran through:

```
NativeAdreno: GMU payloads mapped, bank=1 asid=1 sid=5 slot=0 ttbr0=0x100084711c000
NativeAdreno: GMU firmware validated (57272 bytes)
NativeAdreno: startup config refused
```

So the GPU context/memory/table path works on 16 KiB pages, and only the HFI
startup config is refused.  Two things were wrong or unknown:

- `AdrenoHfiBuildBandwidth()` requires `kbps[0] == 0` and strictly ascending
  levels; `prepare-liuqin-gmu.py` filtered the zero OPP out, so the bandwidth
  table started at 1720 and was refused.  Fixed (7 levels, starting at 0).
- The refusal gives no clue which of the six checks failed, so
  `ShengGmuBuildStartup()` now records `sheng_gmu_vote_stage` (1 chip/args,
  2 GPU ARC votes, 3 GMU ARC votes, 4 BCM aux, 5 bandwidth, 6 final validate)
  and the sizes/addresses that went in; the service logs them.

New image: kernel `51ad7a6b…`, `shim-boot-gmu.img` `d2e9d6df…`, default
`shim-boot.img` `1fa8458c…`.  The next log either shows
`ColdStart status=...` or exactly which stage refused:

```
NativeAdreno: startup config refused stage N (gfx … mx … cx … sh0 … mc0 … acv … cn0 0x… chip 0x…)
```

The stage number is the next thing to act on: 2/3 means the command-db ARC
tables do not match this SoC's format, 4 means the BCM aux bytes are not the
8-byte record the A740 code expects, 5 means an address/size in the BCM
policy (the guessed SH0/MC0/ACV values).

### Fourth boot: startup config accepted, ColdStart got a zero device

The zero-bandwidth fix worked (no "startup config refused"), and ColdStart ran:

```
NativeAdreno: ColdStart status=1 fw_ready=0 hfi_ready=0 stage=0
```

Status 1 is `ShengGmuArgument`, and it comes from the first thing
`ShengGmuColdStart()` does: `ShengGmuLoadAndStartHfi()` validates
`d->io.read32/write32/barrier` and `d->services.sha256/now_us/wait_us`.  The
service built `gmu_io_` and `services` but never copied them into
`gmu_device_`, so the device was all zeroes and the validation refused it
before any register access.

Patch 0097 sets `gmu_device_.io = gmu_io_` and
`gmu_device_.services = services` right before the call.  Rebuilt: kernel
`1acebdee…`, `shim-boot-gmu.img` `bd7450b9…`, default `shim-boot.img`
`a340e62f…`.

The next step in the framework is the GMU load: reset/retention/HFI status
reads, the 19 RSC/PDC writes, FAL, TCM retention, HFI queue init, firmware
blocks (it expects 15 from `gmu_gen70000.bin`), TCM readback, then the
`FW_INIT_RESULT` poll and HFI start.  A status other than 0 now comes from one
of those and is worth reporting verbatim.

### The 0097 boot reset after the ladder

The next boot printed `LIUQIN BOOT: gpu ladder done at 47.024 s` and then the
tablet reset, before any `ColdStart status` line.  That is the first boot in
which `ShengGmuColdStart` actually touches the GMU (io/services are wired now),
so the reset is either the first GMU register access with the clocks left by
`liuqinadreno=5`, one of the 19 RSC/PDC writes, the FAL/TCM-retention writes,
or the CM3 release + firmware start.

`shim-boot-probe.img` (kernel `541d1cf9…`, sha256 `58958a3d…`) narrows it:

- `liuqinmute` is removed, so the console shows every `IOLog` line;
- `sheng-gpu-readonly=1` reads CM3_SYSRESET, HFI_CTRL_STATUS,
  CX_MISC_TCM_RET_CNTL, RSCC_SEQ_BUSY, AO_HOST_INTERRUPT_STATUS and
  GMU2HOST_INTR_INFO through the same five-window adapter and stops before any
  write.

If `NativeAdreno: readonly sysreset ...` appears, register access is fine and
the reset comes from the write/firmware path; if the board resets before that
line, the GMU window is not safely accessible with the ladder stopped at step
5 and the ladder has to be run further (or the GMU powered explicitly) before
the cold start.

### Probe result: reads are safe, so the reset is in the write path

The probe boot (no reset) printed:

```
NativeAdreno: readonly sysreset 0x1 hfi 0x0 cx 0x0 rscc 0x0 ao 0x0 cm3 0x0
```

All six state registers read cleanly, and the values are exactly what
`ShengGmuLoadAndStartHfi()` expects before it may write: CM3 held in reset
(1), HFI not ready, TCM retention cleared, RSC idle, no AO/CM3 fault.  So the
GMU window is accessible with the ladder stopped at step 5 and the reset in
the 0097 boot comes from a write, not from a read.

That same probe boot then failed the HFS+ root mount for ~37 retries
(`unknown Volume Signature : 666b`), but the next boot mounted and ran
userspace to 1305 s, so userdata is not corrupted - the failed mount was a
per-boot effect (the console was unmuted in that image).

### Bisect the write path

`sheng-gpu-stop=N` makes `ShengGmuLoadAndStartHfi()` return right after stage
N, without any cleanup write:

| N | stops after |
| --- | --- |
| 1 | the 19 RSC/PDC writes and their barrier |
| 2 | the FAL writes and the TCM-retention set/readback |
| 3 | the firmware load and the TCM readback |
| 4 | the config-register readback (before the CM3 reset release) |

Images `shim-boot-stop1.img` … `shim-boot-stop4.img` are unmuted so the
`ColdStart status=... stopped_at=N` line stays on screen even if the board
resets immediately afterwards.  Boot them in order, stop at the first reset:
that N names the stage that hangs.  If 1 resets, the RSC/PDC window/offsets
are wrong; if 3 resets, the firmware load into TCM/ICACHE is; if 4 resets, the
CM3 release itself is.

### Stop 1-4 all boot: the reset is the CM3 release / firmware run

All four stop images reached the Apple logo progress bar, so the RSC/PDC
writes, FAL + TCM retention, firmware load + TCM readback and the config
readback (with CM3_SYSRESET held at 1) are all safe.  The reset in the full
cold-start image therefore happens at `CM3_SYSRESET = 0` or the first
firmware fetch after it.

The prime suspect is the GMU's SMMU context: the CM3 starts fetching from
ICACHE at IOVA 0x4000 through stream 5, and if that stream is not routed to
bank 1 exactly like the ladder routed it (SMR0 = 0x84000005 after the ladder
run), the fetch faults or bypasses into DRAM.

`shim-boot-probe2.img` (kernel `ae071c60…`, sha256 `f583807e…`) extends the
read-only probe with `smmu scr0 smr0 s2cr0 smr1 s2cr1 cb1_sctlr` after the
GMU context is built.  Compare those with the ladder's values:

| register | ladder (liuqinadreno=22) |
| --- | --- |
| SMR0 | 0x84000005 (stream 5) |
| S2CR0 | context 1, type 0 |
| SCR0 | 0x002e0406 (SMMU enabled) |

If SMR0/S2CR0 are 0 or type 2 (bypass), the port's context is not routing the
GMU stream and that is the reset.

### The qsmmu traps context-bank reads

The first SMMU-context probe panicked with a kernel data abort right after
printing the `readonly` line: the extra read of the context-bank SCTLR at
offset 0x8000 traps.  This is a `qcom,qsmmu-v500` window: the hypervisor
emulates the global registers (SCR0/SMR/S2CR) but not the context-bank space.
The ladder only ever touches the global registers there.

`shim-boot-probe3.img` (kernel `5ca024b5…`, sha256 `3b40709a…`) keeps
`scr0/smr0/s2cr0/smr1/s2cr1` and drops the context-bank read.  The ladder's
values for comparison: SCR0 = 0x002e0406 (enabled), SMR0 = 0x84000005
(stream 5), S2CR0 = context 1/type 0.

### The GMU stream match is invalid

probe3 printed:

```
NativeAdreno: smmu scr0 0x2e0406 smr0 0x00000005 s2cr0 0x1 smr1 0x0 s2cr1 0x20000
```

SCR0 is enabled and S2CR0 is context 1, but SMR0 has no VALID bit
(0x00000005 instead of 0x80000005).  The ladder writes
`SMR = VALID | (0x400 << 16) | sid` = **0x84000005**; the kit's
`ArmSmmu4KAttachStreams()` writes `VALID | sid` and does not set the stream
mask.  Whatever the qsmmu does with that (it leaves the valid bit clear), the
result is that stream 5 does not match: the CM3's first fetch at IOVA 0x4000
falls through to the default fault/bypass policy, which matches the reset.

`shim-boot-route.img` (kernel `beed603b…`, sha256 `d4762dcc…`) writes the
ladder's exact pair right after the GMU context is built, when
`sheng-gpu-route=1` is given, and logs the readback:

```
NativeAdreno: route smr0 0x84000005 s2cr0 0x1
```

then runs the full cold start unmuted.  If the readback keeps the VALID bit
and the CM3 still resets, routing is not the whole story; if the readback is
0x5, the qsmmu rejects this write and the context has to be built differently.

### The route fix works: the CM3 runs, but the 10 ms poll times out

With the ladder's SMR/S2CR pair the full cold start no longer resets:

```
NativeAdreno: route smr0 0x84000005 s2cr0 0x1
NativeAdreno: ColdStart status=6 fw_ready=0 hfi_ready=0 stage=0
```

Status 6 is `ShengGmuTimeout` from the `FW_INIT_RESULT` poll.  The ported
`poll()` gives the firmware 10 ms from start to ready; the A730 CM3 boots by
fetching ICACHE through the SMMU and can need longer.  The poll is raised to
100 ms (the pinned 10 ms was for the A740) and the ColdStart line now also
prints `fw_init=<last value> hfi=<value> polls=<count>`, which distinguishes
"not ready yet" (0) from "wrong mask" (e.g. the firmware writes 0xbabeface
while the code waits for 0x100).

`shim-boot-route2.img` (kernel `8faeca0e…`, sha256 `67efd4cb…`) runs the same
route path with the longer poll.

### The firmware never ran because the ICACHE was still in the CPU cache

`fw_init=0x0 polls=783` with a 100 ms budget: the CM3 never wrote
`FW_INIT_RESULT`.  The firmware and the HFI queue table are written with
ordinary cacheable CPU stores, and the GMU reads ICACHE through the SMMU.
The ladder always calls its `to_memory()` (clean to PoC) after loading the
ICACHE and after initialising the HFI queue; the Sheng framework only did a
`dsb`, so the GMU could fetch stale (zero) words.

Add a `clean` callback to `ShengGmuServices` and call it for icache, dummy,
debug, log and hfi right after the firmware load, before the CM3 release.
`NativeAdrenoGmuPlatform::services()` points it at
`NativeAdrenoMaintainOwnedPages(range, bytes, NativeAdrenoClean)`.

`shim-boot-route3.img` (kernel `26e4407b…`, sha256 `53869602…`) is the same
route path with the cache clean.

## Cold start verified on hardware

```
NativeAdreno: route smr0 0x84000005 s2cr0 0x1
NativeAdreno: ColdStart status=0 fw_ready=1 hfi_ready=1 stage=1 stopped_at=0 fw_init=0x100 hfi=0x1 polls=20
```

`ShengGmuColdStart()` returns `ShengGmuSuccess`: the CM3 was released, the
firmware reached `FW_INIT_RESULT = 0x100` in 20 polls (~2 ms), the HFI
controller reported ready (`0x1`), and `AdrenoHfiStartupRun()` completed the
startup config (stage 1) — the vote tables, ARC levels and BCM policy built
from the A730 command DB are accepted by the GMU.

The fixes that got here, in order:

| # | fix |
| --- | --- |
| 0094 | retry the GMU buffers without physical contiguity |
| 0095 | accept the 16 KiB kernel page in `NativeAdrenoQueryCpu4K()` |
| 0096 | bandwidth table must start at 0 (`AdrenoHfiBuildBandwidth`) |
| 0097 | hand `gmu_io_`/`services` to `ShengGmuDevice` before the cold start |
| 0102 | write the ladder's SMR/S2CR pair for stream 5 (VALID + mask) |
| 0103 | 100 ms `FW_INIT_RESULT` poll instead of the A740's 10 ms |
| 0104 | clean ICACHE/HFI to PoC before the CM3 release |

## Next: the GPU context and the command processor

After the cold start the service logs that the ring/SQE are refused because
the GPU context is not built (`gpu_context_ready_ == false`).  The pieces are
ready in the tree:

- `NativeAdrenoGpuContextBringup(smmu, bytes, bank, asid, slot0, sid0,
  slot1, sid1, progress, report)` builds bank 2 / asid 2 with streams 0 and
  1, a 2 GiB TTBR0 GPU space and the fixed buffers (SQE 0x100000, ring
  0x200000, memptrs 0x300000, pwrup 0x301000, scratch 0x302000).
- `NativeAdrenoGpuContextBuffers(out[5])` reports their physical/iova/bytes;
  `NativeAdrenoGpuContextVm()` is the VM for the user client.
- The service then has to submit the A730 SQE/pwrup (the ladder's steps 19-21
  are the proven sequence) and drop the old `ring_mem_`/`vm_.map` path when
  the GPU context is ready.

The GPU bank/asid/slots/sids should become boot arguments (default 2/2/0/0/
1/1) so the ladder's chosen values can be overridden without a rebuild.

### GPU context bring-up wired

After a successful cold start the service now builds the GPU's own context:

```
NativeAdreno: GPU context status 0x0 step 7 (bank 2 asid 2 slots 1/2 sids 0/1)
NativeAdreno: GPU buffers sqe 0x100000 ring 0x200000 memptrs 0x300000 pwrup 0x301000 scratch 0x302000
```

- bank/asid default to 2/2 (`sheng-gpu-gpubank`, `sheng-gpu-gpuasid`);
- the two SMR slots default to the first two free entries after the GMU's
  (`sheng-gpu-gpuslot0/1` override them). "Free" is SMR without VALID and
  S2CR type 2 (fault);
- streams 0 and 1 both route to bank 2;
- `NativeAdrenoGpuContextBuffers()` reports the fixed IOVAs above.

The old ring/SQE block still refuses because `gpu_context_ready_` is not yet
set: the CP submission (SQE microcode, power-up list, ring doorbell) is the
next piece.

## The desktop wall is not the GPU: SpringBoard waits in the BKS check-in

Disabling the two HID plugin directories in the root image
(`SessionFilters.off`, `ServicePlugins.off`) let backboardd finish
`BKHIDSystem init`: the process sampler now completes all 40 samples (before,
it stopped at sample 8 inside the plugin enumeration), and SpringBoard moved
past `_BKSDisplayServicesStart`.

The last samples (sample 39, ~80 min in) show:

```
SpringBoard t0: mach_msg2_trap <- _dispatch_mach_send_and_wait_for_reply
  <- _xpc_connection_send_message_with_reply_sync
  <- -[BSXPCServiceConnectionMessage sendSynchronouslyWithError:]
  <- -[BKSSystemShellService _checkIn] <- _FBSystemShellInitialize
  <- _SBSystemAppMain

backboardd t0: idle in its CFRunLoop
backboardd t8: dispatch_group_wait inside a block (main binary)
```

and backboardd probes `IOAcceleratorES`/`IOAcceleratorESLegacy` 533 times
over the boot, always "none".  `ios-sweet`'s NOTES documents the same wall
("no GPU, so no CoreAnimation renderer yet") and later reached the GUI with
CPU rendering after the display-mode/scale and root-state fixes — without a
GPU driver.  So liuqin is at the same point: the system shell check-in never
returns because backboardd's display/renderer bring-up is waiting.

Next candidates, in order:

1. compare liuqin's display properties with sweet's working values
   (`/chosen/display-scale`: liuqin 2 for 1800x2880, sweet 3 for 1080x2400;
   the IOMFB frame-info/refresh selectors must return a non-zero mode);
2. instrument the backboardd check-in handler (the dispatch group's
   outstanding task) to see whether it waits for the accelerator or for the
   display transaction;
3. only then decide whether a stub accelerator service or a different
   renderer path is needed.

### CP step 1: load the A730 SQE microcode into the GPU context

`NativeAdrenoGpuContextBufferCpu(index)` exposes the CPU mapping of the five
GPU-context buffers.  With `sheng-gpu-cp=1` the service copies
`a730_sqe.fw` (from the fwarea) minus its header word into the context's sqe
buffer, zeroes ring/memptrs/pwrup/scratch and cleans the sqe range, then
logs:

```
NativeAdreno: CP: sqe 74672 bytes at 0x100000 ring 0x200000 memptrs 0x300000
```

`shim-boot-cp1.img` (kernel `47e2d6a7…`, sha256 `7ec085e3…`) runs the ladder
to step 5, the GMU cold start with the route fix, the GPU context bring-up
and this load.  The CP register start (the ladder's step 20 list, CP_ME_INIT
through the ring) is the next step.

### The HFI startup needs more than 10 ms

The cp1 boot returned `ColdStart status=6 ... fw_ready=1 hfi_ready=1
stage=1`: the GMU was up but `AdrenoHfiStartupRun()` timed out at its first
stage.  `ShengGmuStartupLimits` was `{4,100,1000,10000}` - 10 ms for the whole
startup protocol, which the A730 misses under load (the earlier route3 boot
happened to fit).  Raise it to `{8,200,5000,200000}` (200 ms total).

`shim-boot-cp2.img` (kernel `41f932c6…`, sha256 `ae29af2d…`) repeats the cp1
test with the longer budget: it should print `ColdStart status=0`, then the
GPU context and `CP: sqe ...`.

### HFI startup: the per-command timeout, not the total, is the limit

With `{8,200,5000,200000}` the protocol still timed out, and the new fields
showed why:

```
proto stage=1 polls=22 transport=4 fwerr=0x0 elapsed=5232us
```

`transport=4` is `AdrenoHfiEmpty` (no response packet) and the loop stopped
after 22 polls / 5.2 ms - the per-command timeout (`command_timeout_us`),
not the 200 ms total.  The GMU simply does not answer the first startup
command within 5 ms.

`shim-boot-cp4.img` (kernel `f583306d…`, sha256 `1d5d1bb5…`) raises the
per-command timeout to 100 ms (512 polls x 200 us) and keeps the 200 ms
total.  If the GMU answers late, startup completes; if it stays Empty for
100 ms, the HFI queue/notification path is the problem (the earlier route3
boot did complete once, so the queue can work).

### The HFI queue was never visible to the GMU (cache, not timeout)

Even with a 100 ms per-command timeout the startup stayed Empty
(`polls=415 transport=4 elapsed=100182us`): the GMU never answered.  The
command queue is written with cacheable CPU stores, while the GMU reads it
through the SMMU, so the dirty lines never reached DRAM (the route3 success
was the lucky eviction).  The response side has the mirror problem: the CPU
reads stale lines until it invalidates.

`ShengGmuServices` gains an `invalidate` callback.  `notify()` now cleans the
16 KiB HFI region before raising `HOST2GMU_INTR`, and `startup_health()`
invalidates it before every response poll.

`shim-boot-cp5.img` (kernel `6ba2355f…`, sha256 `aa15e732…`).

### HFI response scan: stage 1 passes, then a kernel panic - reverted

The scan workaround let the first startup command through (the GMU's ACK is
a real packet in the response buffer) but the queue indices never advance, and
two follow-on problems appeared:

- with the scan advancing read_index, the next poll took the ring path, read
  data[4]=0 and reported Corrupt (status=10, stage 2);
- with the scan keeping read==write (rescanning every poll), the cp14 boot
  panicked in `_kalloc_type_view_parse` from `NativeAdrenoGpuService::start`,
  i.e. an allocation after the protocol failure hit corrupted kernel
  metadata.

The scan is removed again (0120).  The remaining fact stands: this Gen7 GMU
publishes the response packet but does not update the HFI queue header
indices (m[1] and the queue sizes are rewritten by the firmware too).  The
next approach should port the ladder's HFI transport (`hfi_table_init` after
the GMU start, interrupt wait/clear then direct queue read) instead of
extending the a6xx kit's index-based poll.

Image: shim-boot-cp15.img 092f196b..., kernel a1fe0d25....

### The interrupt transport (0121): stop trusting indices this GMU never updates

`docs/GPU_NEXT.md` above recorded the wall: the GMU publishes a real ACK
packet in the response window and raises `GMU2HOST_INTR`, but both queue
index pairs stay at zero, and the firmware also rewrites table fields
(`m[1]` 168 -> 160, queue `size` 1024 -> 16384).  The kit's `AdrenoHfiPoll`
is index-driven, so it reports `Empty` while an answer sits in memory; the
scan attempt (0116/0119) fixed stage 1 but desynchronised the ring and ended
in a `_kalloc_type_view_parse` panic (0120 removed it).

The ladder (`bsd/dev/liuqin_adreno.c`, `hfi_send`) never trusts the indices
either.  It writes a message, pulses `HOST2GMU_INTR`, waits for
`GMU2HOST_INTR_INFO` bit 0, clears it, invalidates the queue memory and reads
the answer.  Patch 0121 implements exactly that as a second cold-start path,
`ShengGmuColdStartInterrupt()`, selected with `sheng-gpu-hfi=1`:

1. zero the first 19 x 4 words of the command and response windows, so a
   stale packet can never pass for an answer;
2. publish the command at the base of the command window with the command
   indices consistent (`read 0`, `write padded`) - the ring is reset to this
   base state for every message, so whatever the firmware does with the
   indices cannot accumulate;
3. clean the command window, the response window and the queue table, drop a
   stale `GMU2HOST_INTR`, pulse `HOST2GMU_INTR`;
4. bound-wait (512 polls x 200 us, 100 ms per command) for
   `GMU2HOST_INTR_INFO` bit 0, acknowledge it with `INTR_CLR`;
5. invalidate the table and the response window, then accept a packet only if
   its length is 1..19, its message id equals the command id and its return
   header carries this command's sequence number; four word-aligned base
   slots are checked (the firmware may pad the start).

Nothing advances or rewinds an index the firmware owns, and no packet is
accepted without a fresh sequence number, so the failure mode of 0116 cannot
recur.  A firmware error packet (id 100) is surfaced as
`transport=7 fwerr=...` instead of being skipped.  `sheng-gpu-hfi=0` (default)
keeps the kit transport, so the two can be compared on one boot line.

The same patch adds one diagnostic line pair (`mode=intr` runs only):

```
NativeAdreno: ColdStart status=... mode=intr ... transport=... elapsed=... us
NativeAdreno: HFI intr inv=0x... tbl <6 words> intr=0x...
NativeAdreno: HFI intr q0 rd/wr <2> q1 rd/wr <2> rsp <4> cmd <4>
```

Reading the result:

- `status=0 mode=intr` - all startup messages were acknowledged and the GPU
  context / `CP: sqe ...` block runs next (that is the next milestone);
- `transport=4` (`Empty`, no interrupt) - the GMU never raised the doorbell
  for this message; the problem is the `HOST2GMU_INTR` notification path,
  not the queue;
- `transport=6` (`Unexpected`) with `intr=0x1` - the GMU answered but the
  packet is not where the model expects it: the `rsp` and `cmd` words in the
  dump say where it went;
- `transport=7` - the firmware rejected the message; `fwerr` carries its code.

Image: `shim-boot-hfi.img` sha256 `bba49838…`, kernel `3d6336e7…`
(boot line = the cp15 line plus `sheng-gpu-hfi=1`).

### Series repair (0112 / 0120)

`tools/verify-patches.sh` found three patches that did not apply:

- 0112 added `config.hfi.cpu[27..29]` to the failure dump but not the matching
  `q1 %08x %08x %08x` format string, so 0113's context no longer matched.  The
  format change is now part of 0112 (the dump prints all fifteen words).
- 0120 removed the response scan with a context that predated 0119's comment;
  it is regenerated from the real state after 0119.

With those two repairs the whole series (0001..0121) applies cleanly and the
resulting tree is byte-identical to the build tree for every file touched by
0121.


### First interrupt-transport boot: refused before the GMU was touched (0122)

The `sheng-gpu-hfi=1` image printed:

```
NativeAdreno: ColdStart status=1 mode=intr fw_ready=1 hfi_ready=1 stage=1
  stopped_at=0 fw_init=0x100 hfi=0x1 polls=20 proto stage=1 polls=0
  transport=4 fwerr=0x0 elapsed=4 us
```

`status=1` is `ShengGmuArgument`, not a GMU answer: the new exchange capped the
**command** packet at 19 words, a limit that belongs to ACKs.  The first
startup message (the performance table) is ~150 words, so it was refused
before any register write (`polls=0`, 4 us), and the failure path then
re-asserted reset (`sysreset=0x1`) - which is exactly what the dump below the
line shows.  Nothing about the GMU or the interrupt path was tested.

0122 separates the two bounds (`SHENG_HFI_PACKET_WORDS 19` for responses,
`SHENG_HFI_COMMAND_WORDS 254` for commands, the ring is 1024 words), raises
the sequence's total budget from 200 ms to 2 s (six messages of up to 100 ms
each could not fit), and adds `ack=0x%x` to the ColdStart line so the
acknowledged-stage mask is visible even on failure.

Image: `shim-boot-hfi2.img` sha256 `2e8b83c8…`, kernel `af323341…`.

### HFI actually works: the indices were right all along (0123)

The `hfi2` boot (`sheng-gpu-hfi=1`, kernel `af323341…`) is the first time the
GMU accepted and answered startup messages:

```
NativeAdreno: ColdStart status=10 mode=intr fw_ready=1 hfi_ready=1 stage=6
  stopped_at=0 fw_init=0x100 hfi=0x1 polls=21 proto stage=6 polls=10
  transport=6 fwerr=0x0 ack=0x1e elapsed=1234 us
NativeAdreno: HFI q0 00000001 60009000 00000a00 00000400 ... 00000004 00000004
NativeAdreno: HFI q1 00000001 6000a000 00000a01 00000400 ... 00000000 00000014
NativeAdreno: HFI intr q0 rd/wr 00000004 00000004 q1 rd/wr 00000000 00000014
  rsp 00000000 00000000 00000000 00000000 cmd 0050020e 00000000 fafafafa fafafafa
```

- `ack=0x1e` - bits 1..4: PERF_TABLE, BW_TABLE, ACD enable and ACD table were
  all published and **acknowledged** (real ACK packets with matching
  sequence numbers).  `hfi=0x1`, `ctrl_init=0x1`, `qtbl_addr=0x60008000`,
  `fw_init=0x100`: the controller and the queue table are live.
- `q0 rd/wr 4/4` - the GMU consumed every command and moved the command read
  index to our write index.
- `q1 rd/wr 0/0x14` - the GMU published five 4-word ACK packets; its write
  index is 20 words.
- `transport=6` at stage 6 (CORE_FW_START) was **my** bug: the exchange looked
  for the answer at the fixed slots 0/4/8/12, but the fifth ACK sits at word
  offset 16.  The earlier belief that "this GMU never advances the indices"
  came from the kit path, where no command had ever been consumed; with the
  interrupt transport the indices advance exactly as the ladder's code
  assumes.

0123 therefore replaces the base-slot model with the ladder's index-driven
transport, which needs no scanning and no window zeroing:

- publish at the command queue's `write` index, pad the packet to four words
  with `0xfafafafa`, clean the window, advance `write`, clean the table, pulse
  `HOST2GMU_INTR`;
- bound-wait for `GMU2HOST_INTR` bit 0, acknowledge it, invalidate the table
  and the response window;
- read the packet at the response queue's `read` index, advance `read` (and
  clean the table), skip F2H error messages and replies to other sequence
  numbers, accept only the ACK carrying this command's sequence number.

The dump now also prints twelve response words, so a misplaced packet is
visible instead of guessed at.

Image: `shim-boot-hfi3.img` sha256 `7339aa23…`, kernel `c1ec0d3d…`.
Expected on success: `ColdStart status=0 mode=intr ... ack=0xde` (six stages),
then the GPU context block and `CP: sqe ...`.

### Five of six startup stages acknowledged (0124)

The `hfi3` boot reached `ack=0x5e` and stopped at the last stage:

```
NativeAdreno: ColdStart status=1 mode=intr stage=7 ack=0x5e ...
```

`0x5e` = stages 1,2,3,4,6: performance table, bandwidth table, ACD enable,
ACD table and CORE_FW_START acknowledged; the index-driven transport read them
all correctly.  `status=1` (`ShengGmuArgument`) on stage 7 is the kit encoder's
choice, not the GMU's: `AdrenoHfiStageStart` encodes a **zero-word** payload
(the message is header-only, `AdrenoHfiStartupEncode` sets `count=0`), and
`hfi_exchange` rejected `!words`.  0124 accepts a zero-word payload for the
command side (`payload` may be unused then) and keeps the response bound at 19
words.  The START message is published as a 1-word packet padded to 4, like
the ladder's `hfi_send("START", ...)`.

Image: `shim-boot-hfi4.img` sha256 `7fd64f41…`, kernel `b5b2d443…`.
Expected on success: `ColdStart status=0 mode=intr ... ack=0xde`, then
`GPU context status 0x0 step N (bank 2 asid 2 ...)`, `CP: sqe ... bytes at ...`
and the ring attach.

### All six startup stages pass; the GPU context panics (0125 diagnostics)

The `hfi4` boot completed the whole HFI startup - and then panicked in the
GPU context bring-up:

```
NativeAdreno: ColdStart status=0 mode=intr fw_ready=1 hfi_ready=1 stage=8
  stopped_at=0 fw_init=0x100 hfi=0x1 polls=20 proto stage=8 polls=12
  transport=0 fwerr=0x0 ack=0xde elapsed=1473 us
NativeAdreno: HFI intr q0 rd/wr 00000018 00000018 q1 rd/wr 00000018 00000020
  cmd 00150304 ... rsp 00010304 00103b00 ... 00110303 0020a003 ... 0021030b 0030040b
panic(cpu 0 caller 0xfffffe0007b06368): Kernel data abort. at pc ... (saved ...)
```

`ack=0xde` = all six stages (PERF_TABLE, BW_TABLE, ACD enable, ACD table,
CORE_FW_START, START) acknowledged, `transport=0`, 1.47 ms total.  The queue
now shows six consumed commands (`q0 0x18/0x18`) and the ACKs still in the
response window, each four words (`00010304 00103b00`, `00110303 0020a003`,
`0021030b 0030040b`, ...), i.e. sequence 1, 2, 3 with id 4, 3, 11.

The panic follows the last debug line, so it is inside the GPU context block
(`NativeAdrenoGpuContextBringup`), before its `GPU context status` line.  The
screen photo is too blurred to read the pc/lr reliably (candidates resolve to
`NativeAdrenoGpuService::start+0x14xx` or to the dead `ArmSmmu4KBuild`), so
0125 makes the path self-describing instead: `GPU ctx call`, `GPU ctx
allocate object` / `object`, `enter tables ... aperture ... oas ...`, `tables
status ... map ...`, `buffers status ...`, `prepare start/status`, `program
status ... phase ... writes ...`, `attach status ... verified ...`, `vm attach
status ... spans ...`, and per-buffer `map N iova ... status ...`, plus the
`Step` callback from `bringUp`.  The last line printed before the panic then
names the step and the values that killed it.

Image: `shim-boot-hfi5.img` sha256 `8ea9a0ec…`, kernel `2e0ad0e4…`.

### Panic site narrowed; breadcrumbs and an escape hatch (0126)

The sharper photo of the `hfi5` boot gives pc `0xfffffe0007a8d418`, lr
`0xfffffe0007a8d3ec`, i.e. `NativeAdrenoGpuService::start` + 0x14d4 / + 0x14a8
- the same relative site as the `hfi4` panic (`start` + 0x14d4).  The console
still ends at the HFI dump (`HFI intr rsp ...`), and **no** `GPU ctx ...` line
appears, so the fault is between the dump and the first GPU-context log: the
four `PE_parse_boot_argn` calls or the 32-slot SMR/S2CR scan that picks the
two free stream slots.  The offsets cannot be resolved to a source line
because the unstripped kernel of that exact build is gone (the dSYM carries no
DWARF), so 0126 adds breadcrumbs instead of another guess:

```
NativeAdreno: A1 started=.. hfi_mode=.. slot=..
NativeAdreno: A2 bank=.. asid=.. smmu=.. bytes=..
NativeAdreno: A3 i=0 smr=.. s2cr=..   (every 8th slot)
NativeAdreno: A4 slots ../..
```

The last one printed names the faulting statement.  0126 also adds
`sheng-gpu-ctx=0`: with it the GPU context block is skipped (and logged as
skipped), so the boot reaches userspace and the rest of the GPU path
(`CP: no GPU context`) can be checked while this fault is bisected.

Image: `shim-boot-hfi6.img` sha256 `d76b184c…`, kernel `6cd9b68b…`.

### The fault is in the SMMU slot scan, at slot 17 (0127)

The `hfi6` boot printed breadcrumbs up to

```
NativeAdreno: A3 i=16 smr=00000000 s2cr=00020000
```

and then faulted, so the crash is one of the two register reads for slot 17
(the loop had no early exit and scanned all 32 entries even after it had two
free slots).  Two facts frame the fix:

- IDR0 read at boot is `0x4c017e18`, so `IDR0[7:0] = 0x18` = 24 stream-match
  registers; the ladder bounds its scan by exactly that field, our code used a
  hard 32 and read past what the hardware decodes;
- the scan kept running after `gslot0`/`gslot1` were filled, so the reads for
  slots above the chosen pair served no purpose.

0127 bounds the scan to the first 16 entries, logs every slot it reads
(`A3 i=.. smr=.. s2cr=.. free=..`), stops as soon as both slots are found, and
lets `sheng-gpu-gpuslot0`/`sheng-gpu-gpuslot1` skip the scan entirely.  If
fewer than two slots are free below 16 it reports
`GPU context: no free stream slots` instead of touching the faulting tail.

Image: `shim-boot-hfi7.img` sha256 `80724699…`, kernel `4820cf85…`.

### The GPU context is built; the CP ring now uses it (0128)

The `hfi7` boot is the first one with no panic at all, and the GPU context
comes up completely:

```
A1 started=0 hfi_mode=1 slot=0
A2 bank=2 asid=2 smmu=... bytes=262144
A3 i=1 smr=00000000 s2cr=00020000 free=1
A3 i=2 smr=00000000 s2cr=00020000 free=1
A4 slots 1/2
GPU ctx enter tables 4108 KiB aperture 0x80000000 oas 36 bank 2 asid 2 slots 1/2
GPU ctx tables status 0x0 bytes 4206592 map 0xfffffe4284a44000
GPU ctx buffers status 0x0 sqe 77824 ring 32768 maps ...
GPU ctx prepare status 0x0
GPU ctx program status 0x0 phase 5 writes 13
GPU ctx attach status 0x0 verified 2
GPU ctx vm attach status 0x0 spans 0
GPU ctx map 0..4 status 0x0 spans 1..5
GPU context status 0x0 step 7 (bank 2 asid 2 slots 1/2 sids 0/1)
GPU buffers sqe 0x100000 ring 0x200000 memptrs 0x300000 pwrup 0x301000 scratch 0x302000
CP: sqe 74672 bytes at 0x100000 ring 0x200000 memptrs 0x300000
```

- the whole HFI startup completed again (`ack=0xde`, `q0 rd/wr 0x110/0x110`,
  `q1 rd/wr 0x18/0x18`), so the ACKs are consumed through the real indices;
- slots 1 and 2 are free (`smr=0`, `s2cr` type 2) and the scan now stops before
  the faulting tail;
- the GPU's own SMMU context (bank 2, asid 2, streams 0/1) is programmed from
  scratch (13 register writes, phase 5), both routes verified, and the five CP
  buffers are mapped at 0x100000/0x200000/0x300000/0x301000/0x302000.

The one thing that did **not** happen was the ring: the next line was
`ring/SQE refused: the GPU context (iommus 0/1) is not built`, because
`gpu_context_ready_` was never set after a successful bring-up.  0128 fixes
that and replaces the legacy ring path - which allocated its own buffers and
mapped them into the *GMU* VM at iova 0 - with one that uses the GPU
context's buffers: ring at `GpuRingIova` (0x200000, 32 KiB), fence in the
memptrs page at `GpuMemptrsIova` (0x300000), cache maintenance through the
buffers' own physical pages.  The CP init (power-up list, ME_INIT) is left out
of this step on purpose: ring attach first, then the CP start on top of it.

Image: `shim-boot-hfi8.img` sha256 `93de7d13…`, kernel `2f3272b5…`.
Expected new line: `CP ring attached ring 0x200000/0x8000 fence 0x300000/0x1000`
(or `CP ring attach stopped at 0x...`).

### The ring is attached; the CP start is next (0129)

The `hfi8` boot printed the line the legacy path never reached:

```
NativeAdreno: CP ring attached ring 0x200000/0x8000 fence 0x300000/0x1000
```

so the CP's ring registers are programmed against the GPU context's own
buffers and `CP_RB_RPTR` reads back.  `publishing the GPU node without a
started CP` follows, which is accurate: only the ring exists so far.

0129 adds the CP start, matching the ladder's step 20 (the one sequence that
has been proven to run the A730's command processor on this board):

1. the a6xx_hw_init register list - RBBM_GBIF_HALT, SECVID_TSB, GBIF QOS,
   UCHE_GBIF_GX_CONFIG, PERFCTR, the NC_MODE_CNTL set, the hang interrupt,
   UCHE_CLIENT_PF/CMDQ, CP_APRIV/BV/LPAC_APRIV, UCHE trap/write-thru bases and
   CP_SQE_INSTR_BASE (the ring already wrote CP_RB_BASE/CNTL/RPTR_ADDR);
2. `CP_SQE_CNTL = 1`, a 20 ms wait, then `rbbm/hw_fault/rptr` logged;
3. the A740 power-up register list in the GPU context's pwrup page, then
   a7xx_cp_init (CP_THREAD_CONTROL + CP_ME_INIT with that list) through the
   ring, completed by the ring's timestamp fence (2 s budget).

New lines to look for: `CP registers written, starting the SQE`,
`CP SQE alive rbbm=.. hw_fault=.. rptr=..`, and either
`CP started fence=.. list=.. bytes` or `CP start stopped at 0x..`.

Image: `shim-boot-hfi9.img` sha256 `fd96301c…`, kernel `7097b58d…`.

### CP start: the power-up list went to the wrong page (0130)

The `hfi9` boot reached the CP and reported

```
CP registers written, starting the SQE
CP SQE alive rbbm=0x0 hw_fault=0x0 rptr=0
CP start stopped at 0xe00002ca (rbbm=0x0 hw_fault=0x0)
```

`0xe00002ca` is `kIOReturnIOError`, and no "fence not observed" line followed, so
the failure was inside `startCp`'s own steps rather than a CP timeout.  Two
things were wrong:

- the call passed the **fence** page where the power-up list belongs, so the
  list was built over the memptrs page and `CP_ME_INIT` would have read an
  uninitialised page at 0x301000;
- the failure was silent, so nothing said which step returned the error.

0130 passes `GpuPwrupIova`'s own buffer and logs every step with its status:
`CP SQE alive ... sqe=0x%x%08x (want 0x100000)` (the CP_SQE_INSTR_BASE readback
- this also tells whether the GPU register window at 0x2d00000 accepts writes,
which matters because the stock DT puts the GPU at 0x3d00000), then
`CP power-up list status=.. bytes=..`, `.. published 0x.. at 0x..` and
`CP ME_INIT submit 0x.. fence=..`.

Image: `shim-boot-hfi10.img` sha256 `91a18643…`, kernel `8223c6ab…`.

### The GPU register window does not answer (0131)

`hfi10` gave the decisive readback:

```
CP SQE alive rbbm=0x0 hw_fault=0x0 rptr=0 sqe=0x00000000 (want 0x100000)
CP power-up list status=0x0 bytes=144
CP power-up list published 0x0 at 0x8476cc000
CP ME_INIT submit 0xe00002ca fence=0
```

`CP_SQE_INSTR_BASE` reads back 0 after being written with 0x100000, and every
other GPU register reads 0, so the window at 0x2d00000 (the ADT's `gpu` node)
accepts no writes at all - the CP failure is downstream of that.

The addresses disagree:

- the vendor DTB the shim loads names the node `qcom,kgsl-3d0@3d00000` and the
  GMU `qcom,gmu@3d69000`, and the ladder (`LIUQIN ADRENO`, which really read
  `RBBM_STATUS`) uses `GPU_PHYS 0x3d00000`;
- our ADT says `gpu` 0x2d00000, `gmu` 0x2d68000/0x2d50000/0xa290000,
  `gpu-smmu` 0x2da0000 - exactly 0x1000000 lower, and the GMU *does* boot
  through those addresses, so the ADT is not uniformly wrong.

0131 therefore probes both: it reads `RBBM_HW_VERSION` (dword 0x0000) and
`RBBM_STATUS` (0x0210) through the ADT window and through a fresh mapping of
0x3d00000, logs both pairs, and keeps the window that answers.  Everything
downstream (ring IO, CP start) uses the chosen window.

Image: `shim-boot-hfi11.img` sha256 `7841d743…`, kernel `1ebf742c…`.
Expected new line: `GPU window ADT 0x2d00000 id=.. status=.. | DT 0x3d00000 id=.. status=..`
followed by `GPU window switched to the DT address 0x3d00000` when the DT one
is the live block.

### Both GPU windows read zero: the GX domain was never raised (0132)

`hfi11` showed

```
NativeAdreno: GPU window ADT 0x2d00000 id=0x0 status=0x0 | DT 0x3d00000 id=0x0 status=0x0
```

so the window address is not the problem: no GPU register answers through
either view.  Parsing the vendor DTB settles the addressing question - it
lists `kgsl-3d0@3d00000` with `reg = 0x3d00000/0x40000, 0x3d61000/0x800,
0x3d50000/0x10000, 0x3d8b000/0x2000, 0x3d9e000/0x1000, ...` and
`gmu@3d69000` with `reg = 0x3d68000/0x37000, 0xb290000/0x10000,
0x3d40000/0x10000`, i.e. the ADT's 0x2d... values are the same blocks with the
0x3d... range shifted down by 0x1000000 - and the GMU boots through the ADT's
0x2d68000, so that convention is what this kernel can use.  The GPU block at
0x2d00000 is therefore simply **off**: the GMU is running (always-on domain)
but it never raised the GPU's own GX domain.

The missing step is the ladder's last resume message.  After HFI START the
ladder sends **HFI 30**, the initial GX perf/bandwidth vote
(`{0, blocking ack, perf level 1, bandwidth level 3}`); our kit's startup ends
at START and never asks the GMU for a GX perf level, so the core stays
unpowered.  0132 sends that vote after the six startup stages (as a seventh
exchange, logged through the same transport and health checks) and gives the
domain 20 ms before the GPU window probe, which now also logs whether the
0x3d00000 mapping itself succeeded.

Image: `shim-boot-hfi12.img` sha256 `e6ab8bf7…`, kernel `60b8324e…`.
Expected: the ColdStart line now reports the vote as acknowledged
(`ack` bit for stage 7 is `AdrenoHfiStageComplete`; the vote increments
`published_commands`), and `GPU window ADT ... id=` should stop being zero.

### The GPU needs an OOB GPU_SET before any register answers (0133)

The `hfi12` log shows the HFI 30 vote *was* sent and acknowledged
(`elapsed` 1.4 ms -> 19 ms, `q1 rd/wr 0x1c`, `polls=87`), and the 0x3d00000
mapping really succeeded (`GPU window DT map 0xfffffe503f9ec000`), yet both
windows still read 0:

```
GPU window ADT 0x2d00000 id=0x0 status=0x0 | DT 0x3d00000 id=0x0 status=0x0
CP SQE alive rbbm=0x0 hw_fault=0x0 rptr=0 sqe=0x000000000 (want 0x100000)
```

So the address and the vote are both fine; the GX domain is simply never
raised.  The ladder does one more thing in its step 18 that the kit has no
analogue for - `a6xx_gmu_set_oob(GMU_OOB_GPU_SET)`: write bit 30 of
`GMU_HOST2GMU_INTR_SET`, wait for bit 31 of `GMU2HOST_INTR_INFO`, clear it -
after which the GMU enables the GX GDSC itself (the ladder then verifies
`RBBM_STATUS` and continues to the CP).

0133 adds `powerGx()`: the OOB request, a 200 ms acknowledged-bit poll, the
ack clear, and a log (`OOB GPU_SET acked/NOT ACKED (intr=0x..)` plus
`GX power up/failed`).  It runs right after a successful cold start, before the
GPU context, ring and CP steps that need live registers.

Image: `shim-boot-hfi13.img` sha256 `cfc6ada5…`, kernel `db9c28ae…`.

### GX is up, the GPU block lives at 0x3d00000, and the CP write hangs (0134/0135)

The `hfi13` boot with the OOB GPU_SET answered the addressing question for good:

```
OOB GPU_SET acked (intr=0x80000000)
GPU window ADT 0x2d00000 id=0x0 status=0x0 | DT 0x3d00000 id=0x70040000 status=0x1
CP ring attached ring 0x200000/0x8000 fence 0x300000/0x1000
CP registers written, starting the SQE
```

- the OOB request is acknowledged and the GMU raises GX;
- with GX powered the **0x3d00000** window answers (`RBBM_HW_VERSION 0x70040000`,
  `RBBM_STATUS 1`) while the ADT's 0x2d00000 keeps reading 0 - so the ADT's
  `gpu` node address is wrong and the vendor DT's 0x3d00000 is the live block;
- the probe switches to it, the ring attach succeeds against real registers,
  and the register list is written.

Then the machine hung and reset: the last printed line is
`CP registers written, starting the SQE`, i.e. the stall is the
`CP_SQE_CNTL = 1` write (or the readbacks right after it).  A hang on an MMIO
access means the CP block's slave did not respond - the SQE never fetches.
Two changes:

- 0134 brackets that write with breadcrumbs (`CP_SQE_CNTL=1 written`,
  `CP settle done`, `CP readback start`) and reads back CP_RB_BASE next to
  CP_SQE_INSTR_BASE, so the next boot names the exact instruction;
- 0135 adds the ladder's very first step-20 write, `GBIF_HALT (0x3c45) = 0`,
  which our list was missing: with the GBIF halted the SQE's first fetch
  cannot complete.

Image: `shim-boot-hfi15.img` sha256 `59b283e5…`, kernel `fe5a86f9…`.

### Starting the SQE by register write hangs the bus (0136)

Even with GBIF_HALT cleared first (`hfi15`), the boot still stops on the same
line:

```
GPU window ADT 0x2d00000 id=0x0 status=0x0 | DT 0x3d00000 id=0x70040000 status=0x1
CP ring attached ring 0x200000/0x8000 fence 0x300000/0x1000
CP registers written, starting the SQE          <- last line, then reset
```

The store to `CP_SQE_CNTL` (0x0808) never completes - a bus stall, not a
fault and not a CP timeout, and everything else in the CP block (0x0800-0x0807
through the ring attach, 0x0830/0x0831 in the register list) is read/written
normally.  That means the SQE's fetch path is not usable at the moment it is
started; the ladder's step 20 was written for this board but its completion
was never recorded, so the sequence is a hypothesis, not proof.

0136 makes that write opt-in so the boot survives and the rest of the CP path
becomes observable: it reads `CP_SQE_CNTL` first (a read is harmless), then
writes only when `sheng-gpu-sqe-start=1`.  Without the write the ME_INIT
submission and the timestamp-fence wait still run, so the next log shows
`CP_SQE_CNTL reads 0x..`, the register readbacks (`sqe=`, `rb=`), the
submission status and whether any fence ever arrives.

Image: `shim-boot-hfi16.img` sha256 `932480cf…`, kernel `aa7fa5a6…`.

### CP registers are live; only the SQE start is missing (ladder oracle)

`hfi16` (SQE start skipped) gave the full picture without hanging:

```
CP registers written
CP_SQE_CNTL reads 0x0, start=skipped (sheng-gpu-sqe-start=0)
CP SQE alive rbbm=0x1 hw_fault=0x0 rptr=0 sqe=0x00010000 (want 0x100000) rb=0x00020000
CP power-up list status=0x0 bytes=144
CP ME_INIT submit 0x0 fence=1
CP start fence 1 not observed (1, observed 0)
```

`rbbm=0x1` (idle), `hw_fault=0`, and both base registers read back what
`startCp` wrote, so the CP block is live and correctly programmed; only the
SQE never runs, so the ring is never fetched and the timestamp fence never
arrives.  Writing `CP_SQE_CNTL = 1` is what stalls the bus.

Before inventing another sequence, the ladder is run as an oracle on the same
image: `shim-boot-ladder20.img` boots with `liuqinadreno=20` and without
`sheng-gpu-bringup`, so `bsd/dev/liuqin_adreno.c` owns the GPU and executes its
steps 1-20 - the same CP register list, `CP_SQE_CNTL`, and ME_INIT - printing
every register write and readback.  Its step 20 either
prints `20 THE 3D CORE TAKES COMMANDS` (and we copy the exact difference) or
hangs at the same write (and the ladder's step 20 was never verified on this
board either).

Image: `shim-boot-ladder20.img` sha256 `53f1eca3…`, kernel `aa7fa5a6…`.

### The ladder starts the CP on this board; two writes were missing (0137)

`shim-boot-ladder20.img` (`liuqinadreno=20`, no NativeAdreno GPU work) proves the
sequence works on this device:

```
20 CP_SQE_INSTR_BASE 0 -> 0x1100000 (reads 0x1100000)
20 CP_RB_CNTL       0 -> 0x800020c (reads 0x800020c)
20 CP_RB_RPTR_ADDR  0 -> 0x1200080 (reads 0x1200080)
20 CP_BV_RB_RPTR_ADDR 0 -> 0x1200100 (reads 0x1200100)
20 CP_SQE_CNTL      0 -> 0x1 (reads 0x1)
20 alive after the SQE start: RBBM_STATUS 0x1, CP_HW_FAULT 0, rptr 0
20 done (rptr 10, RBBM_STATUS 0x1)
20 THE 3D CORE TAKES COMMANDS
```

Comparing the ladder's register file with `startCp` leaves exactly two
differences, and both explain a stalled CP that then stops acknowledging the
bus:

- **`CP_BV_RB_RPTR_ADDR` was never written** (reset 0), so when the SQE starts
  the BV ring's read pointer would be written to address 0, whose translation
  fault stalls the CP - and the next register access hangs the host bus.  The
  ladder points it at its scratch page +0x100;
- **`CP_RB_CNTL` bit 27** (the ladder's `0x800020c` vs our `0x20c`).

0137 adds both (BV rptr at the memptrs page +0x100, CP_RB_CNTL = 0x0800020c)
and turns the SQE start back on by default (`sheng-gpu-sqe-start=0` keeps it
off for readbacks only).

Image: `shim-boot-hfi17.img` sha256 `23396c41…`, kernel `4ee2b1b2…`.
Expected: `CP SQE alive ... sqe=0x000000000100000 rb=0x000000000200000`,
`CP ME_INIT submit 0x0 fence=1`, `CP started fence=1 list=144 bytes`.

### CP register order: AHB first, then the ring, then the SQE (0138)

`hfi17` still hung on `CP_SQE_CNTL` even with the BV read-pointer address and
`CP_RB_CNTL` bit 27 added, so the remaining difference to the ladder is the
*order*.  In the ladder's step 20 (which starts the core successfully on this
board) the sequence is

```
20 CP_AHB_CNTL 0 -> 0x1            (line 12 of its list)
20 CP_RB_BASE / CP_RB_CNTL / CP_RB_RPTR_ADDR / CP_BV_RB_RPTR_ADDR
20 CP_SQE_CNTL 0 -> 0x1 (reads 0x1)
```

while our ring attach programmed `CP_RB_*` *before* the register list had
turned `CP_AHB_CNTL` on, i.e. before the CP's AHB clock existed.  A wedged CP
would then never acknowledge the `CP_SQE_CNTL` store, which is exactly the
hang.

0138 splits `startCp` into the ladder's two phases and moves the ring attach
between them:

1. phase 0 - register list (AHB control, RBBM/UCHE, CP_SQE_INSTR_BASE,
   CP_BV_RB_RPTR_ADDR, CP_RB_CNTL) and the power-up list publication;
2. `ring_.attach` - CP_RB_BASE/CNTL/RPTR_ADDR/WPTR against a live CP AHB;
3. phase 1 - `CP_SQE_CNTL`, readbacks, a7xx_cp_init submission and the
   timestamp fence.

Image: `shim-boot-hfi18.img` sha256 `65a2f2a6…`, kernel `cd15ab84…`.

### The GPU streams never matched: the kit's SMRs lack the stream mask (0139)

`hfi18` fixed the order (AHB on, power-up list, ring attach, then
`CP_SQE_CNTL reads 0x0, start=yes` -> `writing CP_SQE_CNTL=1` -> hang), so the
order was not the cause.

The remaining suspect is the CP's own instruction fetch: it reads the SQE at
`CP_SQE_INSTR_BASE` through the GPU's SMMU stream.  The kit's stream attach
writes `SMR = VALID | sid` (`0x80000000 | sid`), while the ladder - and the
GMU route fix in 0102 - writes `VALID | 0x400 << 16 | sid`.  The qsmmu drops
an SMR written without the mask (0102 proved it for the GMU's stream), so the
GPU's streams 0/1 never matched their S2CR entries: the CP's fetch takes the
default path, faults, and the CP stops acknowledging register writes, which is
exactly the `CP_SQE_CNTL` stall.

0139 rewrites both GPU routes after the context comes up, in the ladder's
form, and logs the readback:

```
GPU route smr1=0x84000000 s2cr1=0x2 smr2=0x84000001 s2cr2=0x2
```

A readback of `0x8400000x` means the hardware kept it; `0x0` would mean the
write was dropped again.

Image: `shim-boot-hfi19.img` sha256 `8b9fbb3c…`, kernel `d2c2f257…`.

### SMMU preflight before the SQE start (0140)

With the GPU routes rewritten in the ladder's masked form the readback is right:

```
GPU route smr1=0x84000000 s2cr1=0x2 smr2=0x84000001 s2cr2=0x2
```

0140 adds a read-only SMMU preflight just before `CP_SQE_CNTL`, because the CP
fetches its microcode through the GPU's stream: a stale global fault or a
context bank that did not take the tables would explain a stalled fetch
without needing the register write to survive.  Geometry is derived from IDR1
as the ladder does (page size bit 31, context banks at
`(2 << ((IDR1 >> 28) & 7))` pages):

```
SMMU preflight idr1=0x.. cb2base=0x.. sgfsr=0x.. sctlr=0x.. tcr=0x.. ttbr0=0x........ fsr=0x..
```

`sgfsr != 0` or `fsr != 0` means the fetch path already faulted;
`ttbr0` should be the GPU context's table root (non-zero) and `sctlr` should
have its enable bit set.

Image: `shim-boot-hfi20.img` sha256 `9b5d7108…`, kernel `46d73522…`.

### The SQE starts; the CP does not fetch the ring yet (0141)

The SMR-mask fix unblocked the start: `CP_SQE_CNTL=1 written` no longer hangs.

```
CP registers written (AHB on, SQE base set)
CP power-up list status=0x0 bytes=144
CP ring attached ring 0x200000/0x8000 fence 0x300000/0x1000
CP_SQE_CNTL reads 0x0, start=yes
writing CP_SQE_CNTL=1
CP_SQE_CNTL=1 written
CP settle done
CP SQE alive rbbm=0x1 hw_fault=0x0 rptr=0 sqe=0x000100000
CP ME_INIT submit 0x0 fence=1
CP start fence 1 not observed (1, observed 0)
CP start stopped at 0xe00002d6 (rbbm=0x1 hw_fault=0x0)
```

So the command processor starts and accepts the `a7xx_cp_init` submission, but
within 2 s neither the timestamp fence appears nor (as 0141 now reads back) is
any state change visible.  0141 adds the readbacks that separate the two
possible causes:

- `rptr`/`wptr` right after the submission and again after the timeout: if
  `wptr` advanced but `rptr` stayed 0 the CP never fetched the ring; if both
  stay 0 the WPTR write never reached the CP;
- the first four words of the memptrs page (`mem ...`), i.e. the rptr shadow
  and the timestamp the fence packet should write.

Image: `shim-boot-hfi21.img` sha256 `0da50dcb…`, kernel `f81cbbb4…`.

### The CP consumes the ring but stops inside ME_INIT (0142)

`hfi21` readbacks settle what the timeout means:

```
CP ME_INIT submit 0x0 fence=1 rptr=0 wptr=17
CP start fence 1 not observed (1, observed 0) rptr=17 wptr=17 mem 00000000 00000000 00000000 00000000
```

`rptr` ran from 0 to 17 and matches `wptr`, so the CP fetched the submission -
the CP, the ring and the SMMU path all work.  The timestamp write that follows
`CP_ME_INIT` never executed and the memptrs page is untouched, i.e. the CP
stops *inside* the ME_INIT packet.

The ladder's a7xx_cp_init (which works on this board) passes **no power-up
register list** - its last three payload words are zero - while the kit's
`NativeAdrenoCpInit` points the CP at the list at 0x301000 and sets the
read-list flag.  0142 submits the ladder's exact ten words through
`ring_.submit` instead, and adds post-timeout readbacks of `CP_HW_FAULT`,
`CP_INTERRUPT_STATUS` and `RBBM_INT_0_STATUS`.

Image: `shim-boot-hfi22.img` sha256 `dcaac7eb…`, kernel `8945cd74…`.

### A NOP-only probe separates ME_INIT from the timestamp path (0143)

With the ladder's zero-list ME_INIT the `hfi22` log is unchanged:

```
CP SQE alive rbbm=0x1 hw_fault=0x0 rptr=0 sqe=0x000100000 rb=0x000200000
CP ME_INIT submit 0x0 fence=1 rptr=0 wptr=17
CP after timeout hw_fault=0x0 cp_int=0x0 rbbm_int0=0x0
CP start fence 1 not observed (1, observed 0) rptr=17 wptr=17 mem 0 0 0 0
```

No fault of any kind is reported, `rptr` reaches `wptr` (the CP fetches the
whole submission), and the memptrs page stays untouched.  Either the CP stops
executing *inside* ME_INIT, or the event-write packet that carries the
timestamp is never executed.

0143 adds a probe before ME_INIT: a two-word submission (one `CP_NOP`) whose
only possible effect is the ring's own timestamp event write.  Its outcome
splits the two cases:

- `CP probe fence=.. result=0 observed=..` -> the CP executes the ring and the
  event packet works, so ME_INIT is the blocker;
- `CP probe fence=.. result=<timeout> observed=0` -> the event/timestamp packet
  itself is not executed, independently of ME_INIT.

Image: `shim-boot-hfi23.img` sha256 `2b087769…`, kernel `d5b12ab0…`.

### THE CP RUNS (0143/0144)

The NOP probe answers it, and the ME_INIT that used to stall then goes through:

```
CP probe fence=1 result=0 observed=1     <- ring, event write and timestamp all work
CP started fence=2                        <- a7xx_cp_init executed and fenced
```

So the whole chain is live on the device for the first time: GMU cold start
(`ack=0xde`) -> OOB GPU_SET -> GPU SMMU context with masked SMRs -> ring
attached at 0x200000 -> `CP_SQE_CNTL` -> ME_INIT -> timestamp fence observed.
The earlier first-submission stall was the *first* submission after the SQE
start not being executed; a two-word CP_NOP submission primes it, and the
ME_INIT behind it completes.

0144 restores the submission path on top of the running CP: interrupt attach
and mask, then `NativeAdrenoSubmit` and `NativeAdrenoUserClient` bound to the
**GPU context's** VM (the GMU VM has no iovas the GPU streams can reach), and
publishes the node ready:

```
NativeAdreno: CP started, fence=2, interrupts masked=0x00000000, client=ready
```

Next: userspace submissions through that client (the Mesa/Turnip side), which is
where actual rendering starts.

Image: `shim-boot-hfi24.img` sha256 `6ae4cd32…`, kernel `50adafc7…`.

### Interrupt mask is best-effort; fences are polled (0145)

`hfi24` reached the submission tail and stopped there:

```
NativeAdreno: CP started, fence=2 ...
NativeAdreno: cp interrupt setup stopped at 0xe00002ca
```

`0xe00002ca` is `kIOReturnIOError` from `NativeAdrenoIrq::programMask`: it
writes `RBBM_INT_0_MASK` with the full enabled set and poisons when the
readback differs, i.e. this RBBM does not keep every requested bit.  The ring
retires fences by polling the memptrs timestamp (that is how `CP started
fence=2` was observed), so interrupts are an optimisation here and must not
block the client.

0145 logs the mask readback and the status register and continues:

```
NativeAdreno: CP interrupt mask 0x.. (want 0x.., status 0x..) refused 0x..; polling fences
```

(or `CP interrupt mask programmed 0x..` when the hardware keeps the bits).

Image: `shim-boot-hfi25.img` sha256 `f028ce9c…`, kernel `f545de0c…`.

### GPU READY: CP started, client published (0145/0146)

The `hfi25` boot is the first one with the whole bring-up complete:

```
CP SQE alive rbbm=0x1 hw_fault=0x0 rptr=0 sqe=0x000100000 rb=0x000200000
CP probe fence=1 result=0 observed=1
CP ME_INIT submit 0x0 fence=2 rptr=9 wptr=26
CP started fence=2
CP interrupt mask 0x3b283c2 (want 0x33b283c2, status 0x00000000) refused 0xe00002ca; polling fences
CP started, fence=2, interrupts masked=0x33b283c2, client=ready
GPU ready, publishing services
GPU started on gpu
```

The interrupt mask keeps everything except bit 28; fences are polled, so the
client comes up regardless.  From GMU cold start to a published, running GPU
node with a submit path this is the first complete pass on the device.

0146 adds the kernel-side self test that the next boot reports: a
`CP_MEM_WRITE` of `0x600dc0de` into the scratch page at 0x302040, retired by
the timestamp fence, then the scratch word is read back from the CPU:

```
CP self-test submit 0x0 fence=3 result=0 observed=3 wrote=0x600dc0de (want 0x600dc0de)
```

`wrote=` matching proves the CP executes arbitrary PM4 streams - the last
prerequisite before userspace submissions.

Image: `shim-boot-hfi26.img` sha256 `4831bf03…`, kernel `4b077679…`.

### The CP executes arbitrary command streams (0146 verified)

```
CP self-test submit 0x0 fence=3 result=0 observed=3 wrote=0x600dc0de (want 0x600dc0de)
```

`CP_MEM_WRITE` of a magic word into the scratch page was retired by the
timestamp fence and the word read back intact, so the kernel side of the GPU is
complete and proven end to end:

```
GMU cold start (ack=0xde)
  -> OOB GPU_SET (GX powered, window at 0x3d00000 answers)
  -> GPU SMMU context (bank 2, asid 2, streams 0/1, masked SMRs)
  -> ring at 0x200000, fence in the memptrs page at 0x300000
  -> CP_SQE_CNTL -> CP_NOP probe -> a7xx_cp_init -> timestamp fence
  -> CP_MEM_WRITE self test -> value verified
  -> NativeAdrenoUserClient published (client=ready)
```

What remains is the **userspace** half: the wire ABI (`NativeAdrenoABI.h`,
version 1, 16 methods) has no consumer in the tree yet - no library, no test
tool, no Turnip backend. The next milestone is therefore a minimal userspace
client that opens the GPU node, allocates a BO, maps it, submits a PM4 stream,
waits on the fence and verifies the GPU-written value - the same self test, but
through the real ABI - before any Mesa/Turnip work.

### Userspace ABI test (tools/liuqin-gpu-test.cpp)

The kernel side is complete, so the next milestone is the wire ABI itself.  The
tree had no userspace consumer at all; it now has one:

- `tools/liuqin-gpu-test.cpp` - opens `NativeAdrenoGpuService` (waiting up to
  180 s for it to publish), calls Query and GetDeviceInfo, allocates a command
  BO and a target BO, maps them into the GPU aperture and into the process,
  writes one `CP_MEM_WRITE` indirect buffer, submits it through
  `NativeAdrenoMethodSubmit`, waits on the returned fence and reads the magic
  word back.  The same check the kernel runs at bring-up, but through the ABI
  Turnip will use;
- `tools/build-gpu-test.sh` - builds it for iOS arm64 from the iOS 17.5 SDK
  against the ABI header in the patched tree (`WORK`), output
  `shim/out/liuqin-gpu-test`;
- `tools/liuqin-root-tweaks.py` now installs it as `/usr/libexec/liuqin-gpu-test`
  plus `/System/Library/LaunchDaemons/com.liuqin.gputest.plist` (RunAtLoad,
  stdout/stderr on `/dev/console`, so the result lands in the persistent kernel
  log).  It also gained a `safe_chown` so the tweaks run on a macOS mount that
  ignores ownership.

Sparse userdata image with the tool installed:
`~/liuqin-root/ipad71-root-gputest.simg` (sha256 `926e59b0…`, 1034 blocks,
4 MiB, diffed against the as-flashed image).  The tool waits up to ten minutes
for the service, because on this board the GPU is only published about five
minutes into the boot.

Expected lines in the log after flashing it and booting the hfi26 kernel:

```
gputest: abi=1 in=32 out=36 flags=0x1 max_bo=268435456
gputest: chip=0x.. gmem=0x.. va=0x..+0x..
gputest: BOs cmd=.. target=..
gputest: mapped cmd=0x.. target=0x..
gputest: submitted fence=..
gputest: WaitFence 0x0
gputest: target[0x40]=0x600dc0de want 0x600dc0de -> PASS
```

### The userspace test must not run mid-boot (opt-in switch)

The log pulled after the stuck boot turned out to be the previous one (no
`gputest:` lines, no `com.liuqin.gputest`), but it explains the symptom: on that
boot `GPU ready` came at **51 s**, i.e. the GPU service publishes while launchd
is still bringing the system up.  The test daemon submitted as soon as it
appeared, and a submission at that moment can take the display down with it -
the second boot progress bar never draws and the console text scrolls over the
logo.

The tool is now opt-in: it reads `kern.bootargs` and only runs when the boot
line carries `sheng-gpu-gputest=1`, otherwise it prints
`gputest: skipped (boot with sheng-gpu-gputest=1 to run)` and exits.  When
enabled it also waits 20 s after the service appears, so the submission cannot
race the boot.

Two artefacts:

- `~/liuqin-root/ipad71-root-optin.simg` (sha256 `999968f3…`, 272 blocks) -
  sparse diff against the image currently on the device; installs the opt-in
  tool.  A normal boot then only logs the skip line.
- `shim-boot-gputest.img` (sha256 `43a6485e…`) - the same kernel with
  `sheng-gpu-gputest=1` in the boot line, for when the test should run.

### The "no progress bar" screen is the unmuted console

Every GPU image so far dropped `liuqinmute` on purpose, so `IOLog` lines are
drawn on the panel.  iOS's second boot progress bar is drawn by userspace at
the same time, and the console text scrolls it and the Apple logo away - which
looks like "the boot stopped" while the system is in fact reaching SpringBoard
(the last normal-boot log shows `exec SpringBoard`, `exec backboardd` and the
SMP sampler running).

`shim-boot-mute.img` (sha256 `2809d735…`) is the same boot line with
`liuqinmute=1`: the screen keeps the logo and the progress bar, and the
persistent log still receives every line (the mute stops panel drawing, not the
console stream), so GPU progress and `gputest:` results are still recoverable
from the log pull.

### The desktop block is LaunchServices/`lsd`, not the display (2026-10-24)

The muted boot log shows the software display path working end to end:

```
LIUQIN FB: AppleCLCD 1800x2880 stride 7200 at 0xb8000000, default surface 1
LIUQIN FB: display on
LIUQIN FB: backboardd sel 79/74/12/6/27/17/84/50/68/5/3/55 ... -> 0x0
LIUQIN SURF: backboardd create id 2 1800x2880 fmt 0x42475241 bpe 4 bpr 7424
LIUQIN FB: swap 1 layer 0 surface 2 1800x2880 ...
LIUQIN FB: swap 3 layer 0 surface 2 ...
```

so backboardd creates IOSurfaces, CoreAnimation renders in software and IOMFB
presents three frames.  What never happens is the GUI: after those swaps the
system only execs `fairplayd.A2`/`bluetoothd` in a loop for another ten
minutes.

Symbolicating this boot's sampler stacks against the root's dyld shared cache
(`ipsw dyld a2s`, slide `0x37150000`) gives the two blockers:

```
SpringBoard t0: -[LSApplicationRecord initWithBundleIdentifier:allowPlaceholder:error:]
  <- LaunchServices::Database::Context::_get <- _LSContextInitWithOptions
  <- _LSContextInitCommon ...
backboardd  t8: mach_msg2_trap <- _dispatch_mach_send_and_wait_for_reply
  <- _xpc_connection_send_message_with_reply_sync
  <- -[NSXPCConnection _sendSelector:withProxy:arg1:]
```

SpringBoard is inside LaunchServices initialising its database and backboardd
is waiting for a synchronous XPC reply, i.e. both are waiting on
LaunchServices/`lsd` (exec'd at 83.8 s, still alive: it talks to AppleKeyStore).
No crash reports at all.

`shim-boot-sample-lsd.img` (sha256 `26ca9cad…`) boots muted with
`liuqinsample=SpringBoard,backboardd,lsd`, so the next log also carries lsd's
stacks and shows what it is waiting on.
