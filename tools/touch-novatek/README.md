# Novatek NT36532 protocol prototype (from the NT36523 work)

**Status: reference only.** liuqin's panel controller is an NT36532
(dual-DSI, 2880x1800, 120 Hz); this directory holds the offline protocol
harness written for the NT36523 of the Xiaomi Pad 5
([iPadOS-nabu](https://github.com/mytiantian001/iPadOS-nabu)). The two parts
share the Novatek page/address framing and the trim-id readout, but the
report format is **not** guaranteed to be the same, so do not treat
`nt36523.c` as a liuqin decoder until it is checked against a bus dump from
the tablet.

Nothing here has XNU dependencies and nothing touches hardware.

## What is implemented

- Novatek page/address framing (`0xff`, `addr >> 15`, `addr >> 7`)
- SPI read/write framing (`addr & 0x7f`, `| 0x80`)
- trim-id match table for NT36523 (`... 0x23 0x65 0x03`)
- FWINFO parsing (39 bytes, resolution fallback)
- point-frame parser for 10 fingers (65-byte frame, 6 bytes per finger)
- host tests with a fake bus

## Run

```sh
make
./test_nt36523
```

## What liuqin needs before this is a driver

- the NT36532 trim-id and firmware info, read over SPI on the tablet
- GENI SPI (QUPv3 SE) init: clocks, DFS, pin mux, SMMU, DMA vs FIFO
- reset and IRQ GPIOs, `vdd`/`iovdd` rails (PM8350C LDOs)
- whether the stock firmware is already running after ABL, or a firmware
  image must be supplied
- HID coordinate mapping into the 1800x2880 framebuffer
  (`iokit/Kernel/arm/hid/LiuqinHID.cpp`, patch 0058)

Those belong in `bsd/dev/liuqin_touch.c` behind `liuqintouch=N`, and must be
verified on the device.
