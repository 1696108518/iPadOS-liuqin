# iPadOS-liuqin 工程记录

最后更新：2026-10-24。本文只写**观察到的事实**和**明确的推断**，两者分开标注。
每次里程碑后更新“交接”一节。GPU 部分的最新事实在 `docs/GPU_MILESTONE.md`。

## 1. 目标与当前状态

目标：在 Xiaomi Pad 6 Pro（`liuqin`，SM8475）上启动 XNU（xnu-8796，iOS 16 世代），
运行未修改的 iPadOS 16 用户态。派生自 ios-sweet（SM7150），参考
xiaomipad-6pro-mainline（liuqin 设备树）与 iPadOS-nabu（iPad 身份/触摸）。

状态一句话（2026-10-24）：**已上真机，启动到 iPadOS 锁屏界面**。内核 ~50 秒
`exec SpringBoard`；首启时 LaunchServices 需要建库（几十分钟），建好后 SpringBoard 起
来、CoreAnimation 软件渲染 + IOSurface + IOMFB 把界面送到屏幕。GPU 侧 Adreno 730 已经
冷启动 GMU（HFI 六步全确认）、OOB 拉起 GX、建立 GPU 自己的 SMMU context、启动 CP 并
执行任意 PM4（内核自检 + 用户态 ABI 自检都通过），但还没接进 UI。**触摸/按键仍无驱动**，
锁屏划不开——这是当前的主要工作（见 `docs/TOUCH.md`）。

## 2. 本机验证证据（2026-10-21，macOS 14.8.9 / x86_64）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 补丁系列 | `tools/verify-patches.sh`（xnu-8796.141.3 + dxb `6639065` + 三个 staging 脚本） | 78/78 干净应用 |
| 主机工具 | `python3 tests/test_tools.py` | 全部通过 |
| shim 编译 | `LLD=<zig ld.lld> shim/build.sh` | `out/Image` 122 880 B，无 ABS64 重定位 |
| boot.img | `tools/mkbootimg.py` + `tools/unpack_bootv2.py` | v2 头、dtb_offset/大小正确、可回读 |
| 触摸 harness | `tools/touch-novatek && make && ./test_nt36523` | 通过（NT36523 协议，非 NT36532） |

**没有做过的**：任何 `fastboot`、任何真机日志。内核编译的进展见 §6
（2026-10-22 已在本机链出 `kernel.development.liuqin`）。

## 3. 硬件事实（来源：主线设备树）

来源都是公开代码，不是猜测：

- `arch/arm64/boot/dts/qcom/sm8450.dtsi`（torvalds/linux）
- `arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts`（yzddmr6/linux-sm8450-liuqin）
- `include/dt-bindings/...`、`pm8350*.dtsi`

| 项目 | 值 | 出处 |
| --- | --- | --- |
| SoC | SM8475，1×Cortex-X2 + 3×A710 + 4×A510，ARMv9（本移植按 v8.2 编译） | `compatible = "qcom,sm8475"` |
| GICD | 0x17100000，64 KiB | `intc: interrupt-controller@17100000` |
| GICR | 0x17180000，0x200000，stride 0x40000 | `redistributor-stride` |
| 定时器 | 19.2 MHz；PPI 13/14/11/10 → 虚拟定时器 INTID **27** | `timer { interrupts }` |

2026-10-22 拿到用户提供的 stock `boot.img` / `vendor_boot.img` / `dtbo.img`
（都在 gitignore 的 `backup/` 里）：

- `boot.img`（192 MiB，sha256 `00df87c1…`）：Android boot **v4**
  （`header_size` 0x630，`signature_size` 4096），内核是 PE 格式的通用 Linux
  （`MZ` @0x1000，`image_size` 0x2040000），ramdisk 在 0x206e000、LZ4 压缩的
  first-stage init（21 个 cpio 条目，无 dtb）。**整个镜像没有 FDT**。
- `vendor_boot.img`（96 MiB，sha256 `752b7597…`）：v4，dtb 段 6,443,739 B
  在 0x1687000，是 **14 个 SoC 基础 DTB 的拼接**（cape / cape-v2 / capep /
  diwali / ukee / waipio / waipiop），其中 cape 三个的 `/memory` reg 全是 0
  ——ABL 启动时才填真实内存。cmdline 里有
  `liuqin:12/OS3.0.7.0.VMYCNXM`。
- `dtbo.img`（24 MiB，sha256 `21376daf…`）：44 个 overlay，liuqin 是第 15 项
  （752,518 B，`model = "Liuqin based on Qualcomm Technologies, Inc SM8475"`，
  `compatible = qcom,cape-mtp / qcom,cape`），ABL 按 msm-id/board-id 把
  基础 DTB 和它合并后交给内核。

所以真机 DTB 只能由 ABL 在运行时给出完整版本：离线能拿到结构（GIC 0x17100000、
`armv8-timer` 19.2 MHz/PPI 11、UFS 0x1d84000 都对得上），但内存 bank 必须
上机看。shim 现在会在屏幕上打印 `MODEL` 和 `MEM0BASE/MEM0SIZE…`，第一次
`fastboot boot` 就能读到。


## 4. 移植决策

1. **补丁系列**：从 ios-sweet 的 81 个补丁中移出 3 个纯 sweet 硬件驱动
   （GT9896/PM6150/RPMh 资源 ID），保留 78 个，全部 `sweet→liuqin` 重命名。
   移出的文件在 `xnu/patches/reference/`，不编译，作为移植模板。
2. **机器配置**：`MACHINE_CONFIG=LIUQIN`，`-march=armv8.2-a+crypto`（ARMv9 核可执行），
   16K 页、GICv3、无 PAC（与 sweet 相同的非 PAC ABI）。GICR stride 改为 0x40000，
   虚拟定时器 INTID 改为 27。
3. **帧缓冲**：0xb8000000 / 1800×2880。早期 console（补丁 0010/0012）、内核
   fbcon、IOMobileFramebuffer、HID 坐标归一化都改成这个几何。
4. **显示逻辑尺寸**：直接呈现 1800×2880 @2x（900×1440 pt）。sweet 的
   “iPhone X 1125×2436 居中”逻辑保留代码路径，但常量改成面板原生尺寸。
5. **内存**：默认取高端 bank，`mkadt.py --dtb <stock dtb>` 从本机 DTB 自动读
   `/memory`、`/intc`、`/timer`、`/smp` 帧缓冲；也支持 `--ram-gib 12` 和
   `--dram-base/size`。
6. **身份**：iPad8,1 / D321AP（11" iPad Pro，A12X，无 Home 键，120 Hz），
   `chip-id=0x8027`，`display-scale=2`。`/product` 节点需从 iPad8,1 的 IPSW
   提取（`research/ipad-d321ap/d321ap.adt`，不入 git）。
7. **UFS root**：驱动按 GPT 分区名找 `userdata`（`ufs_find_partition`），
   没有硬编码 LBA，所以分区表变化不影响；`rd=ufsroot0 ufsroot=userdata`。
8. **输入**：暂不提供。HID 服务（补丁 0058）已编入，但没有 liuqin 的触摸/按键
   驱动，backboardd 收不到事件——这是下一步的主要工作。

## 5. 风险与未验证项（按重要性）

1. **内存图**：高端 bank 的基址/大小、以及是否有 carve-out，全部来自通用
   SM8450 布局，没有本机 DTB 核对。上机前必须 `mkadt.py --dtb`，否则可能踩到
   firmware 保留区。8 GB 默认值在 12 GB 机上不会越界（只用到前 6 GiB），
   但反向不成立。
2. **MPIDR**：ADT 里 `reg = core << 8` 沿用 sweet 的写法（sweet 上 8 核可启动）。
   SM8475 是三簇，secondary CPU 的 PSCI 目标 ID 未验证；先用 `cpus=1`。
3. **iPad 身份**：D321AP/0x8027 是按机型选的，没有在 liuqin 上验证过。
   备选是 nabu 用过的 iPad7,3（J207AP），只改 `mkadt.py` 三个常量。
4. **显示**：ABL 的 splash 一直扫描输出；XNU 只是往 0xb8000000 写。面板 DSC/
   双 DSI 初始化、背光（KTZ8866，I2C 0x11）都没接管。
5. **USB**：补丁里的地址是 sweet 的 0x0a600000 / 0x088e2000；liuqin 的 DWC3 在
   0x0a600000，但 PHY 是 eUSB2 repeater，初始化未验证。
6. **内核只在主机上编译过**：2026-10-22 用 Xcode 15.4 链出了二进制（§6），
   但没有在真机上跑过；编译器差异暴露过一个补丁错误（`LIUQIN_FB_LOGICAL_W`
   未定义），说明“能编译”不能替代“能启动”。

## 6. 本机编译（2026-10-22，已产出内核）

装上 Xcode 15.4（15F31d）后，`tools/build-local.sh` 在本机（macOS 14.8.9 /
x86_64，无 Homebrew、无 KDK、无 sudo）一次跑通：

- 979 个目标文件编译、链接成功（Xcode 的 `iig` 生成 DriverKit 派发表，
  56 个符号全部解析）；
- `kernel.development.liuqin`：16,578,448 B，
  sha256 `f7455e21ef8ba77f793dd4c829d63ed472a83dff239061086c3a3bfce361042d`，
  `MH_EXECUTE / ARM64 / PIE`，入口 `0xfffffe000748b488`，10 个段；
- 版本串 `Darwin Kernel Version 22.6.0: Wed Oct 21 16:08:35 UTC 2026; qprs:e6a7809`；
- 没有 `LC_DYLD_CHAINED_FIXUPS`：链接器没有生成 rebase，`mkpayload.py`
  按 `fixups: 0` 处理。

打包链也在本机跑通：`payload.bin` 16,589,552 B（magic `LIUQIN01`，10 段，
`virt_base 0xfffffe0006000000`），`shim-boot.img` 16,719,872 B（boot v2，
header_size 1660）。**boot.img 里放的是 985 B 的合成测试 DTB，不是真机 DTB，
不能拿去刷机。**

编译过程中发现并修掉的真问题：

1. 补丁 0060 在 `LiuqinHID.cpp` 里用了只定义在 `LiuqinFramebuffer.cpp` 的宏
   `LIUQIN_FB_LOGICAL_W`，只有真正编译才会报错；改成字面量 1800/2880 后
   78 个补丁重新验证通过。
