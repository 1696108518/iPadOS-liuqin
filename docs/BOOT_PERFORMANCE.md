# Boot performance: where the 20 minutes go

Measured on hardware 2026-10-23, with the instruments added by
`xnu/patches/0090-liuqin-boot-timing.patch`.  Every number below is an
observation from the tablet unless it says otherwise.

## The split

| phase | time |
| --- | --- |
| reset -> three-band GPU picture | ~60 s |
| three-band picture -> Apple logo | ~19 min |
| kernel: `ufs init start` -> `root device ready` | 3.4 s |
| `root device ready` -> first retry of the root mount | 0 s, then a 5 s loop |

So the kernel does almost nothing.  The time is in userspace reading the root
filesystem and in the retry loop of `bsd_init` while the root mount fails.

## The disk is the bottleneck

```
LIUQIN UFS: 1024 ops, 67 MiB, 123827 ms, … , 120925 us/op
LIUQIN UFS: lat min/avg/max 730/112050/843414 us, over 1/10/50 ms: 1022/961/442 of 1024
```

- **Per-command latency is real**: 1022 of 1024 commands took more than 1 ms,
  961 more than 10 ms, the fastest was 730 us.  (The `us/op` number alone is
  wall clock over the window and includes idle time; the histogram is the
  measurement that counts.)
- Effective throughput ~0.5 MB/s.  The boot reads 400+ MiB, so ~14 min is disk.
- Both read sizes give the same throughput (61 KiB -> 110 ms, 408 KiB -> 947 ms),
  i.e. roughly 8 ms per 4 KiB block: a link problem, not a per-command one.

## The link is in PWM

```
LIUQIN UFS: gear before maxhs 4(0) maxpwm 1(0) tx 4(0) rx 4(0) pm 05(0) lanes 1(0)/1(0)
LIUQIN UFS: pm probe same=0 fast=10 fastauto=10
```

- The device reports HS-G4 (`maxhs 4`) and the TX/RX gear attributes can be
  written (they then read back 4).
- `PA_PWRMODE` is **SLOWAUTO (5)** and cannot be changed: writing the current
  value succeeds, writing FAST (1) or FASTAUTO (4) is refused (UIC result 10,
  later 2).  Writing the gear alone changes nothing; the power-mode change is
  what re-trains the link.
- 0.5 MB/s is the right order of magnitude for PWM-G1, so the link never left
  the slow mode our PHY bring-up leaves it in.

## What was ruled out on hardware

| hypothesis | test | result |
| --- | --- | --- |
| console text drawing | `liuqinmute=1` (moved into the platform expert, patch 0090) | no change; the marks themselves cost tens of ms per line, not seconds |
| exec path is slow | `xpcproxy` wrote 18 exec marks in 22 ms | exec is fast; the gaps are the previous process's work |
| read-ahead helps | `liuqinra=1` | same throughput (0.47 MB/s), 8x worse latency |
| inherit ABL's link | `ufsskipinit=1` | controller enabled (HCE, DP, UTRLRDY), device asleep, SCSI fails -> panic |
| soft wake keeps HS | `ufssoftwake=1` (skip PHY reset + DME_RESET) | PHY does not come up (`phy=5`), UIC commands fail |

## What is left to do

The QMP UFS PHY is woken with three register writes (power up, start, poll
ready).  That is enough for PWM and nothing more: the M-PHY has no HS
configuration, so the PA layer refuses the power-mode change.  The fix is to
port the SM8450 QMP UFS PHY init from Linux
(`drivers/phy/qualcomm/phy-qcom-qmp-ufs.c`, the `sm8450` SerDes/TX/RX/PCS tables
and the reference-clock/PLL setup) into `ufs_phy_wake()`.

Until that lands, the tablet boots and runs, at ~0.5 MB/s.  (The tables
landed in patch 0091 and the clock layer around them in patch 0092 - see the
last section.)

## The PHY port attempt (patch 0091)

`ufs_phy_wake()` now carries the register tables from Linux's PHY driver,
behind boot-arg `ufsphyhs=1`; the default path keeps the minimal power-on.

- **v5 layout (97 register writes, sm8450 cfg)**: wrong block for this board.
  Every UIC read came back as 8 with result 16 and the first SCSI command timed
  out after 5 s - the link was destroyed, and the tablet did not boot.
