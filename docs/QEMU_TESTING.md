# QEMU testing (kernel-only changes, no hardware)

`tools/qemu-liuqin.py` boots the same payload the shim boots, on QEMU's `virt`
board, so a kernel change can be checked without touching the tablet.

It places the segments, the ADT and `boot_args` where `shim/loader.c` would,
starts the CPU at a small stub and then dumps what the kernel drew.

## Layout used in QEMU

| | value | why |
| --- | --- | --- |
| RAM | 0x40000000, 4 GiB (`-m 4096`) | covers both regions below |
| framebuffer | 0xb8000000, 1800x2880 | keeps liuqin's hardcoded FB address |
| kernel `phys_base` | 0xc0000000, 0x3d000000 | above the framebuffer, inside RAM |
| GICD / GICR | 0x08000000 / 0x080a0000 | QEMU virt |
| CNTFRQ | 62.5 MHz | QEMU virt |
| virtual timer | INTID 27 | same as liuqin |

Build the QEMU-flavoured ADT with `tools/mkadt.py out.adt --qemu`; the defaults
match the table.

## Run

```sh
python3 tools/mkpayload.py kernel.development.liuqin liuqin-qemu.adt payload.bin \
    --cmdline 'debug=0xc serial=3 cpus=1 keepsyms=1 -enable_kprintf_spam -noprogress'
python3 tools/qemu-liuqin.py payload.bin out/ --secs 120
```

`out/` then contains:

- `fb.png` — the 1800x2880 framebuffer as the tablet panel would show it;
- `qemu.log` — exceptions with ESR/FAR/ELR (`-d int,guest_errors`);
- `regs.txt` — CPU state when the run stopped;
- with `--kernel kernel.development.liuqin` (unstripped Mach-O), `fb.txt` is the
  console text decoded through the kernel's `iso_font`, and `result.json`
  summarises the run. `--fail-on-panic` exits 2 on a visible panic; a blank
  screen is not proof of success.

QEMU runs with one CPU and no UFS: root on `rd=md0` (a RAM disk) only tests the
early kernel path, not storage or SMP.
