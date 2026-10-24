# 本机编译内核（macOS + Xcode 15，不需要 Homebrew / sudo / KDK）

结论：`tools/build-local.sh` 一条命令就能在本机产出
`kernel.development.liuqin`。2026-10-22 实测通过。

## 实测环境

- macOS 14.8.9（x86_64），16 核
- Xcode 15.4（15F31d），`xcode-select -p` 指向它
- SDK `MacOSX14.5.sdk`（Xcode 自带）
- **没有** Homebrew、`/Library/Developer/KDKs`、sudo 权限

Xcode 15 是唯一的硬要求：xnu-8796 链接时需要它的 `iig` 生成的 DriverKit
IORPC 派发表（56 个符号）。Command Line Tools 不含 `iig`，Xcode 16 的 `iig`
又生成了 xnu-8796 不认识的 `IORPC.kernelContent`，所以必须是 15.x。

## 产物

| 文件 | 大小 | sha256 |
| --- | --- | --- |
| `xnu/out/kernel.development.liuqin` | 16,578,448 | `` |
| `shim/out/payload.bin`（内核 + ADT） | 16,589,552 | `` |
| `shim/out/shim-boot.img`（含 vendor_boot 的 14 个基础 DTB） | 23,166,976 | `` |

内核是 `MH_EXECUTE / ARM64 / PIE`，入口 `0xfffffe000748b488`，10 个段，
`LC_DYLD_CHAINED_FIXUPS` **不存在**（链接器没有生成 rebase，`mkpayload.py`
按 `fixups: 0` 处理）。版本串：

```
Darwin Kernel Version 22.6.0: Wed Oct 21 16:08:35 UTC 2026; qprs:e6a7809
```

`KERNEL_BUILD_DATE` / `KERNEL_BUILD_OBJROOT` 可以覆盖它（脚本默认用源码
tag 的提交日期和 tag 名；注意 newvers.pl 只对尚未 stamp 的 `version.c` 生效，
改动后要删 `$WORK/build/xnu.obj/*/version.c` 才会重新生成）。

## 用法

```sh
tools/build-local.sh                  # 首次约 20 分钟（16 核），增量约 1 分钟
tools/build-local.sh --clean          # 丢掉 build/ 与 fakeroot/，源码保留
WORK=~/liuqin-build tools/build-local.sh
KERNEL_CONFIG=RELEASE tools/build-local.sh
```

脚本会自己下载并应用：xnu-8796.141.3、darwin-xnu-build 的基础补丁、
`xnu/patches/*.sh`（libpthread / hfs / BearSSL）和 89 个 liuqin 补丁。
所有中间件都装在 `$WORK/fakeroot`，不碰系统目录，不需要 sudo。

之后打包：

```sh
python3 tools/mkadt.py shim/out/liuqin.adt
python3 tools/mkpayload.py "$WORK/fakeroot/System/Library/Kernels/kernel.development.liuqin" \
    shim/out/liuqin.adt shim/out/payload.bin --cmdline 'debug=0xc serial=3 cpus=1 ...'
shim/build.sh shim/out/payload.bin     # DTB=... 指向真机 DTB；macOS 上 LLD=/path/to/ld.lld
```

## 脚本做了什么

1. 用 `xcrun` 找到 Xcode 15 的 `iig` / `mig` / `unifdef` 和 SDK。
2. 拉 xnu tag，按 `tools/verify-patches.sh` 同样的顺序打补丁（可用
   `SEED_XNU=` 复用已经验证过的树）。
3. 编 `AvailabilityVersions`（SDK 里没有 `availability.pl`）和
   `libplatform` 头文件，装进 fakeroot。
4. `make installhdrs`：把 Kernel.framework 头文件装进 fakeroot。
5. 从 libdispatch 编 `libfirehose_kernel.a`（plain arm64；上游只出 arm64e，
   而 liuqin 是非 PAC 的 arm64）。
6. `make install` 出内核，装进 `fakeroot/System/Library/Kernels/`。

## 和 CI 的差异

| 项 | CI（`xnu.yml` + dxb） | 本机脚本 |
| --- | --- | --- |
| iig / mig | Xcode 15（runner 自带） | Xcode 15.4 |
| KDK | 下载安装 13.5 KDK | **不需要**（LIUQIN 不在 KDK `-force_load` 列表里） |
| libfirehose_kernel | xcodebuild + KDK 头 | clang 直接编 libdispatch 的单个源文件 |
| LTO | `BUILD_LTO=0` | 同 |
| CTF（`__CTF` 段） | 有（dtrace 的 ctfmerge） | `DO_CTFMERGE=0`，无 CTF |
| dSYM | 有 | `BUILD_DSYM=0`，无 dSYM |
| 版本串 | dxb 固定 tag 日期 | 脚本固定 |

CTF/dSYM 只影响 lldb 调试体验，不影响启动。要补 dSYM，在跑过
`xcodebuild -runFirstLaunch` 的机器上去掉 `BUILD_DSYM=0` 即可。

## 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| `iig not found` | `sudo xcode-select -s /Applications/Xcode_15.4.app/Contents/Developer` |
| `xcodebuild ... Abort trap: 6`、`CoreSimulator.framework` 缺失 | 新装的 Xcode 没跑过 `xcodebuild -runFirstLaunch`；脚本已绕开 xcodebuild |
| `env: python: No such file or directory` | 脚本会在 `$WORK/bin/python` 建 python3 的符号链接 |
| `unifdef` 返回 1 | 正常：它删掉了条件编译行，脚本按成功处理 |
| 链接报 56 个 `*::Dispatch(IORPC)` | 用错 Xcode（16 或只有 CLT） |

## 为什么不再用“复制 SDK 头文件”的 iig 桩

早先没有 Xcode 时，我用一个复制 SDK DriverKit 头文件的假 `iig` 让编译走到
链接，但 `.iig.cpp` 里的 56 个派发符号仍然缺失，链接必然失败。现在装了
Xcode 15，这条路径已经废弃，脚本不再需要任何 iig 桩、firehose 桩或 mig
wrapper。