- **v6 layout (sm8475 cfg: PCS 0x400, ready 0x1a8, tx/rx 0x1000/0x1200, plus
  the G4 overlay)**: better - `phy post pwrdn 0x0 ready 0x43` shows the PCS
  reaching its ready state - but every UIC command is still refused
  (`uic reset 60 enable 16 poweron 16 link 16`), so no gear can be set and the
  link never comes up in HS.

So the tables alone are not enough.  What Linux does around them and this
driver does not: the GCC-side `ufsphy` reset and the reference/interface clock
*rates* (the driver only sets branch enable bits in the CBCRs), the PHY
`com_init` sequence, and the regulator/voltage votes.  That is the next piece,
and it is why the HS tables stayed opt-in: the default image still boots.

## Using the instruments

Patch 0090 prints `LIUQIN BOOT: <phase> at <s> (+<delta> ms)` at the phases of
`liuqin_ufs_init()`/`rootdev`, a per-command latency histogram every 1024 UFS
commands, a forced `cannot mount root, errno = N`, and `exec <name>` timestamps.
Those lines are drawn even with `liuqinmute=1`, which is what makes a muted
boot diagnosable; the rest of the console stays dark.

Diagnostic boot arguments (all off by default):

| argument | effect |
| --- | --- |
| `liuqinboot=0` | hide the boot marks |
| `ufsmibscan=1` | DME_GET the M-PHY MIB attributes 0x00-0x3f and print them |
| `ufsgear=1` | read/set the PA gear and power-mode attributes (probe) |
| `ufsskipinit=1` | use the controller as the bootloader left it |
| `ufssoftwake=1` | wake without PHY reset and DME_RESET |

## Third attempt: Linux power-on order (still no HS)

With `ufsphyhs=1` the HS path then followed `qmp_ufs_com_init()` /
`qmp_ufs_power_on()` exactly: hold `SW_PWRDN` while the tables are written,
assert and release `SW_RESET`, start the SerDes, release the power-down, poll
ready.  Result on hardware:

```
LIUQIN UFS: phy post pwrdn 0x0 ready 0x4
LIUQIN UFS: uic reset 60 enable 16 poweron 16 link 16 (softwake 0)
```

The PCS ready bit does not even come up now (0x4, bit 0 clear; the minimal
power-on at least reached 0x43), and every UIC command is still refused with
the same result codes.  The register sequence is no longer the variable - what
this driver lacks is the layer around the PHY: the GCC-side `ufsphy` reset, the
reference/interface clock rates (CBCR branch enables only, no RCG rates) and
the PHY/controller regulator votes.  That is where the next session started:
`drivers/ufs/host/ufs-qcom.c` (`ufs_qcom_phy_init_clks`) plus the SM8450 GCC
UFS clock definitions.  Patch 0092 implements that layer; the section at the
end of this file says what it does and what to look for on the next boot.

## The clock layer (patch 0092, host-side build only)

**Booted on hardware 2026-10-23** (`shim-boot-hs.img`, the 0092 kernel
`2d33f4bc…`; the rebuild below is HS by default). The
UFS bottleneck is gone: the first 1024 commands moved 74 MiB in 4.66 s
(~16 MiB/s, versus 0.5 MB/s before) and the kernel reached `exec SpringBoard`
at 49.6 s. The system does not reach the UI yet, but that is now a userspace
problem (see "After the boot" below), not a disk one.

| measurement | before (PWM) | after (0092) |
| --- | --- | --- |
| command latency min/avg/max | 730/112050/843414 us | 15/176/2898 us |
| commands over 1/10/50 ms | 1022/961/442 of 1024 | 4/0/0 of 1024 |
| throughput, first 1024 commands | ~0.5 MB/s | 74 MiB / 4.663 s = **~16 MiB/s** |
| `exec launchd` | ~19 min after the picture | 47.14 s |
| `exec SpringBoard` | never | 49.65 s |

### Re-reading the third attempt's return values

The console mixed three different number spaces.  With Darwin errno and the
UFS UIC result codes separated, the last run says something else:

- `uic reset 60` is `ETIMEDOUT` (errno 60): DME_RESET was accepted by the
  host and never completed.
- `enable 16 poweron 16 link 16` are `EBUSY` (errno 16) from `ufs_uic()`'s
  "UIC not ready" early return (HCS bit 3 clear).  The host lost the UniPro
  clock while the command was in flight - consistent with the PHY being
  powered down by the driver's own `pcs[0x004] = 0`.
