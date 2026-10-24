# Reference: sweet's input drivers (not applied, not built)

These are the original, unrenamed files from
[dipa-studio/ios-sweet](https://github.com/dipa-studio/ios-sweet)
(`xnu/patches/0060`, `0062`, `0063`). They are kept as the template for the
liuqin input drivers because they are the only working, on-hardware example
of this XNU tree driving keys, touch and a fuel gauge through SPMI/GENI.

`xnu/patches/` is what CI applies and what the liuqin kernel is built from;
nothing here is compiled. The liuqin drivers are **not ported yet** — see
`NOTES.md`, "Next steps".

| File | What it does on sweet | Why it is not liuqin's driver |
| --- | --- | --- |
| `sweet-keys-touch-drivers.patch` | `bsd/dev/sweet_keys.c` (power/volume through the PM6150 PON block over SPMI) and `bsd/dev/sweet_touch.c` (Goodix GT9896 over GENI I2C) | liuqin's PMIC is PM8350/PM8350B/PM8350C/PMK8350, and its touch controller is a Novatek NT36532 on GENI **SPI** |
| `sweet-touch-power-battery.patch` | RPMh regulator votes for the touch rail, an SPMI helper, `SweetBattery.cpp` | different RPMh resource IDs, SPMI USIDs, and a PM8350B fuel gauge |
| `sweet-goodix-geni-packing.patch` | GENI FIFO packing fix and the GT9896 report format | NT36532 has a different report format (see `tools/touch-nt36532/`) |

## What a liuqin port needs

The hardware facts come from `sm8475-xiaomi-liuqin.dts` and `pm8350.dtsi`;
the register programming has to be checked on the tablet.

* **Touch** — Novatek NT36532 on a QUPv3 SE in SPI mode, reset and IRQ on
  TLMM GPIOs, `vdd`/`iovdd` from PM8350C LDOs. Reuse the shape of
  `sweet_touch.c` (poll + IRQ, HID digitizer through
  `iokit/Kernel/arm/hid/LiuqinHID.cpp`, which is applied by patch 0058) but
  speak SPI, and feed it from `tools/touch-nt36532/nt36532.py`.
* **Keys** — PM8350 PON (`qcom,pm8350-pon`, PON GEN2 at peripheral 0x1000):
  KPDPWR/RESIN status. Same PON generation as PM6150, different SPMI USID.
* **Battery** — PM8350B `qcom,pm8350b-bms`; not worth porting before the
  fuel gauge itself works.
