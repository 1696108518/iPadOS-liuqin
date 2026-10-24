# iPadOS-liuqin

**把 Apple 开源 XNU（xnu-8796，iOS 16 世代）移植到 Xiaomi Pad 6 Pro（`liuqin`，高通 SM8475 / Snapdragon 8+ Gen 1），上面跑未修改的 iPadOS 16 用户态。**

本项目是 [dipa-studio/ios-sweet](https://github.com/dipa-studio/ios-sweet)（Redmi Note 10 Pro，SM7150）的 fork，重新适配到 SM8475；硬件资料参考了：

- [yzddmr6/xiaomipad-6pro-mainline](https://github.com/yzddmr6/xiaomipad-6pro-mainline) —— liuqin 的主线 Linux 设备树、分区与器件资料；
- [mytiantian001/iPadOS-nabu](https://github.com/mytiantian001/iPadOS-nabu) —— Xiaomi Pad 5 上的 iPadOS 用户态、iPad 身份与 Novatek 触摸协议。

> **当前状态（2026-10-24）：平板已经能启动到 iPadOS 锁屏界面。** XNU 跑起未修改的 iPadOS 16 用户态：launchd、系统 daemon、backboardd、SpringBoard 全部起来，CoreAnimation 用软件渲染写进 IOSurface，再由我们自己的 IOMobileFramebuffer 送到面板。**还没有触摸和按键驱动**，所以锁屏划不开。GPU 侧 Adreno 730 已经能冷启动 GMU、拉起 GX、建立 SMMU context、启动 CP 并执行命令流，但 UI 还没用上它。这是实验性 bring-up，不是可用的平板系统。

## 已经验证的（在这台 Mac 上，可复现）

| 项目 | 结果 |
| --- | --- |
| 内核补丁系列 | 146 个补丁按序应用到 `xnu-8796.141.3`（macOS 13.5 对应的 tag），叠加 darwin-xnu-build 的基础补丁与 Apple libpthread / hfs / BearSSL 后，**0 失败** |
| ADT 生成 | `tools/mkadt.py` 生成 1800×2880、GIC 0x17100000、vtimer INTID 27、6 GiB 高端内存的 liuqin 设备树；`--dtb` 能从原厂 DTB 自动取内存/GIC/定时器/帧缓冲 |
| 引导 shim | 用 clang `--target=aarch64-none-elf` + LLD 在 macOS 上编译链接通过，`tools/elf2bin.py` 检查无绝对重定位 |
| boot.img | `tools/mkbootimg.py` 打出 v2 头（含 DTB），`tools/unpack_bootv2.py` 可回读 |
| 主机测试 | `python3 tests/test_tools.py` 全绿（FDT 解析、ADT、boot.img 往返） |
| 触摸协议 harness | `tools/touch-novatek/` 的主机测试通过（NT36523 协议；NT36532 待真机核对） |

## 还没做的

| 项目 | 状态 |
| --- | --- |
| 触摸 / 按键 | **未移植，当前的主要工作**。HID 事件服务（`iokit/Kernel/arm/hid/LiuqinHID.cpp`）已在核里，但没有驱动去喂它：NT36532 触摸走 **GENI SPI4 @0x990000**、IRQ GPIO21，按键在 PMIC（SPMI）。硬件事实与计划见 [docs/TOUCH.md](docs/TOUCH.md) |
| GPU 进 UI | Adreno 730 已能冷启动 GMU、拉起 GX、建 SMMU context、启动 CP，并验证任意 PM4 执行与时间戳 fence；但 CoreAnimation 仍走软件渲染，上面还没有 Mesa/Turnip 后端 |
| 其它外设 | Wi-Fi、蓝牙、蜂窝、音频、相机、传感器、电池计、SEP 都没有；对应 daemon 会启动失败并反复重启 |
| 首次启动慢 | 首启时 LaunchServices 要建立数据库（在这块存储上要几十分钟），之后启动明显变快 |

## 启动链

```text
ABL（原厂解锁 bootloader）
  -> fastboot boot shim-boot.img             （临时启动，不写 boot 分区）
     -> shim：自建 MMU、读 live FDT、从 ramdisk 加载 payload
        -> XNU Mach-O + 生成的 ADT + boot-args
           -> XNU -> UFS 驱动 -> userdata 里的 HFS+ root
              -> dyld + shared cache -> launchd -> daemon -> backboardd / SpringBoard
```

liuqin 没有可用串口（调试 UART 没有引出）。内核 console 画在 ABL 留下的
`0xb8000000` 帧缓冲上，同时写进 `userdata` 末尾的持久 ring，用 recovery 读出来。

## 构建

### 1. 内核（需要 macOS + Xcode 15，或 GitHub Actions）

本机（已装 Xcode 15）一条命令：

```sh
tools/build-local.sh
# -> /tmp/liuqin-build/fakeroot/System/Library/Kernels/kernel.development.liuqin
```

不需要 Homebrew、sudo 或 KDK，细节见 [docs/LOCAL_BUILD.md](docs/LOCAL_BUILD.md)。
补丁在 `xnu/patches/`，由 [blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build)
驱动（固定 commit `6639065`）。GitHub runner 上是：

```sh
gh workflow run xnu.yml -R QPRS-6/iPadOS-liuqin
gh run download RUN_ID -R QPRS-6/iPadOS-liuqin -n xnu-kernel -D xnu/out
```

产物是 `kernel.development.liuqin`（`MACHINE_CONFIG=LIUQIN`）。本地验证补丁能否应用：

```sh
tools/verify-patches.sh          # 下载 xnu-8796.141.3，应用 dxb 基础补丁 + 本仓库系列
```

`tools/verify-patches.sh` 需要 `git`、`curl`；会联网拉取 Apple 的 xnu/libpthread/hfs 与 BearSSL。

### 2. 引导链（本机即可）

```sh
# 原厂 DTB：解包手机自己的 boot.img/vendor_boot.img
python3 tools/unpack_bootv2.py backup/boot.img dtb/boot_unpacked

python3 tools/mkadt.py shim/out/liuqin.adt --dtb dtb/boot_unpacked/dtb
python3 tools/mkpayload.py xnu/out/kernel.development.liuqin shim/out/liuqin.adt shim/out/payload.bin \
    --cmdline 'debug=0xc serial=3 cpus=1 keepsyms=1 -enable_kprintf_spam -noprogress rd=ufsroot0 ufsrw=1 rootrw=1 launchd_unsecure_cache=1 liuqinfb_noconsole=1 wdt=-1 wdt_test=no_user_checkin ignition_level=1 ufslogsecs=1'

LLD=$(command -v ld.lld) shim/build.sh shim/out/payload.bin    # -> shim/out/shim-boot.img
```

macOS 上没有 `ld.lld` 时：`python3 -m pip install --target .tools/ziglang ziglang`，
然后 `LLD="$PWD/.tools/ziglang/ziglang/zig ld.lld" shim/build.sh ...`（或 `brew install llvm`）。
Linux 上装 `gcc-aarch64-linux-gnu` 后直接 `shim/build.sh`。

### 3. iPadOS root（Linux）

```sh
tools/mkiosroot.sh OS.dmg SystemOS.dmg AppOS.dmg ipados16-root.img 16G
sudo fsck.hfsplus -fy ipados16-root.img
sudo mount -t hfsplus -o loop,force ipados16-root.img /mnt/ipadroot
sudo python3 tools/liuqin-root-tweaks.py /mnt/ipadroot
sudo umount /mnt/ipadroot
img2simg ipados16-root.img ipados16-root.simg
```

`mkiosroot.sh` 需要一个 iPadOS 16.x 的 iPad8,1（iPad Pro 11 第一代，D321AP）IPSW。
`liuqin-root-tweaks.py` 会跳过 Setup、写入 activation 状态、关闭会杀慢进程的 watchdog，
并按 root 里的 `SystemVersion.plist` 生成 DataMigration 数据。

### 4. 刷入

```sh
fastboot -S 512M flash userdata ipados16-root.simg   # 清空 Android 用户数据
fastboot boot shim/out/shim-boot.img
```

`boot`/`recovery`/`modem`/`persist` 分区都不动；`userdata` 会被覆盖，先备份。
定位参考基准可以用 [iPadOS-nabu](https://github.com/mytiantian001/iPadOS-nabu) 的 bring-up 流程。

## 与 ios-sweet 的硬件差异（本仓库改了什么）

| 项目 | sweet（SM7150） | liuqin（SM8475） | 依据 |
| --- | --- | --- | --- |
| GICD / GICR | 0x17a00000 / 0x17a60000 | **0x17100000 / 0x17180000** | `sm8450.dtsi` |
| GICR stride | 0x20000 | **0x40000**（8 个 redistributor） | `redistributor-stride` |
| 虚拟定时器 INTID | 19（PPI 3） | **27（PPI 11）** | `timer { interrupts }` |
| 帧缓冲 | 0x9c000000，1080×2400 | **0xb8000000，1800×2880**（43 MiB） | `framebuffer@b8000000` |
| UFS 控制器 | 0x1d84000 | 0x1d84000（相同） | `ufshc@1d84000` |
| 高端内存 | 0x1_00000000 + 0xfcc00000 | **0x1_00000000 + 6 GiB**（8 GB 机；12 GB 用 `--ram-gib 12` 或 `--dtb`） | 板级内存布局 |
| 显示 | 6.67" 1080×2400 | 11" 2880×1800 120 Hz，双 DSI + DSC | `sm8475-xiaomi-liuqin.dts` |
| 触摸 | Goodix GT9896，GENI I2C | Novatek NT36532，GENI **SPI** | 同上 |
| PMIC | PM6150/PM6150L | PM8350/PM8350B/PM8350C/PMK8350 | `pm8350*.dtsi` |
| 设备身份 | iPhone X（iPhone10,3 / D22AP） | **iPad Pro 11"（iPad8,1 / D321AP）** | `tools/mkadt.py` |

所有常量都集中在 `tools/mkadt.py`、`shim/console.h`、`shim/shim.c` 和补丁
0004（机器配置）里；`docs/PORTING.md` 是逐项清单。

## Boot-args

| 参数 | 作用 |
| --- | --- |
| `rd=ufsroot0 ufsrw=1 rootrw=1` | root 在 `userdata` 的 HFS+ 上，可读写（GPT 里按名字找分区） |
| `cpus=1` | 只启动 1 个核；`cpus=8` 仍待验证（SM8475 是三簇：X2/A710/A510） |
| `maxmem=` | 限制可用内存，便于二分 |
| `liuqinfb_noconsole=1` | 不把内核文本画到 GUI 上（仍写持久 log，panic 会画） |
| `liuqinfb_hz=30` | 面板 CPU 拷贝帧率 |
| `liuqinfb_native=1` | 用真实面板尺寸而不是 1800×2880 逻辑尺寸 |
| `ufslogsecs=1` | 每秒刷一次持久 log ring |
| `wdt=-1 wdt_test=no_user_checkin` | 关闭 Apple watchdog |
| `liuqinusb=1/2` | USB 探测 / 枚举（地址沿用 sweet，未在 liuqin 上验证） |

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| `shim/` | 裸机引导 shim（C + asm），打包成 Android boot.img v2 |
| `xnu/patches/` | 89 个 XNU 补丁（按序 `git apply`） |
| `xnu/patches/reference/` | ios-sweet 的 Goodix/PM6150 驱动，作为移植模板保留，不编译 |
| `.github/workflows/xnu.yml` | 内核 CI（macOS runner，`MACHINE_CONFIG=LIUQIN`） |
| `tools/mkadt.py` | 生成 Apple Device Tree（liuqin 常量 + `--dtb` 自动探测） |
| `tools/fdtread.py` | 纯 Python FDT 解析（无 dtc 依赖） |
| `tools/mkbootimg.py` | boot.img v2 打包（macOS 上替代 AOSP mkbootimg） |
| `tools/elf2bin.py` | ELF64 → 平铺镜像 + 绝对重定位检查 |
| `tools/mkpayload.py` | 打包 kernel + ADT + boot-args |
| `tools/mkiosroot.sh` | 从 IPSW 构建 HFS+ iPadOS root（Linux） |
| `tools/mkiosroot-macos.sh` | 同上，macOS 版（hdiutil + newfs_hfs，需要 sudo） |
| `tools/liuqin-root-tweaks.py` | root 的 bring-up 修改 |
| `tools/liuqin-log*.sh`, `liuqin-crash.py` | 从 recovery 读持久 log / 符号化 |
| `tools/touch-novatek/` | Novatek 协议 harness（主机测试） |
| `tests/test_tools.py` | 主机侧工具测试 |
| `docs/LOCAL_BUILD.md` | 本机（Xcode 15，无 Homebrew/sudo/KDK）编译内核的完整说明 |
| `NOTES.md` | 工程记录：验证证据、硬件事实、风险、下一步 |

不在 git 里：分区备份、live device tree、recovery 镜像、Apple 固件/root 镜像、
下载的 build tree。它们含设备序列号/校准数据，或 Apple 版权文件。

## 下一步（按优先级）

1. **产出内核**：跑一次 `xnu.yml`，拿到 `kernel.development.liuqin`；先 `cpus=1` 上机，看帧缓冲 console 和持久 log。
2. **核对内存图**：用 `mkadt.py --dtb` 从本机 DTB 取真实高端内存（8/12 GB、有没有 carve-out）。
3. **按键**：PM8350 PON（SID/寄存器取自 `pm8350.dtsi`），从 `reference/` 的 `sweet_keys.c` 改。
4. **触摸**：NT36532 over GENI SPI；先用 `tools/touch-novatek/` 的思路抓一次协议，再写 `liuqin_touch.c`。
5. **显示**：面板 DSC/双 DSI 初始化仍由 ABL 的 splash 维持；UI 出现后再处理背光（KTZ8866）与亮度。

## Credits / 许可

基于 Apple 开源发布（xnu、IOHIDFamily、libpthread、hfs）、[BearSSL](https://bearssl.org)、
[blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build)、
[ipsw](https://github.com/blacktop/ipsw)、Qualcomm/Xiaomi 公开的 Linux 内核与设备树、
以及 ios-sweet / iPadOS-nabu 的 bring-up 工作。iOS/iPadOS 本身属于 Apple，不在此分发。