- `pm probe fast=10` *is* a UIC result: 10 = invalid value.  That probe wrote
  `1` to `PA_PWRMODE`, but `PA_PWRMODE` packs two nibbles (RX mode 7..4, TX
  mode 3..0), so FAST/FAST is `0x11`.  The device was not refusing HS; the
  argument was malformed.  (`READ_ATTR bCurrentPowerMode` also returns 0x11,
  but for SLOW - the two encodings have nothing to do with each other.)

### What the patch adds

- **GCC UFS_PHY power domain and reset**: `ufs_phy_gdsc` (0x87004, collapse
  bit 0, power-on status bit 31) is brought up, and the block reset (0x87000)
  is deasserted.  The HS path pulses it (assert 200 us, deassert, 1 ms)
  exactly like `ufs_qcom_host_reset()`; that also resets the host controller,
  so the driver re-enables HCE and re-programs the request list afterwards.
- **GCC branches**: axi 0x87018, ahb 0x87020, unipro 0x87064, ice 0x8706c,
  aux 0x870a4, aggre 0x870d4, tx/rx symbol 0x87024/0x87028/0x870c0, and the
  PHY's 19.2 MHz reference buffer `gcc_ufs_0_clkref_en` (0x9c000, `qref`),
  which the driver had never enabled.  Each enable is followed by the
  branch's halt bit and a readback is printed.
- **RCG rates**: axi/unipro/ice are programmed to 300 MHz (GPLL0 main
  600 MHz / 2: divider field 3, source 1) and aux to 19.2 MHz (XO, field 1,
  source 0).  The divider field is `2*div-1`.  The boot log decodes the rate
  back out of the RCG registers, so a wrong source/divider is visible instead
  of assumed.
- **Host clock registers**: `SYS1CLK_1US` (0xC0) is mandatory from UFS HC
  v4.0.0 and now gets core_clk in MHz; CFG1 gets `QUNIPRO_SEL`; the device
  reference clock (CFG1 bit 26) is enabled before HS; and
  `DME_VS_CORE_CLK_CTRL` (0xD002) / `PA_VS_CORE_CLK_40NS_CYCLES` (0x9007)
  get the 300 MHz UniPro values (1 us = 300 cycles, 40 ns = 12 cycles).
- **PHY power-up order**: now `qmp_ufs_com_init()` / `qmp_ufs_phy_calibrate()`:
  `SW_PWRUP` first (the bit Linux renamed from `SW_PWRDN` in 2024 - it is
  active high), then the tables, `SW_RESET` only cleared (never asserted;
  asserting it after the tables is what left the PCS at ready 0x4), then
  `SERDES_START` alone.  The default PWM path is untouched.
- **PA power-mode change**: `ufs_set_hs_gear()` follows
  `ufshcd_change_power_mode()`: RX gear/lanes/termination, TX
  gear/lanes/termination, `PA_HSSERIES` (rate A, then B), `PA_INITIAL_ADAPT`,
  and finally `PA_PWRMODE = 0x11`.  It runs after `fDeviceInit` clears, and
  automatically when `ufsphyhs=1` (or via `ufsgear=1` on the PWM path).

### Boot arguments

