#!/usr/bin/env python3
"""Generate the Sheng GMU tables for liuqin (Adreno 730 / SM8475).

The NativeAdreno service starts the GMU through the Sheng GMU framework, but
that framework needs three generated headers:

    ShengGmuSource.h          register offsets, RSC/PDC setup, firmware hash
    ShengGmuVotesSource.h     GPU/GMU OPPs, bandwidths, BCM policy
    ShengGmuCommandDbSource.h command-db reservation

The A740 generator in UniXNU-sheng parses pinned Linux sources and a board
DTB.  This one takes the A730 values from this port instead:

  * register offsets and the 19 RSC/PDC writes come from the bring-up ladder
    (bsd/dev/liuqin_adreno.c), which was ported from Linux and has run on the
    tablet through step 22;
  * GPU OPPs, ARC levels, ACD levels and the DDR bandwidth table come from the
    vendor DTB's qcom,gpu-pwrlevels and qcom,bus-table-ddr;
  * GMU OPPs come from the ladder's gmu_khz/gmu_level arrays;
  * the GMU firmware hash comes from gmu_gen70000.bin (the fwarea image).

Offset convention: the port's NativeAdrenoGmuPlatform::bind() does not compare
physical windows (upstream ShengGmuBindMmio does), so the core window is
mapped by VA and SHENG_GMU_CORE_GPU_DELTA can be 0: every REG_* below is a
*dword* offset inside window 0 (GMU, ADT base 0x2d68000).  The ladder's
constants are dwords relative to the GPU base, so REG = ladder_dword -
(0x2d68000 - 0x2d00000) / 4 = ladder_dword - 0x1a000.

Usage:
    tools/prepare-liuqin-gmu.py [--dtb PATH] [--fw-dir PATH] [--out DIR]
                                [--install DIR] [--check-ladder PATH]

The headers are written to --out (default shim/out/gmu) and, with --install,
copied into the kernel tree (e.g. /tmp/liuqin-build2/xnu/iokit/NativeAdreno).
"""

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# -- board constants -------------------------------------------------------
# ADT-relative bases (mkadt.py); the service logs the same values.
WINDOWS = [
    ("gmu",    0x2d68000, 0x37000),
    ("rscc",   0x2d50000, 0x10000),
    ("gmu_pdc", 0xa290000, 0x10000),
    ("gpu",    0x2d00000, 0x40000),
    ("cx_misc", 0x2d9e000, 0x1000),
]
GPU_BASE = 0x2d00000
GMU_BASE = 0x2d68000
CORE_DELTA_DWORDS = (GMU_BASE - GPU_BASE) // 4          # 0x1a000

CHIP_ID = 0x07030000          # Adreno 730, ADRENO_CHIP_IDS
FW_CHIP_ID = 0x07030100       # the value the ladder writes to GMU_GENERAL_10
GMU_FIRMWARE = "gmu_gen70000.bin"
COMMAND_DB_BASE = 0x80860000  # ladder CMD_DB_PHYS
COMMAND_DB_BYTES = 0x20000

