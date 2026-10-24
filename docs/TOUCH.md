# liuqin touch and buttons: what the driver needs

Hardware facts below come from the mainline device tree this port's owner points
at - `yzddmr6/linux-sm8450-liuqin`, branch `liuqin-6.17`, files
`arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts` and `sm8450.dtsi` - which in
turn cites the vendor bootloader's captured FDT (`abl-final-fdt.dts`).  The
`xiaomipad-6pro-mainline` project (`kernel/source.json`) pins exactly that tree.

## The touchscreen

| Fact | Value | Source |
| --- | --- | --- |
| Controller | Novatek **NT36532** (TDDI, shares the panel die) | `novatek,NVT-ts-spi` node |
| Bus | GENI SPI, **spi4** = SE 4 of QUPv3 wrapper 0, `spi@990000`, 0x4000 | `sm8450.dtsi:1505` |
| Chip select | 0 | touch node `reg = <0>` |
| SPI clock | 9.6 MHz max | `spi-max-frequency` |
| Interrupt | **TLMM gpio 21**, edge rising | `novatek,irq-gpio` |
| Reset / swrst | software reset register `0x1fb43e` | `novatek,swrst-n8-addr` |
| Firmware | `novatek/liuqin/novatek_nt36532_m81_fw_csot.bin`, RAM-loaded | `firmware-name` |
| Rails | none in the DT; the panel's rails feed the die | comment in the node |
| Pins | data/clk `gpio16 gpio17 gpio18`, cs `gpio19`, function `qup4` | `qup_spi4_data_clk`, `qup_spi4_cs` |
| GIC | SPI 605 (level high) | `sm8450.dtsi` spi4 `interrupts` |

The DT comment records an important ordering fact: the NT36532 is a TDDI die,
so the DRM panel's prepare sequence resets it and discards the RAM-loaded
firmware; a driver must download the firmware again after every panel prepare.
On liuqin there is no panel driver - the display is the bootloader's
framebuffer, presented by our own IOMFB - so the die is not reset by us and
whatever ABL loaded may still be running.

## SPI4 pin mux (TLMM, `0x0f100000`)

`gpio16`..`gpio19` must take function `qup4`; the CS pin gets drive strength 6
and no bias, the others the DT defaults.  The generic msm pinctrl layout is
`TLMM_GPIO_CFG(pin) = 0x1000 * pin` (mux in bits 2:0, drive in 8:6, pull in
5:4), plus the interrupt config register for gpio21.

## SE4 clocks (GCC, `0x100000`)

From `drivers/clk/qcom/gcc-sm8450.c`:

| Clock | Register | Field |
| --- | --- | --- |
| `gcc_qupv3_wrap0_s4_clk` (branch) | enable `0x62008` | BIT(14); halt `0x274dc`, BRANCH_HALT_VOTED |
| `gcc_qupv3_wrap0_s4_clk_src` (RCG) | cmd_rcgr `0x274e4` | m/n/2d off `0x274e4`, hid 5 bits, parent GPLL0 even |
| nominal rate | 7.3728 MHz or 19.2 MHz (BI_TCXO) | `ftbl_gcc_qupv3_wrap0_s0_clk_src` |

The QUPv3 wrapper itself also needs its core and AHB clocks
(`gcc_qupv3_wrap0_core_2x_clk`, `gcc_qupv3_wrap0_core_clk`,
`gcc_qupv3_wrap_0_ahb_clk`); the driver must enable those before any SE
register access or the bus hangs.

## GENI SPI programming

Reference: `drivers/spi/spi-geni-qcom.c`, `include/linux/soc/qcom/geni-se.h`,
`drivers/soc/qcom/qcom-geni-se.c` (all in the same kernel tree). The parts a
polled, FIFO-only driver needs:

1. SE reset deassert and GENI core init (`SE_GENI_CONFIG`, `SE_GENI_CLK_CTRL`,
   `SE_GENI_CFG_REG` packing of the protocol and FIFO sizes);
2. SPI master config: loopback off, `SE_SPI_LOOPBACK`, `SE_SPI_SPI_MODE`,
   `SE_SPI_CS_CLK_DELAY`/`SE_SPI_CS_TOGGLE` for CS0, 8-bit words;
3. FIFO transfers: write the command (`SE_GENI_M_CMD0` with the opcode,
   `SE_GENI_M_CMD_CTRL`), push/pull 32-bit words through
   `SE_GENI_M_CMD0`/`SE_GENI_M_CMD1`... actually `SE_GENI_TX_FIFOn` and
   `SE_GENI_RX_FIFOn`, wait on `SE_GENI_M_IRQ_STATUS`/`SE_GENI_S_IRQ_STATUS`,
   and read the command's result from `SE_GENI_M_CMD0`;
4. the transfer is prepared with the `SPI_M_TRANSFER` word chain that carries the
   packet count and the `SPI_M_GO`/`SPI_M_CS_ASSERT` bits.

Because the DT leaves the bus at the default speed and the controller is not
performance-critical here, the driver can run at 1-2 MHz, polled, without DMA.

## Novatek protocol

`tools/touch-novatek/` already has the page/address framing and a 10-finger,
65-byte frame parser written for the NT36523 of the Pad 5.  The NT36532 on this
tablet shares the framing (`0xff` page prefix, `addr >> 15`, `addr >> 7`) and the
trim-id readout but the report format must be checked against a bus dump from
this unit before being trusted; `README.md` there says the same.

The init sequence a driver needs, from the mainline `novatek-nvt-ts` driver in
this same kernel tree:

1. read the trim id / chip id and match it against the table;
2. software reset through `0x1fb43e`;
3. read `FWINFO` (39 bytes) for the resolution, then the report format
   (10 fingers x 6 bytes) for the point frames;
4. enable the pen path only if the firmware is loaded.

## Buttons

Power and volume are on the PMIC (SPMI), not GPIO: power = PM8350 PON,
volume up/down on the PMIC's GPIOs.  A minimal driver needs the SPMI controller
(`spmi@...`), the PMIC revision registers, and the PON block; the HID side is
already in place (`liuqin_hid_key(0/1/2, down)` in
`iokit/Kernel/arm/hid/LiuqinHID.cpp`).

## Plan

1. **Read-only audit** (`bsd/dev/liuqin_touch.c`, `liuqintouch=1`): map SPI4,
   TLMM and GCC, log the SE4 branch/RCG state, the QUP core clock state and the
   pin mux of gpio16..19/21.  This tells us whether ABL already clocked the SE
   and muxed the pins, and validates the base addresses before any write.
2. **Chip-id read**: enable the missing clocks, mux the pins, program the SE as a
   polled SPI master at ~1 MHz and read the Novatek trim id.  One log line is
   enough to prove the bus.
3. **Reports**: parse point frames from the same FIFO path and call
   `liuqin_hid_touch(finger, down, x, y)`; map the 1800x2880 panel coordinates
   (the lock screen is drawn at 1800x2880 with scale 2).
4. **Buttons** after that, over SPMI.