2. `tools/mkbootimg.py` 的 v2 头只有 1620 B（`id` 写成 8 B、缺
   `recovery_dtbo_size/offset` 和 `header_size`），`unpack_bootv2.py` 回读时
   dtb 字段错位。已改成 AOSP 的 1660 B 布局，测试同步更新。

## 6.1 为什么必须是 Xcode 15

xnu-8796 链接需要 `iig` 生成的 DriverKit IORPC 派发表。Command Line Tools
不含 `iig`；Xcode 16 的 `iig` 生成 `IORPC.kernelContent`，与 xnu-8796 不兼容。
所以固定 Xcode 15.x（CI 的 `macos-14` runner 同样是 15）。

本机 Xcode 没跑过 `xcodebuild -runFirstLaunch`（要 sudo），所以
`xcodebuild` 起不来；`tools/build-local.sh` 因此不用 xcodebuild，直接用
clang 编 libdispatch 的 `firehose_buffer.c` 得到 `libfirehose_kernel.a`。

## 6.2 第一次真机启动（2026-10-22）

`fastboot boot` 了 `shim/out/shim-boot.img`，屏幕（红底，shim 自己的 console）依次出现：

```
SHIM liuqin milestone 4: xnu loader
INITRD lo=0xB702C000 hi=0xB7FFE2F0
MODEL Liuqin based on Qualcomm Technologies, Inc SM8475
```

说明：ABL 接受了这个镜像并把 16.6 MB 的 payload 放在 0xB702C000；ABL 选择并
合并了 liuqin 的设备树（model 串来自 dtbo 里的 liuqin overlay）；`fdt_changed`
解析正常。

随后屏幕变蓝，这是 **shim 自己的异常处理**（`exc_handler`，蓝底 + `PANIC
SYNC`），不是 XNU 的 panic。当时的构建里 STAGE 只有 `loader` 一个值，无法
区分是 DTB 扫描、段拷贝还是跳转后 XNU 早期代码触发，所以：

- 给 loader 加了分阶段标记：`ldr-dtb` / `ldr-payload` / `ldr-phys` /
  `ldr-copy` / `ldr-adt` / `ldr-args` / `xnu`，panic 屏的 `STAGE` 行会指出
  最后一个完成的阶段；
- 同时打印 `MEMBANKS n=`（ABL 传进来的 DTB 里 /memory 的数量，0 表示没填）。
  实测是 **4 段**，但蓝屏把红屏冲掉了，看不到 base/size；
- 为此加了诊断镜像 `shim/out/shim-diag.img`：payload cmdline 带
  `liuqin_diag=1`，打印完 banks 和 payload 头之后 `wfi` 挂起、不拷贝也不跳转，
  用来把 4 段 `/memory` 完整留在红屏上拍照。

设备是 **8 GB 版本**（用户确认）。诊断镜像实测 `/memory` 有 5 段（2/2 cells）：

| base | size |
| --- | --- |
| 0x0000000080000000 | 0x6A000000 |
| 0x00000000F1400000 | 0x0EC00000 |
| 0x0000000800000000 | 0x39700000 |
| 0x0000000840000000 | 0x140000000 |
| 0x0000000839D00000 | 0x01E00000 |

合计约 7.84 GB，正好是 8 GB 机器。**高 bank 在 0x8_40000000（33 GiB），
不是 0x100000000** —— 这解释了正常镜像为什么在拷贝第一笔写 `0x101004000`
时就 fault。现在 ADT/payload 按 `0x840000000 + 0x140000000`（5 GiB 连续段）
生成，内核落到 `0x841004000`，`/pram` 在 `0x853FF00000`。

诊断镜像（`liuqin_diag=1`）把 ABL 填好的 4 段内存完整打了出来：

```
MEM0BASE 0000000080000000  MEM0SIZE 000000006A000000
MEM1BASE 00000000F1400000  MEM1SIZE 000000000EC00000   (正好到 0x100000000)
MEM2BASE 0000000800000000  MEM2SIZE 0000000397000000
MEM3BASE 0000000840000000  MEM3SIZE 0000000140000000
```

前两段是可信的（0x80000000+1696 MiB、0xF1400000+236 MiB，末尾正好 4 GiB 边界）；
后两段的基址在 0x8_00000000/0x8_40000000（32 GiB 以上），不是普通 RAM 地址，
但它们和“高 4 GiB 之上的 DDR”有什么关系还没确定。把 bank 上限提到 8 之后，
`/memory` 实际有 **5 段**——第 5 段很关键（可能就是真正的高 bank，或者它的
base 不在 0x100000000）。原始 cell 的 RAW 输出那一版
有 bug（`fdt_memory_raw` 没有复位节点状态，后面把 `/sram@17D09400` 的 reg 当成
了 memory 的，`RAW4..7` 是栈残留），源码已修（复位 + 最多 8 段），但本机缺 ELF
链接器，当前诊断镜像还是旧 `Image` 重新封装的，下一版会带上修正。payload 仍按
`0x100000000 + 6 GiB − 1 MiB` 摆放内核。

正常镜像（`shim-boot.img`）实测：红屏打到 `ENTRY` 就蓝屏，`SEGS` 没出来，
说明死在拷贝循环的第一笔写入（目标 `0x101004000`）——高 bank 很可能不在
`0x100000000`，或者那一带是保留区。需要蓝屏上的 `FAR` 确认。

另外 `print_bank()` 的 `switch` 被 clang 编译成 **绝对地址跳转表**，又是同一个
坑（诊断镜像打印第一段就 fault）。`shim/build.sh` 已加 `-fno-jump-tables`，
并把 switch 改成 if 链。

另外，第一次和第二/三次上机的蓝屏其实是 shim 自己的 bug：`loader.c` 里用
`static const char *base_label[4]` 这种字符串指针表，链接基址 0 而加载地址任意，
第一次 `con_hex(base_label[i], …)` 就 fault（`elf2bin --reloc-check` 看不到，
因为链接器把地址直接烧进了镜像）。改成展开打印后 4 段正常输出。**shim 里不要
放指针表/带指针初值的全局数据。**

同一次启动还暴露了一个必炸的匹配错误：内核 `LiuqinCPU` 的 `IONameMatch`
仍是 sweet 的 `qcom,kryo470`，而 ADT 的 cpu 节点写的是 `qcom,kryo780`
（Kryo 780 = SM8475）。已改补丁 0018/0038 并重编内核（sha256 `50bce05b…`），
否则 CPU 驱动永远不会 attach。

下一次上机要抓的是蓝屏上的 `SLOT / STAGE / ESR / ELR / FAR` 五行：ELR 落在
shim 的物理地址段说明还是 loader 的问题，落在 `0xfffffe0007…` 说明已经进了
XNU 的早期汇编。

## 6.3 XNU 第一次跑起来（2026-10-22）

换成真实内存段（`0x840000000`）之后，XNU 真的启动了：屏幕被 fbcon 接管，
kprintf 输出可见（`LIUQIN EARLY 20: kprintf startup complete`），然后 panic：

```
panic(cpu 0 caller 0xfffffe0007ad1dac): Undefined kernel instruction:
pc=0xfffffe0007491348 Instr=d51be228
```

`d51be228` 用 clang 汇编对照确认是 `msr CNTP_CTL_EL0, x8`；pc 落在
`_fiq_context_init+0x14`。源码里这段是：

```c
/* osfmk/arm64/machine_routines.c: _enable_virtual_timer() */
__builtin_arm_wsr64("CNTV_CTL_EL0", cntvctl);
/* disable the physical timer as a precaution ... */
__builtin_arm_wsr64("CNTP_CTL_EL0", CNTP_CTL_EL0_IMASKED);
```

根因：固件没有置 `CNTHCTL_EL2.EL1PCEN`，EL1 访问物理定时器是未定义指令。
这个移植只用虚拟定时器（INTID 27），那段“保险”删除即可。

- 新补丁 `xnu/patches/0079-liuqin-skip-physical-timer.patch`（79/79 干净应用）；
- 内核重编，sha256 `cc1be243…`，确认镜像里不再有那条指令；
- 仍在等待下一次上机验证。

## 6.4 XNU 走到 BSD init（2026-10-22）

把定时器补丁带上后，XNU 一路初始化：timebase、kprintf、zone allocator、
IOKit、网络协议栈（`gif0`/`stf0`/IPsec）、UFS 驱动，最后停在根文件系统：

```
LIUQIN UFS: command 0x00 failed: ocs 0x0 upiu 0x21 response 0x1 status 0x2
panic(cpu 0 caller ...): IOFindBSDRoot: LIUQIN UFS root ufsroot0 unavailable: 5
```

panic 报告里能看到 `Device: D321`、`Hardware Model: iPad8,1`、
`Kernel text base: 0xfffffe0007004000`、完整的 boot-args、zone 表、
backtrace 和 stackshot —— 身份、boot_args、符号化都对了。

`command 0x00` 是 TEST UNIT READY；设备回了 CHECK CONDITION
（response 0x1 = target failure，status 0x2），不是超时。驱动原来只重试 3 次、
没有 sense data，所以看不到原因。补丁 **0080** 加了：

- sense key / ASC / ASCQ 打印（从 response UPIU 的 offset 32 起）；
- TUR 重试 20 次、每次间隔 50 ms；
- LUN 0 不就绪时探测 LUN 1..3 并打印结果。

即使 UFS 修好，userdata 上目前也没有 iPadOS root，所以下一次仍会停在
`IOFindBSDRoot`——真正的下一个里程碑是用 iPad8,1 的 IPSW 构建 HFS+ root
并写入 userdata。

## 7. 下一步（具体动作）

1. 用 `cdcd6694` 内核的镜像再 `fastboot boot` 一次，读 UFS 的 sense data，
   判断 CHECK CONDITION 是 unit attention、LUN 不对还是设备状态问题。
2. 准备 iPad8,1 的 iPadOS 16.x IPSW，构建 HFS+ root 并写入 userdata
   （工具见 `tools/mkiosroot.sh`，需要 Linux 或能跑 sudo 的主机）。
2. 看 `MEM0BASE/MEM0SIZE`，确认真实内存 bank 和 §3 的假设是否一致；不一致就
   按实测 bank 重打 ADT（`--dram-base/--dram-size`）。
3. 过了早期启动后读持久 log（`liuqin-log-pull.sh`）。第一个真正的里程碑不是
   UI，而是看到 XNU 的 `LIUQIN` 早期 console 和 UFS 分区打印。