# A6XX GMU register numbers (dwords, A6XX XML) minus the ladder's known
# constants.  The ladder's values are the proven ones; XML names are kept
# beside them for provenance.
GMU_REGISTERS = [
    # name, ladder dword constant, source
    ("GMU_CM3_ITCM_START", 0x1b400, "ladder (a6xx_gmu.xml 0xc00 + GMU base)"),
    ("GMU_CM3_DTCM_START", 0x1c400, "ladder (a6xx_gmu.xml 0x1c00)"),
    ("GMU_ICACHE_CONFIG", 0x1f400, "ladder (a6xx_gmu.xml 0x4c00)"),
    ("GMU_DCACHE_CONFIG", 0x1f401, "ladder (a6xx_gmu.xml 0x4c01)"),
    ("GMU_SYS_BUS_CONFIG", 0x1f40f, "ladder (a6xx_gmu.xml 0x4c0f)"),
    ("GMU_CM3_SYSRESET", 0x1f800, "ladder (a6xx_gmu.xml 0x5000)"),
    ("GMU_CM3_BOOT_CONFIG", 0x1f801, "ladder (a6xx_gmu.xml 0x5001)"),
    ("GMU_CM3_FW_INIT_RESULT", 0x1f81c, "ladder (a6xx_gmu.xml 0x501c)"),
    ("GMU_CM3_CFG", 0x1f82d, "ladder (a6xx_gmu.xml 0x502d)"),
    ("GMU_HFI_CTRL_STATUS", 0x1f980, "ladder (a6xx_gmu.xml 0x5180)"),
    ("GMU_HFI_QTBL_INFO", 0x1f984, "ladder (a6xx_gmu.xml 0x5184)"),
    ("GMU_HFI_QTBL_ADDR", 0x1f985, "ladder (a6xx_gmu.xml 0x5185)"),
    ("GMU_HFI_CTRL_INIT", 0x1f986, "ladder (a6xx_gmu.xml 0x5186)"),
    ("GMU_GMU2HOST_INTR_INFO", 0x1f992, "ladder (a6xx_gmu.xml 0x5192)"),
    ("GMU_HOST2GMU_INTR_SET", 0x1f994, "ladder (a6xx_gmu.xml 0x5194)"),
    ("GMU_GENERAL_8", 0x1f9cd, "ladder (a6xx_gmu.xml 0x51cd)"),
    ("GMU_GENERAL_9", 0x1f9ce, "ladder (a6xx_gmu.xml 0x51ce)"),
    ("GMU_GENERAL_10", 0x1f9cf, "ladder (a6xx_gmu.xml 0x51cf)"),
    ("GMU_AO_HOST_INTERRUPT_STATUS", 0x23b05, "a6xx_gmu.xml 0x9305 + GMU base"),
    ("GMU_AHB_FENCE_RANGE_0", 0x23b11, "ladder (a6xx_gmu.xml 0x9311)"),
    ("GPU_GMU_CX_GMU_CX_FAL_INTF", 0x1f8f0, "ladder (a6xx_gmu.xml 0x50f0)"),
    ("GPU_GMU_CX_GMU_CX_FALNEXT_INTF", 0x1f8f1, "ladder (a6xx_gmu.xml 0x50f1)"),
    # These three are addressed in dwords inside their own windows.
    ("REG_CX_MISC_TCM_RET_CNTL", 0x39, "ladder CX_MISC_TCM_RET_CNTL (byte 0xe4)"),
    ("REG_RSCC_SEQ_BUSY_DRV0", 0x101, "ladder RSCC_SEQ_BUSY_DRV0 (byte 0x404)"),
    ("REG_GBIF_QSB_SIDE0", 0x3c03, "ladder GPU_GBIF_QSB_SIDE0 (dword, GPU space)"),
]
# The last three stay raw dwords; everything before them is window-0 relative.
RAW_DWORD_START = 22

# The 19 writes of a6xx_gmu_rpmh_init() for this board, from ladder step 15:
# (window, byte offset, value).
RSC_SETUP = [
    (1, 0x004 * 4, 1 << 24),          # RSCC_STATUS0: SDE clock gating off
    (1, 0x00b * 4, 1),                # PDC slave id
    (1, 0x00e * 4, 0),                # hidden TCS data 0
    (1, 0x00d * 4, 0),                # hidden TCS addr 0
    (1, (0x00e + 2) * 4, 0),          # hidden TCS data 1
    (1, (0x00d + 2) * 4, 0),          # hidden TCS addr 1
    (1, (0x00e + 4) * 4, 0x80000000), # hidden TCS data 2
    (1, (0x00d + 4) * 4, 0),          # hidden TCS addr 2
    (1, 0x100 * 4, 0),                # RSCC_OVERRIDE_START
    (1, 0x008 * 4, 0x4520),           # PDC_SEQ_START
    (1, 0x009 * 4, 0x4510),           # PDC_MATCH_LO
    (1, 0x00a * 4, 0x4514),           # PDC_MATCH_HI
    (1, (0x180 + 0) * 4, 0xeaaae5a0), # SEQ_MEM 0
    (1, (0x180 + 1) * 4, 0xe1a1ebab), # SEQ_MEM 1
    (1, (0x180 + 2) * 4, 0xa2e0a581), # SEQ_MEM 2
    (1, (0x180 + 3) * 4, 0xecac82e2), # SEQ_MEM 3
    (1, (0x180 + 4) * 4, 0x0020edad), # SEQ_MEM 4
    (2, 0x1148 * 4, 0),               # PDC_GPU_SEQ_START_ADDR
    (2, 0x1140 * 4, 0x80000001),      # PDC_GPU_ENABLE_PDC
]

