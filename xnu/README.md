# XNU for liuqin

The kernel is built by `.github/workflows/xnu.yml` on a macOS runner with
[blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build) at a
pinned commit (MIT).

- `MACOS_VERSION=13.5` -> `xnu-8796.141.3` (the tag macOS 13.5 pins; same
  generation as iOS 16.6/16.7, the last iOS for the A11/A12 devices).
- `MACHINE_CONFIG=LIUQIN` -> `pexpert/pexpert/arm64/LIUQIN.h`, 16K pages,
  GICv3, GICR stride 0x40000, virtual timer INTID 27, non-PAC arm64.
- `xnu/patches/*.patch` are copied into the build script's patch directory and
  applied in order. `*.sh` staging scripts fetch and integrate Apple's
  libpthread, hfs and BearSSL sources before the patches run.
- `xnu/patches/reference/` is **not** applied: it is sweet's GT9896/PM6150
  driver code, kept as a template for the liuqin input drivers.

Output: `kernel.<config>.liuqin` (with the stock build script it lands in
`dxb/fakeroot/System/Library/Kernels/`).

Verify the series without a Mac/Xcode:

```sh
tools/verify-patches.sh
```

Fetch the CI artifact:

```sh
gh run download -R QPRS-6/iPadOS-liuqin -n xnu-kernel -D xnu/out
```