| argument | effect |
| --- | --- |
| *(default)* | full HS path: clock rates, qref/symbol clocks, block-reset pulse, Linux PHY order, dev ref clock, DME clock attributes, gear change |
| `ufsphypwm=1` | fallback: the old three-write PHY power-on, no block reset, no gear change (keeps ABL's PWM link) |
| `ufsgear=1` | on the PWM path, only read the link state before `fDeviceInit` and attempt the corrected gear change after it |

HS is the default now that it has booted to SpringBoard.  The PWM path is kept
as a one-flag fallback because it is the one that booted for weeks.

### What it printed on hardware

```
LIUQIN UFS: hw hce 0x1 hcs 0x10f is 0x400 cap 0x1587031f
LIUQIN UFS: gcc gdsc 0xf822f000 bcr 0x0
LIUQIN UFS: gcc rates axi 300.000 MHz (src 1 div 3) unipro 300.000 MHz ice 300.000 MHz aux 19.200 MHz
LIUQIN UFS: gcc cbcr axi 0x221 ahb 0x1 unipro 0x221 ice 0x221 aux 0x1 aggre 0x1 qref 0x1 sym tx 0x1 rx0 0x1 rx1 0x1
LIUQIN UFS: gcc bcr before 00000000 (pulse 1)
LIUQIN UFS: gcc bcr after 00000000
LIUQIN UFS: hc ver 0x40050000 cfg1 0x1c00052c sys1clk 300 MHz
LIUQIN UFS: device ref clock on (cfg1 0x1c00052c)
LIUQIN UFS: phy pre start 0x0 pwrdn 0x0 ready 0x4
LIUQIN UFS: phy post start 0x1 pwrdn 0x1 ready 0xd (hs 1)
LIUQIN UFS: host restart hce 0x1 hcs 0x8 failed
LIUQIN UFS: dme core clk ctrl 0x12c0000 -> 0
LIUQIN UFS: dme 40ns cycles 0xc -> 0
LIUQIN UFS: uic reset 0 enable 0 poweron 1 link 0 (softwake 0)
LIUQIN UFS: power mode 0x11 (query 0)
...
LIUQIN UFS: hs pre maxhs 4(0) tx 1(0) rx 1(0) pm 0x55(0) lanes 1(0)/1(0)
LIUQIN UFS: hs set gear 4 rx=0 lanes_rx=0 tx=0 lanes_tx=0 adapt=0 pwr 0x11 -> 0 series 1 conn 0x1
LIUQIN UFS: hs after tx 1 rx 1 pm 0x55 lanes 1/1
LIUQIN UFS: 1024 ops, 74 MiB, 4663 ms, 16 MiB/s, 4554 us/op
LIUQIN UFS: lat min/avg/max 15/176/2898 us, over 1/10/50 ms: 4/0/0 of 1024
```

Three things in that output were wrong or misleading and were fixed after the
run (the numbers above prove the link is HS anyway):

- **`host restart ... failed`** - the check wanted HCS 0xf, but bits 0/1 (DP,
  UTRLRDY) only appear after link startup.  HCS 0x8 is UIC ready, which is all
  that matters there; the restart worked.
- **`uic ... poweron 1`** - DME_POWERON is answered with "invalid opcode" (1)
  on this controller.  Linux never sends it; it is gone now.
- **`hs after ... pm 0x55 tx 1 rx 1`** - the readback right after the mode
  change is stale (it still shows ABL's SLOWAUTO settings).  The 16 MiB/s and
  the 176 us average latency are the real evidence that the link is in HS;
  `PA_PWRMODE` is not a reliable readback here.
- The throughput line said `KiB/s` while printing MiB/s; the label is fixed.

### After the boot: the next wall is userspace

The 0092 boot reaches userspace: `exec launchd` at 47.14 s, `exec SpringBoard`
at 49.65 s, `exec backboardd` at 49.85 s.  It then stays there:

- SpringBoard (pid 26) is alive but stuck; the stackshot dumps (`SMP
  SpringBoard[...]`) repeat at the same pc, which is the known missing
  GPU/Metal path, not a UFS wait.
- `CommCenter` crashes 428 times (sig 11, `reason 0xb`), `fairplayd.A2` and
  `bluetoothd` are re-exec'd every ~10 s.  No `LIUQIN exec failed` line
  appears at all, so the exec path itself is fine; the daemons start and then
  die on missing hardware (baseband, FairPlay, Bluetooth).
- The 4 MiB console ring held >21 minutes of that loop.  Later throughput
  windows read low (1-3 MiB/s wall) because the window includes idle time;
  the per-command latency stays at a few microseconds, so the disk is idle,
  not slow.

### Images

Rebuilt after the first on-device run (HS default, log fixes above):

| artifact | bytes | sha256 |
| --- | --- | --- |
| `kernel.development.liuqin` | 16,833,296 | `b0a98ee5e5a927723eac10676db63c260137285a4b85d4d9aa99d338a4e3a757` |
| `shim/out/payload.bin` (HS default) | 25,234,128 | `03bc8c112d7973fbfbf69d3690f8bd40998802699ba7edb93be94619ae562939` |
| `shim/out/payload-pwm.bin` (`ufsphypwm=1`) | 25,234,128 | `35e2b24b606ca26f2b5b9c0ed3078b5e2913c0453665db3578a4a7bc34bb0039` |
| `shim/out/shim-boot.img` | 31,809,536 | `788e43e4f2c4859cf2365c2de0b376b9daac0b2fb73e83d92de6e90ff36c3649` |
| `shim/out/shim-boot-pwm.img` | 31,809,536 | `b265f8fe6e26643f8b2ba2acfd833dc2a6aa5b708508adf4f3b0682da1aef5b2` |

The image that produced the measurements above was `shim-boot-hs.img`
(kernel `2d33f4bc…`, `6ce15fed…`); it has been replaced by the HS-default
build so the extra boot argument is no longer needed.