# GMU OPPs from ladder gmu_khz[]/gmu_level[].
GMU_OPPS = [(0, 0), (200000, 64), (500000, 128)]

# A730 BCM policy.  SH0/MC0 buswidths and the ACV fixed perfmode are NOT in
# mainline (the catalog lives in the downstream msm tree); these are the
# values the first hardware run will have to confirm.  --bcm overrides them.
BCM = [("SH0", 16, 0, 0, 0), ("MC0", 4, 0, 0, 0), ("ACV", 0, 1, 1 << 5, 12000)]

AO_FATAL_MASK = (1 << 0) | (1 << 3) | (1 << 5)   # WDOG_BITE|FENCE_ERR|HOST_AHB_BUS_ERROR
CM3_FATAL_MASK = 1 << 23                          # CM3_FAULT


def be32(data, offset=0):
    return int.from_bytes(data[offset:offset + 4], "big")


def load_dtb(path):
    sys.path.insert(0, str(REPO / "tools"))
    import fdtread
    return fdtread.parse(Path(path).read_bytes())


def find(node, name):
    if name in (node.name or ""):
        return node
    for child in node.children:
        found = find(child, name)
        if found:
            return found
    return None


def read_opps(root, ddr_table, warnings):
    levels = find(root, "qcom,gpu-pwrlevels")
    if not levels:
        raise SystemExit("no qcom,gpu-pwrlevels in the DTB")
    opps = []
    for child in levels.children:
        props = child.props
        hz = be32(props["qcom,gpu-freq"])
        level = be32(props["qcom,level"])
        acd = be32(props["qcom,acd-level"]) if "qcom,acd-level" in props else 0
        index = be32(props["qcom,bus-freq"]) if "qcom,bus-freq" in props else -1
        kbps = 0
        if 0 <= index < len(ddr_table):
            kbps = ddr_table[index] // 1024
        else:
            warnings.append(f"pwrlevel {child.name}: bus-freq {index} outside the DDR table")
        opps.append((hz, level, kbps, 1 if acd else 0, acd))
    opps.sort()
    if not opps:
        raise SystemExit("no GPU pwrlevels parsed")
    return [(0, 0, 0, 0, 0)] + opps