3. 上 PM8350 按键驱动（最小可用：电源键 + 音量），再上 NT36532。
4. 稳定后再试 `cpus=8`，确认三簇 MPIDR 与 PSCI。
5. 内核进真机后，再补 CTF/dSYM（本机脚本目前 `DO_CTFMERGE=0 BUILD_DSYM=0`）。

## 8. 交接

- **2026-10-24 仓库公开**：完整历史已推到 https://github.com/QPRS-6/iPadOS-liuqin
  （`main`，初始提交 `fd94d82`）。`.gitignore` 仍排除 `backup/` `dtb/` `recovery/`
  `research/`，仓库里没有设备转储、序列号或 Apple 二进制。这次 push 命中了
  `xnu.yml` 的 `xnu/patches/**` 路径过滤，内核 CI 已自动触发。

- **2026-10-24 USB 上电（0147）**：`liuqinusb=2` 那张没被主机识别。原因大概率是
  `fastboot boot` 交接后 ABL 把 USB30_PRIM 的 GDSC 折叠、分支时钟关掉，而 probe 只
  读不写，读到 off 就直接返回（`controller unpowered or unclocked; DWC3 not read`），
  gadget 从未启动。0147 在 `enabled>=2` 时自己打开 GDSC（清 SW_COLLAPSE、等 PWR_ON）
  并逐路打开 CBCR（写 CLK_ENABLE、等 CLK_OFF 消失，clkref 不动），每步打印结果，然后
  继续原来的 DWC3 读取与 gadget 启动。镜像 `shim/out/shim-boot-usb2.img`
  （sha256 `4b5b744c…`，内核 `fae1a62d…`）。

- **2026-10-24 USB 现状**：驱动早已在（补丁 0070-0074/0077/0078：DWC3 gadget、ep0、VBUS override、CDC-ECM、live log），但 `liuqinusb` 默认是 1（只报告状态），我们所有镜像都没带 `liuqinusb=2`，所以手机从来没在 iOS 里枚举过。新镜像
  `shim/out/shim-boot-usb.img`（sha256 `d42659d4…`，boot line 加 `liuqinusb=2`）用来验证枚举：设备应为 VID 0x1209 / PID 0x0001、CDC-ECM 两个接口。主机侧
  `tools/liuqin-usbnet.sh` 只支持 Linux（ip/nmcli），macOS 没有 CDC-ECM 驱动；要在 macOS 上用就得把 gadget 换成 NCM。

- **2026-10-24 桌面达成 + 输入计划**：静音启动等待 LaunchServices 首次 seeding 完成后，
  **锁屏界面出现**（CPU 软件渲染 + IOSurface + IOMFB，整条图形链路首次真正跑起来）。
  但触摸/按键都没有：HID 服务（0058/0068）已编入内核，`liuqin_hid_touch` /
  `liuqin_hid_key` 没人调用——缺驱动。硬件事实（来自 `yzddmr6/linux-sm8450-liuqin`
  分支 `liuqin-6.17`，`sm8475-xiaomi-liuqin.dts` + `sm8450.dtsi`，节点又引自 vendor
  ABL 的 FDT）：NT36532 走 **GENI SPI4 `@0x990000`**（QUPv3 wrapper 0 SE4，CS0，
  9.6 MHz 上限），IRQ = **TLMM gpio21**（上升沿），GIC SPI 605，软件复位寄存器
  `0x1fb43e`，固件 `novatek_nt36532_m81_fw_csot.bin` 由驱动 RAM 载入，引脚
  gpio16/17/18 (data/clk) + gpio19 (cs)，function `qup4`；GCC 侧
  `gcc_qupv3_wrap0_s4_clk` 使能 `0x62008` BIT(14)、halt `0x274dc`、RCG
  `0x274e4`。完整计划写在 `docs/TOUCH.md`（只读审计 → chip-id → 报点 →
  按键 SPMI）。

- **2026-10-24 桌面卡点定位到 LaunchServices/lsd**：静音启动的 log 证明软件显示
  链路是通的（`AppleCLCD 1800x2880`、`display on`、backboardd 建 IOSurface、
  `swap 1..3`），但之后十分钟只重复 exec `fairplayd.A2`/`bluetoothd`。用共享缓存
  （`ipsw dyld a2s`，slide 0x37150000）符号化这次的采样栈：SpringBoard t0 在
  `-[LSApplicationRecord initWithBundleIdentifier:…]` → `_LSContextInitCommon`
  （LaunchServices 数据库初始化），backboardd t8 在
  `_xpc_connection_send_message_with_reply_sync` 等同步 XPC 回复——两边都卡在
  LaunchServices/`lsd`（83.8 s exec，仍活着）。没有任何 crash 报告。新镜像
  `shim/out/shim-boot-sample-lsd.img`（sha256 `26ca9cad…`）静音并加
  `liuqinsample=SpringBoard,backboardd,lsd`，下次 log 带 lsd 的栈。

- **2026-10-24 “没有第二遍进度条”其实是控制台没静音**：GPU 这批镜像为了在屏幕上看
  IOLog 故意去掉了 `liuqinmute`，而 iOS 的第二遍进度条是用户态画在同一块屏上的，
  控制台文字会把 logo/进度条顶掉——看起来像“起不来”，其实上一份正常启动的 log 里
  SpringBoard/backboardd 都 exec 了、SMP 也在采样。`shim/out/shim-boot-mute.img`
  （sha256 `2809d735…`）是同一套参数加 `liuqinmute=1`：屏幕保留 logo/进度条，持久
  log 仍然收全部行，所以 GPU 进度和 `gputest:` 结果照样能拉到。

- **2026-10-24 整包 opt-in 镜像**：`~/liuqin-root/ipad71-root-optin.img`（16 GiB
  完整镜像，sha256 `8a4f3da9…`）＝ nohid root + 新版 opt-in 工具
  （`/usr/libexec/liuqin-gpu-test`，sha256 `a3a46027…`）+ 同一个 LaunchDaemon。
  稀疏镜像那次没起来，改用整包刷入。

- **2026-10-24 用户态测试改为 boot-arg 开关**：上一份 log（其实是上一轮启动）显示
  这次 `GPU ready` 在 **51 s**——GPU 服务在 launchd 还在起系统时就发布了，daemon 立刻
  提交，很可能把 GPU/显示一起带走（第二遍进度条画不出来、控制台文字顶掉 logo）。工具
  现在读 `kern.bootargs`，只有 boot line 里有 `sheng-gpu-gputest=1` 才跑，否则打印
  `gputest: skipped (…)` 退出；启用时也会在服务出现后再等 20 s。
  产物：`~/liuqin-root/ipad71-root-optin.simg`（sha256 `999968f3…`，272 块，以当前
  设备镜像为基准的稀疏 diff）与 `shim/out/shim-boot-gputest.img`
  （sha256 `43a6485e…`，boot line 带 `sheng-gpu-gputest=1`）。

- **2026-10-24 整包 userdata（含用户态 GPU 测试）**：
  `~/liuqin-root/ipad71-root-gputest.img`（16 GiB 完整镜像，sha256
  `a0377120…`），内容 = 之前刷过的 nohid root + `/usr/libexec/liuqin-gpu-test`
  + `/System/Library/LaunchDaemons/com.liuqin.gputest.plist`（RunAtLoad，
  stdout/stderr → `/dev/console`）。直接 `fastboot flash userdata` 整包刷入即可，
  比稀疏镜像慢但省事。

- **2026-10-24 用户态 ABI 测试工具（未上机）**：新增 `tools/liuqin-gpu-test.cpp`
  + `tools/build-gpu-test.sh`（iOS 17.5 SDK，iOS arm64，输出
  `shim/out/liuqin-gpu-test`），`tools/liuqin-root-tweaks.py` 会把它装成
  `/usr/libexec/liuqin-gpu-test` 并挂一个 RunAtLoad 的 LaunchDaemon
  （stdout/stderr 到 `/dev/console`，结果进持久 log）；tweaks 里加了 `safe_chown`
  以便在忽略属主的 macOS 挂载上跑。userdata 稀疏镜像
  `~/liuqin-root/ipad71-root-gputest.simg`（sha256 `926e59b0…`，1034 块，4 MiB，
  以“已刷入的镜像”为基准做 diff；工具最多等 10 分钟，因为这块板子的 GPU 大约在启动 5 分钟后才发布）。它走真实 ABI 做与内核自检相同的验证：
  Query/GetDeviceInfo → BoAlloc/BoMap/BoCpuMap → Submit(CP_MEM_WRITE) → WaitFence
  → 读回 0x600dc0de。**尚未上机**。

- **2026-10-24 第二十六次上机（hfi26 那张）——CP 执行通用命令流已验证**：
  `CP self-test submit 0x0 fence=3 result=0 observed=3 wrote=0x600dc0de (want
  0x600dc0de)`。内核侧自此完整且端到端验证：GMU 冷启动 → OOB GPU_SET → GPU SMMU
  context（bank2/asid2/streams 0/1 + 带 mask 的 SMR）→ ring@0x200000 + fence 在
  memptrs@0x300000 → CP_SQE_CNTL → CP_NOP 探针 → a7xx_cp_init → 时间戳 fence →
  CP_MEM_WRITE 自检 → 用户客户端发布。剩下的是**用户态**那一半：
  `NativeAdrenoABI.h`（v1，16 个方法）目前没有任何消费者（没有库、没有测试工具、
  没有 Turnip 后端）。下一步计划写一个最小用户态客户端：开节点 → 分配/映射 BO →
  提交 PM4 → 等 fence → 校验 GPU 写入的值（同一份自检，但走真实 ABI）。

- **2026-10-24 第二十五次上机（hfi25 那张）——GPU READY（整条链路首次完整跑通）**：
  `CP started fence=2`、`CP interrupt mask 0x3b283c2 (want 0x33b283c2) refused …;
  polling fences`、`client=ready`、`GPU ready, publishing services`、`GPU started
  on gpu`。中断只丢了 bit28，fence 轮询不受影响。0146 加内核态自检提交：
  `CP_MEM_WRITE` 0x600dc0de 到 scratch 0x302040，用时间戳 fence 收尾再读回，验证
  CP 对任意 PM4 命令流的执行。镜像 `shim/out/shim-boot-hfi26.img`
  （sha256 `4831bf03…`，内核 `4b077679…`）。

