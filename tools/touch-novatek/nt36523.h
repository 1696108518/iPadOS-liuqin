/*
 * Novatek NT36523 protocol helpers for liuqin.
 *
 * Clean-room implementation from the protocol facts used by the liuqin vendor
 * driver (nt36xxx.c / nt36xxx_mem_map.h).  No kernel dependencies; the bus is
 * supplied by the caller so the same code can run in host tests and in XNU.
 *
 * SPDX-License-Identifier: BSD-2-Clause-Patent
 */
#ifndef LIUQIN_NT36523_H
#define LIUQIN_NT36523_H

#include <stddef.h>
#include <stdint.h>

#define NVT_POINT_DATA_LEN   65
#define NVT_MAX_FINGERS      10
#define NVT_DEFAULT_MAX_X    1600
#define NVT_DEFAULT_MAX_Y    2560

#define NVT_CHIP_VER_TRIM_ADDR      0x3F004u
#define NVT_CHIP_VER_TRIM_OLD_ADDR  0x1F64Eu
#define NVT_EVENT_BUF_ADDR          0x2FE00u
#define NVT_EVENT_FWINFO            0x78u
#define NVT_EVENT_BUF_FWINFO        (NVT_EVENT_BUF_ADDR | NVT_EVENT_FWINFO)

/* Full-duplex SPI transfer, chip select held for the whole transfer. */
struct nvt_bus {
	void *ctx;
	int (*xfer)(void *ctx, const uint8_t *tx, uint8_t *rx, size_t len);
	void (*delay_ms)(void *ctx, unsigned ms);
};

struct nvt_contact {
	int id;          /* 1..NVT_MAX_FINGERS */
	int down;        /* 1 = down/moving, 0 = release */
	int x;
	int y;
	int weight;      /* touch major */
	int pressure;
};

struct nvt_info {
	uint8_t fw_ver;
	uint8_t fw_type;
	uint8_t x_num;
	uint8_t y_num;
	uint16_t max_x;
	uint16_t max_y;
	uint8_t max_buttons;
	int cascade;
	int valid;       /* fw info consistency check passed */
};

/* Page/address access, matching the vendor SPI framing. */
int nvt_set_page(const struct nvt_bus *bus, uint32_t addr);
int nvt_read_addr(const struct nvt_bus *bus, uint32_t addr, uint8_t *out, size_t len);
int nvt_write_addr(const struct nvt_bus *bus, uint32_t addr, const uint8_t *data, size_t len);
int nvt_read_trim_id(const struct nvt_bus *bus, uint32_t addr, uint8_t id[6]);

/* Read one 65-byte point frame from the event buffer. */
int nvt_read_point_frame(const struct nvt_bus *bus, uint8_t frame[NVT_POINT_DATA_LEN]);

/*
 * Parse a point frame into contacts.  Only id/event combinations 1 and 2 are
 * reported as down; missing ids are the caller's release responsibility.
 * Returns the number of contacts, or -1 on invalid arguments.
 */
int nvt_parse_points(const uint8_t frame[NVT_POINT_DATA_LEN],
    uint16_t max_x, uint16_t max_y, struct nvt_contact *out, int max);

/* Parse the 39-byte FWINFO block.  Falls back to 1600x2560 on broken data. */
int nvt_parse_fwinfo(const uint8_t *data, size_t len, struct nvt_info *info);

/* Match a 6-byte trim id against the NT36523 table. */
int nvt_match_nt36523(const uint8_t id[6]);

#endif /* LIUQIN_NT36523_H */