def emit_gmu_source(fw_bytes, fw_sha):
    lines = [
        "/* Generated by tools/prepare-liuqin-gmu.py from the A730 ladder",
        " * constants and gmu_gen70000.bin.  Do not edit. */",
        "#ifndef SHENG_GMU_SOURCE_H",
        "#define SHENG_GMU_SOURCE_H",
        "#include <stdint.h>",
        "#include <stddef.h>",
        "",
    ]
    for name, dword, source in GMU_REGISTERS:
        if name.startswith("REG_"):
            lines.append(f"#define {name} UINT32_C(0x{dword:x})   /* {source} */")
        else:
            value = dword - CORE_DELTA_DWORDS
            lines.append(f"#define REG_{name} UINT32_C(0x{value:x})   /* {source} */")
    lines += [
        "",
        f"#define SHENG_GMU_CHIP_ID UINT32_C(0x{CHIP_ID:x})",
        f"#define SHENG_GMU_FW_CHIP_ID UINT32_C(0x{FW_CHIP_ID:x})",
        "#define SHENG_GMU_CORE_GPU_DELTA 0u",
        f"#define SHENG_GMU_FIRMWARE_BYTES {fw_bytes}u",
        f"#define SHENG_GMU_AO_FATAL_MASK UINT32_C(0x{AO_FATAL_MASK:x})",
        f"#define SHENG_GMU_CM3_FATAL_MASK UINT32_C(0x{CM3_FATAL_MASK:x})",
        "static const uint8_t sheng_gmu_firmware_sha256[32]={"
        + ",".join(f"0x{b:02x}" for b in fw_sha) + "};",
        "static const uint64_t sheng_gmu_window_physical[5]={"
        + ",".join(f"UINT64_C(0x{base:x})" for _, base, _ in WINDOWS) + "};",
        "static const size_t sheng_gmu_window_bytes[5]={"
        + ",".join(f"0x{size:x}" for _, _, size in WINDOWS) + "};",
        "typedef struct {unsigned window;size_t byte_offset;uint32_t value;} ShengGmuSetupWrite;",
        f"#define SHENG_GMU_RSC_SETUP_COUNT {len(RSC_SETUP)}u",
        f"static const ShengGmuSetupWrite sheng_gmu_rsc_setup[{len(RSC_SETUP)}]={{",
    ]
    for window, offset, value in RSC_SETUP:
        lines.append(f"    {{{window},0x{offset:x},UINT32_C(0x{value:x})}},")
    lines += ["};", "", "#endif", ""]
    return "\n".join(lines)


def emit_votes_source(gpu_opps, gmu_opps, bcm):
    # AdrenoHfiBuildBandwidth() requires kbps[0] == 0 and strictly ascending.
    bandwidths = sorted({opp[2] for opp in gpu_opps})
    lines = [
        "/* Generated by tools/prepare-liuqin-gmu.py from the vendor DTB",
        " * (qcom,gpu-pwrlevels + qcom,bus-table-ddr) and the ladder's GMU OPPs.",
        " * The BCM policy is the one A730 value that is not in mainline;",
        " * confirm or override it before trusting bandwidth votes. */",
        "#ifndef SHENG_GMU_VOTES_SOURCE_H",
        "#define SHENG_GMU_VOTES_SOURCE_H",
        "#include <stdint.h>",
        "",
        f"#define SHENG_GMU_VOTES_CHIP_ID UINT32_C(0x{CHIP_ID:x})",
        "typedef struct {uint64_t hz;uint32_t level,kbps,acd_present,acd;} ShengGmuOpp;",
        f"#define SHENG_GPU_OPPS {len(gpu_opps)}u",
        "static const ShengGmuOpp sheng_gpu_opps[]={",
    ]
    for hz, level, kbps, acd_present, acd in gpu_opps:
        lines.append(f"    {{{hz}ull,{level}u,{kbps}u,{acd_present}u,{acd}u}},")
    lines += [
        "};",
        f"#define SHENG_GMU_OPPS {len(gmu_opps)}u",
        "static const ShengGmuOpp sheng_gmu_opps[]={",
    ]
    for khz, level in gmu_opps:
        lines.append(f"    {{{khz * 1000}ull,{level}u,0u,0u,0u}},")
    lines += [
        "};",
        f"#define SHENG_GPU_BANDWIDTHS {len(bandwidths)}u",
        "static const uint32_t sheng_gpu_bandwidths[]={"
        + ",".join(str(value) for value in bandwidths) + "};",
        "static const AdrenoHfiBcm sheng_bcm_policy[3]={",
    ]
    for name, buswidth, fixed, perfmode, perfmode_kbps in bcm:
        lines.append(f"    /* {name} */ {{.buswidth={buswidth},.fixed={fixed},"
                     f".perfmode={perfmode},.perfmode_kbps={perfmode_kbps}}},")
    lines += ["};", "", "#endif", ""]
    return "\n".join(lines)


def emit_command_db():
    return "\n".join([
        "/* Generated by tools/prepare-liuqin-gmu.py: the ladder's command-db",
        " * reservation (CMD_DB_PHYS/CMD_DB_SIZE).  Not observed memory. */",
        "#ifndef SHENG_GMU_COMMAND_DB_SOURCE_H",
        "#define SHENG_GMU_COMMAND_DB_SOURCE_H",
        f"#define SHENG_GMU_COMMAND_DB_BASE UINT64_C(0x{COMMAND_DB_BASE:x})",
        f"#define SHENG_GMU_COMMAND_DB_BYTES {COMMAND_DB_BYTES}u",
        "#endif",
        "",
    ])