- **2026-10-24 第二十四次上机（hfi24 那张）**：CP 起来了，但尾段停在
  `cp interrupt setup stopped at 0xe00002ca`——`NativeAdrenoIrq::programMask` 写
  `RBBM_INT_0_MASK` 后读回不一致就 poison（这块 RBBM 不保留我们请求的全部位）。
  ring 的 fence 本来是轮询 memptrs 的，中断只是优化，不该挡住客户端。0145 改成
  best-effort，并打印 mask/status 读回。镜像 `shim/out/shim-boot-hfi25.img`
  （sha256 `f028ce9c…`，内核 `f545de0c…`）。

- **2026-10-24 第二十三次上机（hfi23 那张）——CP 跑起来了**：
  `CP probe fence=1 result=0 observed=1`（环/事件写/时间戳全有效），紧接着
  `cp started fence=2`（a7xx_cp_init 被执行并确认）。至此整条链路在真机上首次打通：
  GMU 冷启动 → OOB GPU_SET → 带 mask 的 GPU SMMU context → ring@0x200000 →
  `CP_SQE_CNTL` → ME_INIT → 时间戳 fence。之前 ME_INIT 卡住是因为 **SQE 启动后的
  第一次提交不会被执行**，用一个两字的 CP_NOP 提交“点着”之后 ME_INIT 就通了。
  0144 在运行中的 CP 上恢复提交路径：中断 attach+mask、`NativeAdrenoSubmit` 和
  `NativeAdrenoUserClient` 绑到 **GPU context 的 VM**（GMU 的 VM 没有 GPU stream
  可达的 iova），并把节点标记为 ready。镜像 `shim/out/shim-boot-hfi24.img`
  （sha256 `6ae4cd32…`，内核 `50adafc7…`）。下一步是用户态提交（Mesa/Turnip）。

- **2026-10-24 第二十二次上机（hfi22 那张）**：改成阶梯的零表 ME_INIT 后结果不变
  ——`rptr=17=wptr`、`hw_fault=0 cp_int=0 rbbm_int0=0`、memptrs 全 0。也就是没有任何
  fault，CP 把整条提交都取了，但时间戳没写。0143 在 ME_INIT 前加一个只含
  `CP_NOP` 的探针提交（其唯一效果就是环自己的时间戳事件写）：探针 fence 到达 →
  问题在 ME_INIT；不到 → 事件写包本身没执行。镜像
  `shim/out/shim-boot-hfi23.img`（sha256 `2b087769…`，内核 `d5b12ab0…`）。

- **2026-10-24 第二十一次上机（hfi21 那张）**：`rptr` 从 0 走到 17 并与 `wptr` 相等
  → **CP 确实取了环并执行到 ME_INIT**，ring/CP/SMMU 链路都通；但时间戳写回没发生
  （memptrs 全 0），说明 CP 停在 ME_INIT 包里。阶梯的 a7xx_cp_init **不带 power-up
  寄存器表**（最后三个字为 0），而 kit 的版本会把 CP 指向 0x301000 的表并置读表位。
  0142 改成按阶梯的十个字直接 `ring_.submit`，并在超时后补读 `CP_HW_FAULT`/
  `CP_INTERRUPT_STATUS`/`RBBM_INT_0_STATUS`。镜像 `shim/out/shim-boot-hfi22.img`
  （sha256 `dcaac7eb…`，内核 `8945cd74…`），待上机。

- **2026-10-24 第二十次上机（hfi19/20 那张）**：`CP_SQE_CNTL=1 written` 不再挂死
  ——SMR mask 就是根因；CP 起来了（`rbbm=0x1 hw_fault=0`），ME_INIT 提交成功
  （`fence=1`），但 2 s 内 fence 没到（`observed 0`），超时 `0xe00002d6`。0141 在
  提交后和超时后各读一次 `rptr/wptr`，并把 memptrs 前 4 个字打出来，用来区分“CP
  没取环”和“取了但时间戳没写回”。镜像 `shim/out/shim-boot-hfi21.img`
  （sha256 `0da50dcb…`，内核 `f81cbbb4…`）。

- **2026-10-24 第十九次上机（hfi19 那张）**：`GPU route smr1=0x84000000 s2cr1=0x2
  smr2=0x84000001 s2cr2=0x2`——两条 GPU route 都按阶梯的带 mask 形式落进去了。
  0140 在 `CP_SQE_CNTL` 之前加只读 SMMU preflight（IDR1 推导 CB 几何，读 SGFSR、
  bank2 的 SCTLR/TCR/TTBR0/FSR），用来确认 CP 取指路径是否已经 fault。镜像
  `shim/out/shim-boot-hfi20.img`（sha256 `9b5d7108…`，内核 `46d73522…`）。

- **2026-10-24 第十八次上机（hfi18 那张）**：顺序按阶梯改对后仍在 `writing
  CP_SQE_CNTL=1` 挂死，所以不是顺序。剩下的嫌疑是 **CP 自己的取指**：它经 GPU 的
  SMMU stream 读 SQE。kit 的 stream attach 写 `SMR = VALID|sid`（0x80000000|sid），
  而阶梯（以及 0102 给 GMU route 修的写法）是 `VALID|0x400<<16|sid`；qsmmu 会丢掉
  没有 mask 的 SMR，于是 GPU 的 stream 0/1 从未匹配，CP 取指走默认路径 fault，CP
  卡死并不再应答寄存器写。0139 在 context 建好后按阶梯重写两条 GPU route 并打印读回
  `GPU route smr1=0x84000000 s2cr1=0x2 …`。镜像 `shim/out/shim-boot-hfi19.img`
  （sha256 `8b9fbb3c…`，内核 `d2c2f257…`），待上机。

- **2026-10-24 第十七次上机（hfi17 那张）**：补了 BV rptr 和 RB_CNTL bit27 后仍在
  `CP_SQE_CNTL` 挂死。与阶梯 step 20 的剩余差异是**顺序**：阶梯先写
  `CP_AHB_CNTL=1`（第 12 条）再写 `CP_RB_*`（第 28–31 条），而我们的 ring attach
  在寄存器表之前就写了 `CP_RB_*`（此时 CP 的 AHB 还没开）。0138 把 `startCp` 拆成
  两相：相 0 写寄存器表 + 发布 power-up 表 → ring attach → 相 1 启 SQE + ME_INIT +
  fence。镜像 `shim/out/shim-boot-hfi18.img`（sha256 `65a2f2a6…`，内核
  `cd15ab84…`），待上机。

- **2026-10-24 第十六次上机（ladder20 那张）——阶梯证明这套 CP 序列在本机可行**：
  `CP_SQE_CNTL 0 -> 0x1 (reads 0x1)`、`done (rptr 10)`、`THE 3D CORE TAKES
  COMMANDS`。与我们的 `startCp` 逐条对比只剩两处差异，且都能解释“CP 卡住→总线
  不应答”：①**`CP_BV_RB_RPTR_ADDR` 我们没写**（复位 0，SQE 启动后 BV 环的 rptr 会
  往地址 0 写，翻译失败把 CP 卡死）；②**`CP_RB_CNTL` bit27**（阶梯 0x800020c，我们
  0x20c）。0137 补上这两条并把 SQE 启动恢复为默认开（`sheng-gpu-sqe-start=0` 可关）。
  镜像 `shim/out/shim-boot-hfi17.img`（sha256 `23396c41…`，内核 `4ee2b1b2…`）。

- **2026-10-24 第十五次上机（hfi16 那张）**：不启 SQE 时系统正常，数据齐全：
  `CP_SQE_CNTL reads 0x0`、`rbbm=0x1`、`hw_fault=0`、`sqe`/`rb` 读回与写入一致
  （0x100000 / 0x200000）→ CP 块活着且编程正确；ME_INIT 提交成功（fence=1）但
  fence 不到，说明 CP 没执行；写 `CP_SQE_CNTL=1` 才会挂总线。下一张
  `shim/out/shim-boot-ladder20.img`（sha256 `53f1eca3…`）用 `liuqinadreno=20`、
  不带 `sheng-gpu-bringup` 让阶梯自己跑到 step 20，逐条打印寄存器写入/读回，
  作为“这套 CP 启动序列在本机到底能否成功”的判据。

- **2026-10-24 第十四次上机（hfi15 那张）**：补了 GBIF_HALT 后仍旧停在同一行
  （`CP registers written, starting the SQE` 之后重启）——`CP_SQE_CNTL(0x0808)` 的写
  本身就是总线停住，别的 CP 寄存器都正常。0136 把这次写改成默认不发
  （`sheng-gpu-sqe-start=1` 才发），让系统能起完，同时先读一次 `CP_SQE_CNTL`、保留
  `sqe=`/`rb=` 读回、ME_INIT 提交和 fence 等待，用这一轮把 SQE 取指路径的情况摸清。
  镜像 `shim/out/shim-boot-hfi16.img`（sha256 `932480cf…`，内核 `aa7fa5a6…`）。

- **2026-10-24 第十三次上机（hfi13 那张）——GX 上电，GPU 窗口确认为 0x3d00000**：
  `OOB GPU_SET acked (intr=0x80000000)` 之后 `GPU window ADT 0x2d00000 id=0x0 |
  DT 0x3d00000 id=0x70040000 status=0x1`——ADT 的 gpu 节点地址是错的，vendor DT 的
  0x3d00000 才是活的；探测自动切换后 `CP ring attached`、寄存器表全部写完。随后挂死
  并重启，最后一行是 `CP registers written, starting the SQE`，即卡在
  `CP_SQE_CNTL=1`。0134 把这条写前后加面包屑，0135 补上阶梯 step 20 的第一条
  `GBIF_HALT(0x3c45)=0`（我们漏了，GBIF 停着 SQE 首次取指就完不成）。镜像
  `shim/out/shim-boot-hfi15.img`（sha256 `59b283e5…`，内核 `fe5a86f9…`），待上机。

- **2026-10-24 第十二次上机（hfi12 那张）**：HFI 30 确实被确认了（`elapsed` 1.4 ms →
  19 ms、`q1 rd/wr 0x1c`、`polls=87`），0x3d00000 的映射也成功
  （`GPU window DT map 0xfffffe503f9ec000`），但两个窗口仍读 0——地址和 vote 都没
  问题，是 GX 域压根没被抬起来。阶梯 step 18 还有一步 kit 没有的
  `a6xx_gmu_set_oob(GMU_OOB_GPU_SET)`：写 `HOST2GMU_INTR_SET` bit30 → 等
  `GMU2HOST_INTR_INFO` bit31 → 清除，之后 GMU 自己打开 GX GDSC。0133 加了
  `powerGx()` 并在冷启动后立刻执行。镜像 `shim/out/shim-boot-hfi13.img`
  （sha256 `cfc6ada5…`，内核 `db9c28ae…`），待上机。

