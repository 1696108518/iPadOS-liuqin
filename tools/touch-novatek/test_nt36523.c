// SPDX-License-Identifier: BSD-2-Clause-Patent
#include "nt36523.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static void
put_finger(uint8_t *frame, int slot, unsigned id, unsigned event,
    unsigned x, unsigned y, unsigned weight, unsigned pressure)
{
	uint8_t *p = frame + 6 * slot;
	p[0] = (uint8_t)((id << 3) | event);
	p[1] = (uint8_t)(x >> 4);
	p[2] = (uint8_t)(y >> 4);
	p[3] = (uint8_t)(((x & 0x0f) << 4) | (y & 0x0f));
	p[4] = (uint8_t)weight;
	p[5] = (uint8_t)pressure;
}

static void
test_parse_two_fingers(void)
{
	uint8_t frame[NVT_POINT_DATA_LEN];
	memset(frame, 0, sizeof(frame));
	put_finger(frame, 0, 1, 1, 1000, 2000, 7, 0x34);
	put_finger(frame, 1, 2, 2, 200, 300, 9, 0x56);
	frame[62] = 0x01; /* high pressure bits for finger 0 */
	frame[63] = 0x02; /* high pressure bits for finger 1 */

	struct nvt_contact c[4];
	int n = nvt_parse_points(frame, 1600, 2560, c, 4);
	assert(n == 2);
	assert(c[0].id == 1 && c[0].x == 1000 && c[0].y == 2000);
	assert(c[0].weight == 7);
	assert(c[0].pressure == 0x0134);
	assert(c[1].id == 2 && c[1].x == 200 && c[1].y == 300);
	assert(c[1].weight == 9);
	assert(c[1].pressure == 0x0256);
	puts("parse_two_fingers: ok");
}

static void
test_parse_filters(void)
{
	uint8_t frame[NVT_POINT_DATA_LEN];
	memset(frame, 0, sizeof(frame));
	put_finger(frame, 0, 0, 1, 100, 100, 1, 1);      /* id 0 */
	put_finger(frame, 1, 3, 0, 100, 100, 1, 1);      /* release */
	put_finger(frame, 2, 4, 1, 1601, 100, 1, 1);     /* x out of range */
	put_finger(frame, 3, 5, 1, 10, 2561, 1, 1);      /* y out of range */

	struct nvt_contact c[4];
	int n = nvt_parse_points(frame, 1600, 2560, c, 4);
	assert(n == 0);
	puts("parse_filters: ok");
}

static void
test_fwinfo(void)
{
	uint8_t info[39];
	memset(info, 0, sizeof(info));
	info[0] = 0x12;
	info[1] = 0xed;
	info[2] = 18;
	info[3] = 32;
	info[4] = 1600 >> 8;
	info[5] = 1600 & 0xff;
	info[6] = 2560 >> 8;
	info[7] = 2560 & 0xff;
	info[10] = 2;
	info[13] = 0x55;
	info[33] = 1;

	struct nvt_info out;
	assert(nvt_parse_fwinfo(info, sizeof(info), &out) == 0);
	assert(out.valid == 1);
	assert(out.fw_ver == 0x12 && out.fw_type == 0x55);
	assert(out.max_x == 1600 && out.max_y == 2560);
	assert(out.x_num == 18 && out.y_num == 32);
	assert(out.max_buttons == 2 && out.cascade == 1);
	puts("fwinfo: ok");
}

static void
test_fwinfo_broken(void)
{
	uint8_t info[39];
	memset(info, 0, sizeof(info));
	info[0] = 0x12;
	info[1] = 0x00; /* bad complement */
	info[4] = 0x06;
	info[5] = 0x40;
	info[6] = 0x0a;
	info[7] = 0x00;

	struct nvt_info out;
	assert(nvt_parse_fwinfo(info, sizeof(info), &out) == 0);
	assert(out.valid == 0);
	assert(out.max_x == NVT_DEFAULT_MAX_X);
	assert(out.max_y == NVT_DEFAULT_MAX_Y);
	puts("fwinfo_broken: ok");
}

static void
test_trim(void)
{
	static const uint8_t good[][6] = {
		{ 0x20, 0x00, 0x00, 0x23, 0x65, 0x03 },
		{ 0x0c, 0x00, 0x00, 0x23, 0x65, 0x03 },
		{ 0x0b, 0x00, 0x00, 0x23, 0x65, 0x03 },
		{ 0x0a, 0x00, 0x00, 0x23, 0x65, 0x03 },
		{ 0xff, 0x00, 0x00, 0x23, 0x65, 0x03 },
	};
	for (size_t i = 0; i < sizeof(good) / sizeof(good[0]); i++) {
		assert(nvt_match_nt36523(good[i]) == 1);
	}
	const uint8_t bad[6] = { 0x20, 0x00, 0x00, 0x32, 0x65, 0x03 };
	assert(nvt_match_nt36523(bad) == 0);
	puts("trim: ok");
}

struct fake_bus {
	uint32_t page;
	uint8_t data[0x40000];
	int xfers;
	uint8_t last_tx[64];
	size_t last_len;
};

static int
fake_xfer(void *ctx, const uint8_t *tx, uint8_t *rx, size_t len)
{
	struct fake_bus *f = ctx;
	f->xfers++;
	f->last_len = len < sizeof(f->last_tx) ? len : sizeof(f->last_tx);
	memcpy(f->last_tx, tx, f->last_len);

	if (len == 3 && tx[0] == 0xff) {
		f->page = ((uint32_t)tx[1] << 15) | ((uint32_t)tx[2] << 7);
		return 0;
	}
	if (rx != NULL && len >= 2) {
		uint32_t at = f->page | (tx[0] & 0x7f);
		memset(rx, 0, len);
		for (size_t i = 0; i + 2 < len; i++) {
			rx[i + 2] = f->data[(at + i) & (sizeof(f->data) - 1)];
		}
		return 0;
	}
	return 0;
}

static void
test_bus_framing(void)
{
	struct fake_bus f;
	memset(&f, 0, sizeof(f));
	for (unsigned i = 0; i < 39; i++) {
		f.data[NVT_EVENT_BUF_FWINFO + i] = (uint8_t)(0x80 + i);
	}
	for (unsigned i = 0; i < NVT_POINT_DATA_LEN; i++) {
		f.data[NVT_EVENT_BUF_ADDR + i] = (uint8_t)(0x40 + i);
	}
	struct nvt_bus bus = { .ctx = &f, .xfer = fake_xfer, .delay_ms = NULL };
	uint8_t out[39];
	assert(nvt_read_addr(&bus, NVT_EVENT_BUF_FWINFO, out, sizeof(out)) == 0);
	assert(f.xfers == 2);
	assert(f.last_tx[0] == (NVT_EVENT_BUF_FWINFO & 0x7f));
	assert(out[0] == 0x80 && out[38] == 0x80 + 38);

	uint8_t frame[NVT_POINT_DATA_LEN];
	assert(nvt_read_point_frame(&bus, frame) == 0);
	assert(frame[0] == 0x40 && frame[NVT_POINT_DATA_LEN - 1] == 0x40 + NVT_POINT_DATA_LEN - 1);
	puts("bus_framing: ok");
}

int
main(void)
{
	test_parse_two_fingers();
	test_parse_filters();
	test_fwinfo();
	test_fwinfo_broken();
	test_trim();
	test_bus_framing();
	puts("all nt36523 tests passed");
	return 0;
}