def check_ladder(path, fw_bytes):
    text = Path(path).read_text()
    problems = []
    for name, dword, _ in GMU_REGISTERS:
        if name.startswith("REG_"):
            continue
        match = None
        import re
        for pattern in (rf"#define\s+{name}\s+(0x[0-9a-fA-F]+)",
                        rf"#define\s+{name.replace('GMU_', 'GMU_', 1)}\s+\(4 \* (0x[0-9a-fA-F]+)\)"):
            match = re.search(pattern, text)
            if match:
                break
        if match:
            found = int(match.group(1), 16)
            if found != dword:
                problems.append(f"{name}: ladder {found:#x} != table {dword:#x}")
    if "GMU_RSC_SETUP" in text:
        problems.append("ladder has its own RSC setup table; keep this one in sync")
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dtb", default=str(REPO / "dtb/boot_unpacked/dtb"))
    parser.add_argument("--fw-dir", default=str(Path.home() / "liuqin-root/fw"))
    parser.add_argument("--out", default=str(REPO / "shim/out/gmu"))
    parser.add_argument("--install", default=None)
    parser.add_argument("--check-ladder", default=str(REPO / "xnu/patches/reference/liuqin_adreno.c"))
    parser.add_argument("--bcm", default=None, help="SH0:MC0:ACV buswidths")
    args = parser.parse_args()

    fw_path = Path(args.fw_dir) / GMU_FIRMWARE
    fw_bytes = fw_path.read_bytes()
    fw_sha = hashlib.sha256(fw_bytes).digest()

    warnings = []
    root = load_dtb(args.dtb)
    gpu = find(root, "kgsl-3d0")
    ddr = [be32(gpu.props["qcom,bus-table-ddr"], i * 4)
           for i in range(len(gpu.props["qcom,bus-table-ddr"]) // 4)]
    gpu_opps = read_opps(root, ddr, warnings)

    bcm = BCM
    if args.bcm:
        widths = [int(part) for part in args.bcm.split(":")]
        if len(widths) == 3:
            bcm = [("SH0", widths[0], 0, 0, 0), ("MC0", widths[1], 0, 0, 0),
                   ("ACV", 0, 1, 1 << 5, widths[2])]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    headers = {
        "ShengGmuSource.h": emit_gmu_source(len(fw_bytes), fw_sha),
        "ShengGmuVotesSource.h": emit_votes_source(gpu_opps, GMU_OPPS, bcm),
        "ShengGmuCommandDbSource.h": emit_command_db(),
    }
    for name, text in headers.items():
        (out / name).write_text(text)
        print(f"wrote {out / name}")

    if args.install:
        target = Path(args.install)
        for name, text in headers.items():
            (target / name).write_text(text)
            print(f"installed {target / name}")

    print(f"firmware {fw_path}: {len(fw_bytes)} bytes sha256 {fw_sha.hex()}")
    print(f"GPU OPPs {len(gpu_opps)}: " + ", ".join(
        f"{hz // 1000000}MHz/L{level}/B{kbps}/A{acd:#x}" for hz, level, kbps, _, acd in gpu_opps))
    print(f"GMU OPPs {len(GMU_OPPS)}: " + ", ".join(f"{khz}kHz/L{level}" for khz, level in GMU_OPPS))
    print("BCM: " + ", ".join(f"{name} {buswidth}" for name, buswidth, _, _, _ in bcm))
    for warning in warnings:
        print("warning:", warning)

    ladder = Path(args.check_ladder)
    if ladder.exists():
        problems = check_ladder(ladder, len(fw_bytes))
        if problems:
            print("ladder check:")
            for problem in problems:
                print("  -", problem)
        else:
            print("ladder check: constants match")


if __name__ == "__main__":
    main()