- **2026-10-24 第十一次上机（hfi11 那张）**：两个 GPU 窗口都读 0（`ADT id=0x0 |
  DT id=0x0`），地址不是问题。解析 vendor DTB 确认：`kgsl-3d0@3d00000` 的 reg 是
  0x3d00000/0x40000 等，ADT 的 0x2d... 就是同一批块整体低 0x1000000，而 GMU 正是
  在 ADT 的 0x2d68000 上跑起来的——所以 GPU 块是**没上电**：GMU 在跑（always-on
  域），但从没抬 GX 域。缺的是阶梯 resume 的最后一条 **HFI 30**（初始 GX perf/bw
  vote，{0, 1, 1, 3}）；kit 的启动序列止于 START。0132 在六步之后补发这条，并在
  探窗口前留 20 ms 给域上电。镜像 `shim/out/shim-boot-hfi12.img`
  （sha256 `e6ab8bf7…`，内核 `60b8324e…`），待上机。

- **2026-10-24 第十次上机（hfi10 那张）**：`sqe=0x00000000 (want 0x100000)`——写进
  `CP_SQE_INSTR_BASE` 读回 0，整个 GPU 窗口（ADT 的 0x2d00000）不接受写，CP 失败是
  它的下游。地址对不上：vendor DTB 是 `kgsl-3d0@3d00000`、`gmu@3d69000`，阶梯也用
  0x3d00000 真读到过 RBBM_STATUS；而 ADT 是 0x2d00000（整体低 0x1000000），可 GMU
  又确实在 ADT 的 0x2d68000 上跑起来了。0131 双地址探测：读两边 dword0/0x210 并
  打印，选能应答的那个窗口给 ring/CP 用。镜像 `shim/out/shim-boot-hfi11.img`
  （sha256 `7841d743…`，内核 `1ebf742c…`），待上机。

- **2026-10-24 第九次上机（hfi9 那张）**：`CP SQE alive rbbm=0x0 hw_fault=0x0 rptr=0`
  之后 `CP start stopped at 0xe00002ca`（`kIOReturnIOError`，没有 fence 等待日志，
  说明错在 startCp 自己的步骤里）。查到两处：power-up 表被写进了 **fence 页**
  （传参错），而且失败路径不出声。0130 传对 pwrup buffer，并把每一步的状态打出来
  （`sqe=0x..%08x (want 0x100000)` 的读回还能判断 0x2d00000 这个 GPU 窗口到底能不能
  写——stock DT 里 GPU 在 0x3d00000）。镜像 `shim/out/shim-boot-hfi10.img`
  （sha256 `91a18643…`，内核 `8223c6ab…`），待上机。

- **2026-10-24 第八次上机（hfi8 那张）**：`CP ring attached ring 0x200000/0x8000
  fence 0x300000/0x1000`——ring 寄存器在 GPU context 自己的 buffer 上编程成功，
  rptr 也能读回；系统照常起完。0129 接上 CP 启动：按阶梯 step 20 的寄存器表
  （RBBM/UCHE/CP 配置 + CP_SQE_INSTR_BASE）→ `CP_SQE_CNTL=1` + 20 ms → A740
  power-up list + a7xx_cp_init 经 ring 下发 → 等时间戳 fence（2 s）。镜像
  `shim/out/shim-boot-hfi9.img`（sha256 `fd96301c…`，内核 `7097b58d…`），待上机。

- **2026-10-24 第七次上机（hfi7 那张）——GPU context 全部建成，不再 panic**：
  HFI 六步全过（`ack=0xde`），SMMU 空槽 1/2，GPU context `status=0 step 7`
  （prepare/program phase 5 13 写/attach 2 路校验/vm attach/5 个 buffer 全部映射），
  `CP: sqe 74672 bytes at 0x100000`。随后 `ring/SQE refused` 是因为
  `gpu_context_ready_` 从未置位。0128 置位它，并把旧的 ring 路径（在 GMU VM 的
  iova 0 上自建 buffer）换成 GPU context 自己的 buffer：ring 0x200000/32KiB、
  fence 在 memptrs 0x300000，cache 维护走 buffer 自己的物理页；CP init 留到下一步。
  镜像 `shim/out/shim-boot-hfi8.img`（sha256 `93de7d13…`，内核 `2f3272b5…`）。

- **2026-10-24 第六次上机（hfi6 那张）**：面包屑最后一行 `A3 i=16 smr=00000000
  s2cr=00020000`，之后立刻 data abort——故障在槽位 17 的 SMR/S2CR 读。两条事实：
  `IDR0=0x4c017e18` 的低 8 位 = 24 个 SMR（阶梯就是按这个上界扫的），我们硬扫 32；
  而且找到两个空槽后没有提前退出。0127 把扫描限制在前 16 项、逐项打印
  （`A3 ... free=`）、找齐就 break，并允许 `sheng-gpu-gpuslot0/1` 直接指定跳过扫描。
  镜像 `shim/out/shim-boot-hfi7.img`（sha256 `80724699…`，内核 `4820cf85…`）。

- **2026-10-24 第五次上机（hfi5 那张）**：`ColdStart status=0 ack=0xde elapsed=1408 us`
  全过；随后仍在同一个相对位置 `NativeAdrenoGpuService::start+0x14d4` data abort，
  而且 `GPU ctx call` 没打印出来——崩溃点在 HFI dump 之后、第一条 GPU context 日志
  之前，也就是四条 `PE_parse_boot_argn` 或 32 槽 SMR/S2CR 扫描里。0126 加面包屑
  `A1/A2/A3/A4`，并给 `sheng-gpu-ctx=0` 开关（跳过 GPU context，保证能起完）。
  镜像 `shim/out/shim-boot-hfi6.img`（sha256 `d76b184c…`，内核 `6cd9b68b…`）。

- **2026-10-24 第四次上机（hfi4 那张）——启动序列全部通过**：`ColdStart status=0
  ack=0xde stage=8 transport=0`，六条消息（性能表、带宽表、ACD 使能、ACD 表、
  CORE_FW_START、START）全部被确认，`q0 0x18/0x18`、`q1 0x18/0x20`。紧接着在 GPU
  context 建立阶段内核 data abort（还在 `GPU context status` 那行之前），照片太糊，
  pc/lr 读不准。0125 给这条路径加了逐步打印（call/allocate/enter/tables/buffers/
  prepare/program/attach/vm attach/map N + Step 回调），下次上机最后一行就能定位。
  镜像 `shim/out/shim-boot-hfi5.img`（sha256 `8ea9a0ec…`，内核 `2e0ad0e4…`）。

- **2026-10-24 第三次上机（hfi3 那张）**：`ack=0x5e`——性能表、带宽表、ACD 使能、
  ACD 表、CORE_FW_START 五步全部被固件确认，索引驱动读包完全正确。最后一步 START
  报 `status=1`（`ShengGmuArgument`）：kit 的编码器给 START 的是**零字**负载（只有
  首部），而我的交换函数拒绝 `words==0`。0124 允许零字命令（响应仍限 19 字）。
  镜像 `shim/out/shim-boot-hfi4.img`（sha256 `7fd64f41…`，内核 `b5b2d443…`），待
  上机；全过应为 `status=0 ack=0xde`，随后是 `GPU context status ...`、
  `CP: sqe ...`、ring attach。

- **2026-10-24 第二次上机（hfi2 那张）——HFI 真的通了**：`ColdStart status=10
  stage=6 ack=0x1e`，即性能表/带宽表/ACD 使能/ACD 表四步都收到了带正确序号的
  ACK；`q0 rd/wr 4/4`（GMU 消费了每条命令）、`q1 rd/wr 0/0x14`（写回五个 4 字
  ACK）。`transport=6` 失败在第五步 CORE_FW_START，原因是我的交换函数只看固定
  槽位 0/4/8/12，而第五个 ACK 在字偏移 16。之前"这颗 GMU 不推进索引"的结论作废
  ——那是 kit 路径下命令从未被消费时的读数。0123 改成阶梯的索引驱动：按 `write`
  发布、等中断、按 `read` 读包并推进，不再扫描、不再清零窗口，跳过 F2H 错误和别
  的序号。镜像 `shim/out/shim-boot-hfi3.img`（sha256 `7339aa23…`，内核
  `c1ec0d3d…`），待上机；成功时应是 `status=0 ack=0xde`，随后 GPU context 和
  `CP: sqe ...`。

- **2026-10-24 第一次上机（hfi=1 那张）**：`ColdStart status=1`（`ShengGmuArgument`），
  `proto stage=1 polls=0 transport=4 elapsed=4 us`——是我的新传输把**命令**包也卡在
  19 字（那是 ACK 的上限），性能表 ~150 字，第一次交换就被参数检查拒了，GMU 一个
  寄存器都没碰（失败路径随后把 GMU 复位，dump 里 `sysreset=0x1`）。0122 拆成
  `SHENG_HFI_COMMAND_WORDS=254` / `SHENG_HFI_PACKET_WORDS=19`，总预算 200 ms → 2 s
  （六条消息各 100 ms 放不下），ColdStart 行加 `ack=0x%x`。镜像
  `shim/out/shim-boot-hfi2.img`（sha256 `2e8b83c8…`，内核 `af323341…`），待上机。

### 2026-10-24（HFI 改用阶梯的中断传输，待上机）

- 事实：这颗 Gen7 GMU 会把 ACK 包写进响应窗口并抬起 `GMU2HOST_INTR`，但
  **两个队列的 read/write 索引都不推进**（还顺手改写了表头 `size` 字段）。
  kit 的 `AdrenoHfiPoll` 是索引驱动的，所以真实 ACK 在内存里也只会报
  `Empty`；0116/0119 的扫描能过 stage 1，但把环弄错位，最后 panic
  （`_kalloc_type_view_parse`，0120 已撤）。
