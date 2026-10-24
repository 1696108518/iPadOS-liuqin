// SPDX-License-Identifier: BSD-2-Clause-Patent
#include "nt36523.h"

#include <string.h>

static int
nvt_xfer(const struct nvt_bus *bus, const uint8_t *tx, uint8_t *rx, size_t len)
{
	if (bus == NULL || bus->xfer == NULL || len == 0) {
		return -1;
	}
	return bus->xfer(bus->ctx, tx, rx, len) == 0 ? 0 : -1;
}

int
nvt_set_page(const struct nvt_bus *bus, uint32_t addr)
{
	uint8_t tx[3] = { 0xff, (uint8_t)(addr >> 15), (uint8_t)(addr >> 7) };
	return nvt_xfer(bus, tx, NULL, sizeof(tx));
}

int
nvt_write_addr(const struct nvt_bus *bus, uint32_t addr, const uint8_t *data, size_t len)
{
	if (data == NULL || len == 0 || len > 256) {
		return -1;
	}
	if (nvt_set_page(bus, addr) != 0) {
		return -1;
	}
	uint8_t tx[257];
	tx[0] = (uint8_t)((addr & 0x7f) | 0x80);
	memcpy(tx + 1, data, len);
	return nvt_xfer(bus, tx, NULL, len + 1);
}

int
nvt_read_addr(const struct nvt_bus *bus, uint32_t addr, uint8_t *out, size_t len)
{
	if (out == NULL || len == 0 || len > 8192) {
		return -1;
	}
	if (nvt_set_page(bus, addr) != 0) {
		return -1;
	}
	uint8_t tx[8194];
	uint8_t rx[8194];
	memset(tx, 0, len + 2);
	tx[0] = (uint8_t)(addr & 0x7f);
	if (nvt_xfer(bus, tx, rx, len + 2) != 0) {
		return -1;
	}
	memcpy(out, rx + 2, len);
	return 0;
}

int
nvt_read_trim_id(const struct nvt_bus *bus, uint32_t addr, uint8_t id[6])
{
	return nvt_read_addr(bus, addr, id, 6);
}

int
nvt_read_point_frame(const struct nvt_bus *bus, uint8_t frame[NVT_POINT_DATA_LEN])
{
	if (frame == NULL || nvt_set_page(bus, NVT_EVENT_BUF_ADDR) != 0) {
		return -1;
	}
	uint8_t tx[NVT_POINT_DATA_LEN + 2];
	uint8_t rx[NVT_POINT_DATA_LEN + 2];
	memset(tx, 0, sizeof(tx));
	if (nvt_xfer(bus, tx, rx, sizeof(tx)) != 0) {
		return -1;
	}
	memcpy(frame, rx + 2, NVT_POINT_DATA_LEN);
	return 0;
}

int
nvt_parse_points(const uint8_t frame[NVT_POINT_DATA_LEN], uint16_t max_x,
    uint16_t max_y, struct nvt_contact *out, int max)
{
	if (frame == NULL || out == NULL || max <= 0) {
		return -1;
	}
	if (max_x == 0) {
		max_x = NVT_DEFAULT_MAX_X;
	}
	if (max_y == 0) {
		max_y = NVT_DEFAULT_MAX_Y;
	}

	int count = 0;
	for (int i = 0; i < NVT_MAX_FINGERS && count < max; i++) {
		const uint8_t *p = frame + 6 * i;
		unsigned raw = p[0];
		unsigned id = raw >> 3;
		unsigned event = raw & 7u;

		if (id == 0 || id > NVT_MAX_FINGERS) {
			continue;
		}
		/* 1 = enter, 2 = moving.  0/3+ are releases or invalid. */
		if (event != 1 && event != 2) {
			continue;
		}

		unsigned x = ((unsigned)p[1] << 4) | (p[3] >> 4);
		unsigned y = ((unsigned)p[2] << 4) | (p[3] & 0x0fu);
		if (x > max_x || y > max_y) {
			continue;
		}

		unsigned weight = p[4] ? p[4] : 1;
		unsigned pressure = p[5];
		if (i < 2) {
			/* The vendor packs the high pressure bits of the first two
			 * fingers in point_data[63] and [64]; frame[] starts at
			 * point_data[1], so those are frame[62] and frame[63]. */
			pressure |= (unsigned)frame[62 + i] << 8;
		}
		if (pressure == 0) {
			pressure = 1;
		}

		out[count].id = (int)id;
		out[count].down = 1;
		out[count].x = (int)x;
		out[count].y = (int)y;
		out[count].weight = (int)weight;
		out[count].pressure = (int)pressure;
		count++;
	}
	return count;
}

int
nvt_parse_fwinfo(const uint8_t *data, size_t len, struct nvt_info *info)
{
	if (data == NULL || info == NULL || len < 39) {
		return -1;
	}
	memset(info, 0, sizeof(*info));

	uint8_t fw_ver = data[0];
	uint8_t fw_inv = data[1];
	uint16_t max_x = ((uint16_t)data[4] << 8) | data[5];
	uint16_t max_y = ((uint16_t)data[6] << 8) | data[7];

	info->fw_ver = fw_ver;
	info->fw_type = data[13];
	info->x_num = data[2];
	info->y_num = data[3];
	info->max_buttons = data[10];
	info->cascade = data[33] & 1;

	int good = ((unsigned)fw_ver + fw_inv) == 0xffu &&
	    max_x > 0 && max_y > 0 && max_x <= 4096 && max_y <= 4096;
	if (!good) {
		info->max_x = NVT_DEFAULT_MAX_X;
		info->max_y = NVT_DEFAULT_MAX_Y;
		info->valid = 0;
		return 0;
	}
	info->max_x = max_x;
	info->max_y = max_y;
	info->valid = 1;
	return 0;
}

int
nvt_match_nt36523(const uint8_t id[6])
{
	static const struct {
		uint8_t id[6];
		uint8_t mask[6];
	} table[] = {
		{ { 0x20, 0xff, 0xff, 0x23, 0x65, 0x03 }, { 1, 0, 0, 1, 1, 1 } },
		{ { 0x0c, 0xff, 0xff, 0x23, 0x65, 0x03 }, { 1, 0, 0, 1, 1, 1 } },
		{ { 0x0b, 0xff, 0xff, 0x23, 0x65, 0x03 }, { 1, 0, 0, 1, 1, 1 } },
		{ { 0x0a, 0xff, 0xff, 0x23, 0x65, 0x03 }, { 1, 0, 0, 1, 1, 1 } },
		{ { 0xff, 0xff, 0xff, 0x23, 0x65, 0x03 }, { 0, 0, 0, 1, 1, 1 } },
	};

	if (id == NULL) {
		return 0;
	}
	for (size_t t = 0; t < sizeof(table) / sizeof(table[0]); t++) {
		int match = 1;
		for (size_t i = 0; i < 6; i++) {
			if (table[t].mask[i] && id[i] != table[t].id[i]) {
				match = 0;
				break;
			}
		}
		if (match) {
			return 1;
		}
	}
	return 0;
}