- 0121 新增 `ShengGmuColdStartInterrupt()`：按阶梯 `hfi_send` 的顺序，清零两个
  窗口 → 在窗口基址发布命令并把命令侧索引写成一致状态 → clean + 清旧中断 +
  脉冲 `HOST2GMU_INTR` → 限时等 `GMU2HOST_INTR_INFO` bit0 并 CLR → invalidate
  后只接受"长度 1..19 + id 相同 + 序号相同"的包（4 个按字对齐的基址槽）。
  不推进任何固件自己的索引，stale 包因为窗口已清零且序号在涨，不可能误判。
  用 `sheng-gpu-hfi=1` 选择这条路径，`0`（默认）仍是 kit 的索引传输。
- 同一补丁加了 `mode=intr` 的对照打印（table 6 字、两组索引、rsp/cmd 前 4 字、
  `intr`）。判读：`transport=4` = 门铃没抬起来（通知路径问题）；`transport=6`
  且 `intr=0x1` = 答了但包不在模型位置（看 rsp/cmd dump）；`transport=7` =
  固件拒绝，`fwerr` 是它的错误码。
- 镜像 `shim/out/shim-boot-hfi.img`（sha256 `bba49838…`，内核 `3d6336e7…`），
  boot line = cp15 那行再加 `sheng-gpu-hfi=1`。**尚未上机。**
- 本轮增量构建用 `WORK=/tmp/liuqin-build3 tools/build-local.sh`（干净，0 error），
  `tools/validate-repo.py` 通过，`tools/verify-patches.sh` 现在**全部 120 个补丁
  干净应用**。顺带修了两个一直存在的补丁问题：0112 加了三个 dump 参数却没改
  `q1 %08x %08x %08x` 格式串（导致 0113 的上下文对不上），0120 的上下文早于
  0119 加的那句注释。修完后从零重建的树与构建树在 0121 涉及的每个文件上逐字节
  相同。


### 2026-10-24 上午（桌面卡点：BKS check-in，不是 GPU）

- 把 root 里的 `HIDPlugins/SessionFilters` 和 `ServicePlugins` 改名后重新刷
  userdata：backboardd 的 `BKHIDSystem init` 过了（sampler 能跑满 40 次采样，
  之前卡在 sample 8 的插件枚举）。
- 但 SpringBoard 只是往前走了一步：现在停在
  `BKSSystemShellService _checkIn`（同步 XPC 等 backboardd 回复），backboardd
  主线程在 run loop 空闲、一个队列线程在 `dispatch_group_wait`；backboardd 在
  整个 boot 里 533 次探测 `IOAcceleratorES` 都是 none。
- ios-sweet 的 NOTES 记录过同一个墙（"no GPU, so no CoreAnimation renderer
  yet"），他们最后是靠 CPU 渲染 + 显示模式/scale/root 状态修过去的，不是 GPU。
  liuqin 现在在同一位置。
- 下一步候选：对比 sweet 的 display-scale（liuqin 2 / 1800x2880，sweet 3 /
  1080x2400）和 IOMFB frame-info/refresh 返回值；再决定是否要给 backboardd
  一个 stub accelerator 或改渲染路径。

### 2026-10-24 凌晨（GPU：接上 GPU context）

- 冷启动成功后服务现在会建 GPU 自己的 SMMU context：bank/asid 默认 2/2
  （`sheng-gpu-gpubank`/`gpuasid` 可覆盖），stream 0/1 都路由到 bank 2；
  两个 SMR 槽自动取 GMU 之后的空闲项（SMR 无 VALID 且 S2CR type 2），
  也可用 `sheng-gpu-gpuslot0/1` 指定。
- 成功时打印 `GPU context status 0x0 step 7` 和
  `GPU buffers sqe 0x100000 ring 0x200000 memptrs 0x300000 pwrup 0x301000
  scratch 0x302000`。
- 旧的 ring/SQE 块仍会拒绝（`gpu_context_ready_` 还没置位）——下一步是 CP 提交
  （SQE 微码、pwrup 列表、ring 门铃）。
- 镜像 `shim-boot-gpuctx.img`（内核 `76ddbef1…`，sha `069fe391…`）。

### 2026-10-24 凌晨（GPU：Sheng 冷启动真机跑通）

- `ColdStart status=0 fw_ready=1 hfi_ready=1 stage=1 stopped_at=0
  fw_init=0x100 hfi=0x1 polls=20`：CM3 释放、固件 `FW_INIT_RESULT=0x100`、
  HFI ready、`AdrenoHfiStartupRun()` 走完（stage 1）——A730 命令 DB 生成的
  vote/ARC/BCM 配置被 GMU 接受了。**Sheng 冷启动整条链在真机上通了。**
- 关键修复序列：0094 scattered、0095 16K 页、0096 带宽 0 档、0097 io/services、
  0102 SMR/S2CR（VALID+mask）、0103 100ms 轮询、0104 ICACHE/HFI cache clean。
- 下一步：GPU context（bank 2/asid 2，stream 0/1）+ SQE/pwrup + CP 提交，
  然后接 user client。服务现在会打 `ring/SQE refused: the GPU context ... is
  not built`，这是预期行为。

### 2026-10-24 凌晨（GPU：固件没跑，因为 ICACHE 还在 CPU cache 里）

- 100 ms 预算下 `fw_init=0x0 polls=783`：CM3 从没写 `FW_INIT_RESULT`。
- 固件和 HFI 队列表是普通 cacheable CPU 写，GMU 通过 SMMU 读 ICACHE；
  阶梯每次加载完 ICACHE 和初始化 HFI 后都会调 `to_memory()`（clean 到 PoC），
  而 Sheng 框架只做了 `dsb`，GMU 可能取到 cache 里的旧/零数据。
- 补丁 `0104-liuqin-gmu-cache-clean.patch`：`ShengGmuServices` 加 `clean`
  回调，固件加载后、CM3 释放前对 icache/dummy/debug/log/hfi 做 clean；
  `NativeAdrenoGmuPlatform::services()` 指向
  `NativeAdrenoMaintainOwnedPages(..., NativeAdrenoClean)`。
- 新镜像 `shim-boot-route3.img`（内核 `26e4407b…`，sha `53869602…`）。

### 2026-10-24 凌晨（GPU：CM3 起来了，FW_INIT_RESULT 轮询超时）

- 按阶梯值重写 SMR/S2CR 后，完整 ColdStart **不再复位**：
  `route smr0 0x84000005 s2cr0 0x1`、`ColdStart status=6 fw_ready=0 hfi_ready=0
  stage=0`。status 6 = `ShengGmuTimeout`，卡在 `FW_INIT_RESULT` 轮询。
- 移植的 `poll()` 只给固件 10 ms（A740 的预算）；A730 的 CM3 要通过 SMMU 取
  ICACHE，10 ms 不够。改成 100 ms，并在 ColdStart 行里加打
  `fw_init=<最后读到的值> hfi=<值> polls=<次数>`，用来区分“还没 ready”（0）
  和“掩码不对”（例如固件写 0xbabeface 而代码等 0x100）。
- 新镜像 `shim-boot-route2.img`（内核 `8faeca0e…`，sha `67efd4cb…`）。

### 2026-10-23 深夜（GPU：GMU 的 stream match 没有 VALID）

- probe3 打出：`scr0 0x2e0406 smr0 0x00000005 s2cr0 0x1 smr1 0x0 s2cr1 0x20000`。
  SCR0 已使能、S2CR0 是 context 1，但 SMR0 的 VALID 位是 0（阶梯写的是
  `0x84000005` = VALID | mask 0x400 | sid 5）。
- 阶梯的写法：`SMR = VALID | (0x400 << 16) | sid`；kit 的
  `ArmSmmu4KAttachStreams()` 只写 `VALID | sid`，没设 mask，qsmmu 最后把
  VALID 位留成了 0。这样 stream 5 不匹配，CM3 第一次取指（IOVA 0x4000）
  落到默认 fault/bypass，正好对应复位。
- `shim-boot-route.img`（内核 `beed603b…`，sha `d4762dcc…`）：带
  `sheng-gpu-route=1` 时在 GMU context 建好后按阶梯的值重写 S2CR/SMR 并打印
  回读 `route smr0 … s2cr0 …`，然后跑完整 ColdStart（去 mute）。
- 判读：回读保持 0x84000005 且 CM3 还复位 → 路由不是全部原因；回读还是 0x5
  → qsmmu 拒绝这个写，得换方式建 context。

### 2026-10-23 深夜（GPU：qsmmu 的 context bank 读会 panic）

- probe2 在打印 `readonly` 之后 panic（Kernel data abort）：多读的 context
  bank SCTLR（偏移 0x8000）在这颗 `qcom,qsmmu-v500` 上会 trap——hypervisor 只
  模拟全局寄存器（SCR0/SMR/S2CR），阶梯也只碰这些。
- probe3（内核 `5ca024b5…`，sha `3b40709a…`）去掉了 CB 读，保留
  `scr0/smr0/s2cr0/smr1/s2cr1`。对照阶梯：SCR0 0x002e0406、SMR0
  0x84000005（stream 5）、S2CR0 context 1/type 0。
- 另外：probe2 的 panic 有完整文本（去 mute 后），所以之前 gmu 镜像的“无文本
  复位”也可能是一次被 mute 掉的 panic——下一步若 probe3 的 SMR/S2CR 不对，
  就能解释 CM3 释放后的取指失败。

### 2026-10-23 深夜（GPU：stop1–4 全过，复位在 CM3 释放/固件取指）

- stop1/2/3/4 四张镜像**都能读条**：19 条 RSC/PDC、FAL/TCM retention、
  固件加载+TCM 回读、配置回读（含 SYSRESET 保持 1）都安全。
- 所以复位就发生在 `CM3_SYSRESET = 0` 之后、CM3 第一次取指时。首要怀疑
  GMU 的 SMMU context：CM3 从 IOVA 0x4000 取 ICACHE，走 stream 5；如果
  这个 stream 没有像阶梯那样路由到 context 1（SMR0 = 0x84000005），取指就会
  fault 或 bypass 到 DRAM。
- 新探针 `shim-boot-probe2.img`（内核 `ae071c60…`，sha `f583807e…`）在 GMU
  context 建好后只读地打印 `scr0 smr0 s2cr0 smr1 s2cr1 cb1_sctlr`，用来和
  阶梯的值对比（SMR0 0x84000005、S2CR0 context1/type0、SCR0 0x002e0406）。
- 如果 SMR0/S2CR0 是 0 或 type 2（bypass），就是 context 路由的问题。

### 2026-10-23 深夜（GPU：探针只读安全；复位在写路径，开始二分）

- 探针镜像上机：**没有复位**，打印
  `readonly sysreset 0x1 hfi 0x0 cx 0x0 rscc 0x0 ao 0x0 cm3 0x0`——六个状态
  寄存器都读得到，而且正好是 `ShengGmuLoadAndStartHfi()` 写之前要求的状态
  （CM3 在复位、HFI 未 ready、retention 清、RSC idle、无 fault）。
- 所以 0097 的复位发生在**写**路径，不是读。
- 探针那次 boot 的 HFS+ root 连续 37 次挂载失败（`unknown Volume Signature :
  666b`），但下一次 boot 正常挂载并跑到 1305 s，**userdata 没坏**，是那次
  boot 的偶发问题（那张镜像去掉了 mute）。
- 新工具：`sheng-gpu-stop=N`，在 LoadAndStartHfi 的第 N 个阶段后直接返回，
  不做任何清理写：1=19 条 RSC/PDC 写后，2=FAL/TCM retention 后，3=固件加载+
  TCM 回读后，4=配置寄存器回读后（CM3 释放前）。
- 四个镜像 `shim-boot-stop1..4.img` 都是**去 mute** 的，屏幕能看到
  `ColdStart status=... stopped_at=N`。按 1→2→3→4 刷，第一次复位的那个 N 就是
  挂住的位置。

### 2026-10-23 深夜（GPU：ColdStart 开始碰 GMU 后整机复位）

- 0097 的镜像：`gpu ladder done at 47.024 s` 之后整机重启，没等到任何
  `ColdStart status` 行。这是第一次 ColdStart 真正访问 GMU（io/services 已接），
  复位点可能在：第一次 GMU 寄存器读、19 条 RSC/PDC 写、FAL/TCM retention、
  或 CM3 释放后的固件启动。
- 诊断镜像 `shim-boot-probe.img`（内核 `541d1cf9…`，sha `58958a3d…`）：
  去掉 `liuqinmute`（屏幕显示所有 IOLog），加 `sheng-gpu-readonly=1`——只读
  六个 GMU 寄存器然后停，不写任何寄存器。
- 判读：出现 `NativeAdreno: readonly sysreset ...` = 读没问题，复位在写/固件
  路径；在那行之前复位 = `liuqinadreno=5` 停下的时钟/电源状态下 GMU 窗口不可
  安全访问，需要让阶梯多跑几步或显式给 GMU 上电。
- 另：用户没说是 panic 屏还是直接重启；下次留意有没有 panic 文本。

### 2026-10-23 深夜（GPU：startup config 过了，ColdStart 拿到全零 device）

- 0096 上机：`startup config refused` 消失，说明 0 带宽档的修复生效；ColdStart
  真的被调用了：`ColdStart status=1 fw_ready=0 hfi_ready=0 stage=0`。
- status 1 = `ShengGmuArgument`：`ShengGmuColdStart()` → `LoadAndStartHfi()` 的
  第一个 validate 检查 `d->io` / `d->services`，而服务只建了 `gmu_io_` 和
  `services`，从没写进 `gmu_device_`，所以 device 全零、在任何寄存器访问前被拒。
- 补丁 `0097-liuqin-gmu-device-io.patch`：调用前赋值
  `gmu_device_.io=gmu_io_; gmu_device_.services=services;`。
- 新镜像：内核 `1acebdee…`，`shim-boot-gmu.img` `bd7450b9…`，默认
  `shim-boot.img` `a340e62f…`。**未上机**。下一轮进入真正的 GMU load：
  RSC/PDC 19 写、FAL、TCM retention、HFI 队列、15 个固件块、TCM 回读、
  FW_INIT_RESULT 轮询、HFI start；任何非 0 status 都直接发我。

### 2026-10-23 深夜（GPU 自举：GMU context 过了，startup config 被拒）

- 0095 上机：`GMU payloads mapped … ttbr0=0x100084711c000`、`GMU firmware
  validated (57272 bytes)` 都出来了——16K 页这条通了。停在
  `startup config refused`。
- 已知 bug：`AdrenoHfiBuildBandwidth()` 要求带宽表第一项是 0 且严格递增，
  生成器把 0 过滤掉了（表从 1720 开始），已修（7 档，从 0 起）。
- 另外加了诊断：`ShengGmuBuildStartup()` 记录 `sheng_gmu_vote_stage`
  （1 参数/2 GPU ARC/3 GMU ARC/4 BCM aux/5 带宽/6 最终校验）和各 aux 的字节数，
  失败时服务打印一行 `startup config refused stage N (...)`。
- 新镜像：内核 `51ad7a6b…`，`shim-boot-gmu.img` `d2e9d6df…`，默认
  `shim-boot.img` `1fa8458c…`。**未上机**。下一轮看 `ColdStart status` 或
  stage 号：2/3 = 命令 DB 的 ARC 表和这颗 SoC 不匹配，4 = BCM aux 不是 8 字节
  记录，5 = BCM 地址/尺寸（SH0/MC0/ACV 是猜的那组）。

### 2026-10-23 深夜（GPU 自举卡点：16K 页被 4K 检查挡住）

- 0094 的镜像上机：`16760832 bytes not contiguous, retrying scattered` 之后
  仍然 `allocation failed 0xe00002c7`。两次都在描述符之前失败，根因不是内存，
  而是 `NativeAdrenoMemory::allocate()` 开头的 `NativeAdrenoQueryCpu4K()`：
  它只接受 4 KiB 粒度内核，而这个移植是 **16 KiB 页**。
- 补丁 `0095-liuqin-gmu-16k-pages.patch`：接受 16 KiB（TG0=2/TG1=1），并在
  16 KiB 页上关掉 write-combine（`pmap_set_cache_attributes()` 按 4 KiB 页号
  编程，在 16K 内核上会改到无关页）；显式 cache 维护不变。
- 新镜像：内核 `5e32e4b1…`，`shim-boot-gmu.img` `267582ea…`，默认
  `shim-boot.img` `a5c19869…`。**未上机**。下一轮预期能看到 GMU context 的
  Prepare/Program/Attach/Map 走完，进入固件加载和 ColdStart。

### 2026-10-23 深夜（GPU 自举第一次真机：卡在 16 MiB ICACHE 分配）

- `shim-boot-gmu.img` 上机（`liuqinadreno=5` + sheng 参数）：CX 窗口
  `0x2d9e000/0x1000` 对了，SMMU 读到 `id2=0x5511`、`scr0=0x2e0406`，
  GMU 五窗口绑定成功，ColdStart 路径真的跑到了。停在：
  `GMU context stopped at step 1 status 0xe00002c7`（kIOReturnUnsupported）。
- 原因：GMU allocator 第一块是 16 MiB 的 ICACHE（0xffc000），要求物理连续；
  拿不到连续段时 `NativeAdrenoMemory::allocate()` 以 Unsupported 失败。
  SMMU 是逐页映射的，不需要物理连续（Linux 那边也是 IOMMU 映射）。
- 补丁 `0094-liuqin-gmu-scattered.patch`：连续分配失败时退回 scattered，
  并打印大小和回退；这样下一轮要么过 Allocate，要么报出具体是哪个 buffer。
- 新镜像：内核 `2787b458…`，`shim-boot-gmu.img` `6d5b2dfb…`，默认
  `shim-boot.img` `f7cb672a…`。**未上机**。下一步看
  `GMU payloads mapped` / `firmware validated` / `ColdStart status`。

### 2026-10-23 深夜（GPU 路线 A：Sheng 表和服务自举已编进内核）

- 移植了 `ShengGmu.c` / `ShengGmuVotes.c` / `ShengGmuCommandDb.c`（之前只有头，
  所以服务里的 ColdStart 整段被编掉），并加进 `iokit/conf/files.arm64`。
- 新工具 `tools/prepare-liuqin-gmu.py` 生成三张表：寄存器/19 条 RSC-PDC/
  窗口/固件 sha、GPU OPP（vendor DTB 的 13 档 + DDR 带宽表）、命令 DB
  （0x80860000/0x20000）。偏移用 `CORE_GPU_DELTA=0`、窗口内 dword，和阶梯的
  `GMU_REG()` 等价。
- 服务改了两处：GMU 固件改从 fwarea 读（`liuqin_fw_get("gmu_gen70000.bin")`，
  弱符号里没有固件）；CX 窗口按 `reg-names = "cx_misc"` 找（原来是 index 1，
  实际是 `cx_dbgc`）。
- 内核里 “not generated” 字符串消失，`ColdStart status=...` 在。
- 新实验镜像 `shim/out/shim-boot-gmu.img`（`d4c48f2e…`，内核 `db5577b1…`），
  cmdline 加 `liuqinadreno=5 liuqinsvc=1 sheng-gpu-bringup=1
  sheng-gpu-bank=1 sheng-gpu-asid=1 sheng-gpu-sid=5 sheng-gpu-slot=0
  sheng-gpu-cmddb-version=1`。**没上机**。默认镜像 `shim-boot.img` 仍是
  `56dc6823…`（同样换了新内核）。
- 下一步：先上机看 `ColdStart status`；如果 status=0 / fw_ready=1 /
  hfi_ready=1，再接 `NativeAdrenoGpuContext`（bank=2 asid=2，slots/sids 0/1）
  和 ring/SQE。BCM 的 SH0/MC0/ACV 是唯一没把握的数据。

### 2026-10-23 深夜（GPU：GMU 修复真机确认，选路线 A）

- 新镜像（`b0a98ee5`）上机确认了 cell 顺序修复：日志有
  `NativeAdreno: GMU five-window I/O adapter bound`，三个服务照常
  attach/start/register，GPU 节点仍是 `discovered`。
- 服务起不了 CP 的原因已定位：构建里没有 `ShengGmuSource.h` /
  `ShengGmuVotesSource.h` / `ShengGmuCommandDbSource.h`，服务的 GMU
  ColdStart 整段被 `__has_include` 编掉（内核里只有 “not generated” 字符串）。
  路线定为 **A：为 liuqin 生成这三张表**，让服务自己起 GMU/GPU/CP。
  完整的数据清单、19 条 RSC/PDC 写、窗口/偏移公式（`(word<<2)-0x69000`）、
  固件与 command DB、以及缺的 OPP 带宽/BCM 数据都写在 `docs/GPU_NEXT.md`。
- 已知的坑：`ShengGmuBindMmio()` 会逐窗口比较 physical/bytes，而服务目前
  混用了 ADT 相对地址（0x2d…) 和映射后的 VA；生成表之前必须先定死用哪一套，
  否则 bind 返回 `ShengGmuArgument`。
- 下一步（按 GPU_NEXT.md 的 5 个检查点）：先写
  `tools/prepare-liuqin-gmu.py` 生成三张表并让 “not generated” 消失，再接
  `NativeAdrenoGpuContext`。

### 2026-10-23 深夜（UFS HS 真机验证通过；下一步是用户态）

- 0092 的时钟层镜像已上机（`shim-boot-hs.img`，内核 `2d33f4bc…`，
  `ufsphyhs=1`）：**HS 生效**。首个 1024 条命令窗口 74 MiB / 4.663 s =
  ~16 MiB/s（之前 0.5 MB/s），延迟 min/avg/max 15/176/2898 us（之前
  730/112050/843414 us），>1 ms 的命令 4/1024（之前 1022/1024）。
  完整日志与解读在 `docs/BOOT_PERFORMANCE.md` 的 “What it printed” 一节。
- 启动里程碑（内核时钟）：`ufs init start` 43.9 s、`root device ready`
  47.08 s、`exec launchd` 47.14 s、`exec SpringBoard` **49.65 s**、
  `exec backboardd` 49.85 s。20 分钟的 rootfs 读取没有了。
- 三个日志问题已在同一轮修掉：`host restart` 之前错把 HCS 0xf 当成功
  （HCS 0x8 = UIC ready 就够了，当时其实重启成功）；DME_POWERON 在这颗
  控制器上返回 1（invalid opcode），Linux 也不发，已删；吞吐行的单位写成
  `KiB/s`，实际打印的是 MiB/s，已改。
- `hs after … pm 0x55 tx 1 rx 1` 是**过时的回读**（还是 ABL 的 SLOWAUTO
  值），不能拿它判断 HS；16 MiB/s 和 176 us 延迟才是证据。
- 新镜像（HS 变默认，`ufsphypwm=1` 保留旧 PWM 路径）：内核 16,833,296 B
  sha256 `b0a98ee5…`；`shim-boot.img` `788e43e4…`，`shim-boot-pwm.img`
  `b265f8fe…`。**这两个新镜像还没有上机**（上机的是 `2d33f4bc` 那版）。
- 现在停在用户态，和 UFS 无关：SpringBoard（pid 26）活着但卡在同一 pc
  （SMP stackshot 反复打印），CommCenter 崩了 428 次（sig 11 / reason 0xb），
  fairplayd.A2、bluetoothd 每 ~10 秒被 launchd 重新拉起；全程没有一条
  `LIUQIN exec failed`。下一步是 GPU/Metal 那条线（见
  `docs/GPU_MILESTONE.md`），不是继续动 UFS。

### 2026-10-23 夜（UFS 时钟层，已编译、未上机）

- 新补丁 `xnu/patches/0092-liuqin-ufs-clock-layer.patch`，加在 0091 之上
  （全系列 91 个补丁）：GCC 时钟层、主机侧时钟寄存器、Linux 的 PHY 上电顺序，
  以及 PA_PWRMODE 的修正。源码是 `bsd/dev/liuqin_ufs.c`，细节和下一次启动该
  看的日志行都在 `docs/BOOT_PERFORMANCE.md` 最后一节。
- 重新解读第三次上机的数字：`uic reset 60` 是 ETIMEDOUT（Darwin errno 60），
  `enable/poweron/link 16` 是 EBUSY（`ufs_uic()` 的 “UIC not ready” 早退，
  HCS bit 3 为 0），都不是 UIC 结果；`pm probe fast=10` 才是 UIC 结果
  = invalid value。**PA_PWRMODE 是两 nibble（RX 7..4 / TX 3..0），FAST/FAST
  应为 0x11，之前写的是 1**，所以“设备拒绝 HS”这个结论要撤回。
- 这一版补上的东西：GCC 的 GDSC/BCR/全部分支（含 `gcc_ufs_0_clkref_en`
  0x9c000 和 tx/rx symbol clock）、RCG 频率（axi/unipro/ice 300 MHz、
  aux 19.2 MHz，寄存器值由日志反解）、`SYS1CLK_1US` (0xC0)、CFG1 的
  `QUNIPRO_SEL` 和 device ref clock (bit 26)、`DME_VS_CORE_CLK_CTRL`
  (0xD002) / `PA_VS_CORE_CLK_40NS_CYCLES` (0x9007)；PHY 改成 `SW_PWRUP`
  先置位、表在后、`SW_RESET` 只清不置、只开 `SERDES_START`。
- 本机编译（macOS / Xcode 15.4）：`kernel.development.liuqin` 16,833,296 B，
  sha256 `2d33f4bc4c576c5fbfd07eb08a2c2f72d5ccf4e6608a93b232678986dfcec964`；
  默认镜像 `shim/out/shim-boot.img` 31,809,536 B `c12bc453…`，HS 实验镜像
  `shim/out/shim-boot-hs.img` `6ce15fed…`（cmdline 只多 `ufsphyhs=1`）。
  **两个镜像都没有上机。**
- 主机侧验证：`tools/verify-patches.sh` 91/91 干净应用（新建的干净树，
  `xnu-8796.141.3`），`tests/test_tools.py` 全过，`validate-repo.py`
  91 个补丁/19 个工具/11 个脚本全过。这些都不是启动结果。
- 下次上机：先跑默认镜像确认 PWM 路径没被破坏；再跑 `shim-boot-hs.img`，
  依次看 `gcc rates …` / `phy post … ready 0x43` / `dme … -> 0` /
  `hs … pwr 0x11 -> 0` / `hs after … pm 0x11`。若 `phy post` 还是 ready 0x4，
  下一步把 block reset 提到 `ufs_take_over_host()` 之前；若 UIC 仍 `EBUSY`，
  看 `gcc rates` 里的 unipro 是否是 300 MHz。

### 2026-10-23 晚（启动速度：UFS 停在 PWM）

- 真机上量了启动：内核只用 3.4 秒，20 分钟几乎全在用户态读 rootfs。
- UFS 逐条延迟：最小 730 us、平均 112 ms、最大 843 ms（1022/1024 > 1 ms），
  有效吞吐 ~0.5 MB/s，正好是 PWM-G1 的量级。
- 链路属性：设备支持 HS-G4，TX/RX 档位能写进去，但 `PA_PWRMODE` 停在
  SLOWAUTO，写 FAST/FASTAUTO 被拒（UIC 返回 10/2）。
- 已排除（都在真机上试过，见 `docs/BOOT_PERFORMANCE.md`）：控制台绘制、
  exec 路径、读预取、直接继承 ABL 的链路、绕过 PHY 的轻唤醒。
- 结论：驱动里的 QMP PHY 只做了"上电+启动"三行寄存器，没有 HS 配置，
  所以 M-PHY 起不来 HS。唯一的修法是移植 Linux
  `phy-qcom-qmp-ufs.c` 里 SM8450 的 SerDes/TX/RX/PCS 表（0091 已移植；
  0092 补上外面的时钟层，见本节最上面的新条目）。
- 测量工具都在 `0090-liuqin-boot-timing.patch`（boot 标记、延迟直方图、
  强制显示的关键行、`liuqinmute` 提前到平台专家、exec 时间戳）。

### 2026-10-23（NativeAdreno 接线，待上机）

- 镜像已打好，**没有上机**：`shim/out/shim-boot.img`（sha256 `693b0227…`），
  boot line 在 `docs/GPU_MILESTONE.md` 的 “Image” 一节，新增参数 `liuqinsvc=1`。
  上机后先看持久 log 里 `NativeAdreno:` 开头的行，再看 `ioreg -l` 有没有
  `NativeAdrenoGpuService`（应带 `native-adreno-state = discovered`）。
- 本轮修了三件“以前一直没生效”的事：`0082` 与 `0042` 重复的 hunk（旧树是在
  重复 hunk 出现之前建的，所以本机一直没发现，重新 `verify-patches.sh` 会停在
  0082）；NativeAdreno 服务向 `this` 而不是 provider 要 device memory；解析
  reg/iommus 时按大端 FDT 读，而本移植的 ADT 是主机序。
- NativeAdreno 的 62 个文件和三个 kit 现在**在仓库里**（`0089` 补丁），不再只
  存在于 `/tmp/liuqin-build2`。`/tmp` 会被系统清理，别再把唯一副本留在那儿。
- 构建目录是 `/tmp/liuqin-build2`（`WORK=/tmp/liuqin-build2 tools/build-local.sh`，
  增量约 1 分钟）；`SEED_XNU` 可以复用已经打好的树。
- **2026-10-23 第一轮上机结果**：服务全起来了（`nubs ... = 1 1 1`、三个服务
  attach/start/register、GPU 节点已发布），系统照旧走到 `exec launchd`。我自己的
  `parseReg` 把 ADT 的两 cell 值拼反了（低字在前，被当成高字），所以 GMU 绑定
  失败；映射不受影响（走 XNU 自己解析的 `reg`）。已修，日志细节在
  `docs/GPU_MILESTONE.md` 的 “First on-device round”。
- 下一次上机的顺序：先确认多出 `GMU five-window I/O adapter bound` 那行，再让
  服务接管 `liuqinadreno=N` 阶梯已经建立的状态，最后才是 Mesa/Turnip 用户态。

- 本仓库里**没有任何**来自真机的 dump；`dtb/`、`backup/`、`research/` 都在
  `.gitignore` 里，保持这样（含序列号与校准数据）。
- `shim/out/`、`xnu/out/` 是构建产物，不入 git；本机内核的构建目录是
  `/tmp/liuqin-build`（`tools/build-local.sh`，默认 `WORK`）。
- 内核、payload、boot.img 都已就绪：boot.img 里嵌的是 vendor_boot 的
  14 个基础 DTB（cape 优先），dtb 加载地址 0x3000000（避开 17 MB 的 payload
  ramdisk）。内存 bank 要等第一次上机看 shim 的打印（见 §3、§7）。
- 不要改 `boot`/`recovery`/`modem`/`persist` 分区；只覆盖 `userdata`。
- 改补丁后先跑 `tools/verify-patches.sh`，再花 CI 时间。
