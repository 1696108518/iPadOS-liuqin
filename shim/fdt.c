/*
 * Just enough flattened-device-tree parsing for the shim:
 *   - /chosen initrd location (what ABL loaded our payload at)
 *   - the root model string and /memory banks, which ABL fills in for real
 *     hardware even though the DTB shipped in vendor_boot has zeros there.
 * Big-endian, 4-byte aligned tokens.
 */
#include "fdt.h"

#define FDT_MAGIC	0xd00dfeed
#define FDT_BEGIN_NODE	1
#define FDT_END_NODE	2
#define FDT_PROP	3
#define FDT_NOP		4
#define FDT_END		9

static uint32_t be32(uintptr_t a)
{
	return __builtin_bswap32(*(volatile uint32_t *)a);
}

static int streq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

static uintptr_t align4(uintptr_t a)
{
	return (a + 3) & ~3UL;
}

static uintptr_t skip_str(uintptr_t p)
{
	/* byte loads are fine on Device memory as long as they are aligned to 1 */
	while (*(volatile char *)p)
		p++;
	return p + 1;
}

int fdt_chosen_u64(uintptr_t fdt, const char *prop, uint64_t *out)
{
	if (!fdt || be32(fdt) != FDT_MAGIC)
		return 0;
	uintptr_t p = fdt + be32(fdt + 8);		/* off_dt_struct */
	uintptr_t strings = fdt + be32(fdt + 12);	/* off_dt_strings */
	int depth = 0, in_chosen = 0;

	for (;;) {
		uint32_t tok = be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE: {
			const char *name = (const char *)p;
			depth++;
			in_chosen = depth == 2 && streq(name, "chosen");
			p = align4(skip_str(p));
			break;
		}
		case FDT_END_NODE:
			if (in_chosen)
				return 0;
			depth--;
			break;
		case FDT_PROP: {
			uint32_t len = be32(p), nameoff = be32(p + 4);
			uintptr_t val = p + 8;
			if (in_chosen && streq((const char *)(strings + nameoff), prop)) {
				if (len == 4)
					*out = be32(val);
				else if (len == 8)
					*out = (uint64_t)be32(val) << 32 | be32(val + 4);
				else
					return 0;
				return 1;
			}
			p = align4(val + len);
			break;
		}
		case FDT_NOP:
			break;
		default:	/* FDT_END or garbage */
			return 0;
		}
	}
}


/* "memory" or "memory@<addr>", but not "memory-map" / "reserved-memory" */
static int is_memory_node(const char *name)
{
	if (name[0] != 'm' || name[1] != 'e' || name[2] != 'm' ||
	    name[3] != 'o' || name[4] != 'r' || name[5] != 'y')
		return 0;
	return name[6] == '\0' || name[6] == '@';
}

/* First string of a root property (model, compatible). Returns 0 if absent. */
int fdt_root_string(uintptr_t fdt, const char *prop, char *buf, int cap)
{
	if (!fdt || be32(fdt) != FDT_MAGIC)
		return 0;
	uintptr_t p = fdt + be32(fdt + 8);
	uintptr_t strings = fdt + be32(fdt + 12);
	int depth = 0, found = 0;

	for (;;) {
		uint32_t tok = be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE:
			depth++;
			p = align4(skip_str(p));
			break;
		case FDT_END_NODE:
			if (--depth <= 0)
				return found;
			break;
		case FDT_PROP: {
			uint32_t len = be32(p), nameoff = be32(p + 4);
			uintptr_t val = p + 8;
			if (depth == 1 && !found &&
			    streq((const char *)(strings + nameoff), prop)) {
				int n = 0;
				while (n < cap - 1 && n < (int)len &&
				       *(volatile char *)(val + n)) {
					buf[n] = *(volatile char *)(val + n);
					n++;
				}
				buf[n] = 0;
				found = 1;
			}
			p = align4(val + len);
			break;
		}
		case FDT_NOP:
			break;
		default:
			return found;
		}
	}
}

/*
 * Collect the /memory reg banks as base,size,base,size... and return how many.
 * The DTB shipped in vendor_boot has reg = <0 0>, ABL fills in the real map
 * before it jumps to a boot image, so this is the only way to learn the RAM
 * that this specific tablet has.
 */
int fdt_memory_banks(uintptr_t fdt, uint64_t *out, int max)
{
	if (!fdt || be32(fdt) != FDT_MAGIC)
		return 0;
	uintptr_t p = fdt + be32(fdt + 8);
	uintptr_t strings = fdt + be32(fdt + 12);
	int depth = 0, addr_cells = 2, size_cells = 2, n = 0;
	int mem_depth = 0;		/* depth of the memory node being read */
	uint32_t reg_len = 0;
	uintptr_t reg_val = 0;

	for (;;) {
		uint32_t tok = be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE: {
			const char *name = (const char *)p;
			depth++;
			p = align4(skip_str(p));
			if (depth == 2 && !mem_depth && is_memory_node(name))
				mem_depth = depth;
			break;
		}
		case FDT_END_NODE:
			if (mem_depth && depth == mem_depth) {
				uint32_t cells = addr_cells + size_cells;
				if (reg_len && reg_val && cells) {
					uint32_t banks = reg_len / (4 * cells);
					for (uint32_t b = 0; b < banks && n + 2 <= max * 2; b++) {
						uint64_t base = 0, size = 0;
						uintptr_t v = reg_val + b * 4 * cells;
						for (int i = 0; i < addr_cells; i++, v += 4)
							base = base << 32 | be32(v);
						for (int i = 0; i < size_cells; i++, v += 4)
							size = size << 32 | be32(v);
						if (base || size) {
							out[n++] = base;
							out[n++] = size;
						}
					}
				}
				reg_len = 0;
				reg_val = 0;
				mem_depth = 0;
			}
			depth--;
			break;
		case FDT_PROP: {
			uint32_t len = be32(p), nameoff = be32(p + 4);
			const char *pname = (const char *)(strings + nameoff);
			uintptr_t val = p + 8;
			if (depth == 1) {
				if (streq(pname, "#address-cells") && len == 4)
					addr_cells = (int)be32(val);
				else if (streq(pname, "#size-cells") && len == 4)
					size_cells = (int)be32(val);
			} else if (mem_depth && depth == mem_depth &&
				   streq(pname, "reg")) {
				reg_len = len;
				reg_val = val;
			}
			p = align4(val + len);
			break;
		}
		case FDT_NOP:
			break;
		default:
			return n / 2;
		}
	}
}


/*
 * Diagnostic: first /memory node's name, reg length and raw big-endian words.
 * Used to check the cell layout of the tree ABL actually passes.
 */
int fdt_memory_raw(uintptr_t fdt, char *name, int namecap, uint32_t *words,
                   int maxwords, int *addr_cells, int *size_cells, int *reglen)
{
	if (!fdt || be32(fdt) != FDT_MAGIC)
		return 0;
	uintptr_t p = fdt + be32(fdt + 8);
	uintptr_t strings = fdt + be32(fdt + 12);
	int depth = 0, ac = 2, sc = 2, mem_depth = 0, found = 0;
	uint32_t len = 0;
	uintptr_t val = 0;

	*addr_cells = 0;
	*size_cells = 0;
	*reglen = 0;
	name[0] = 0;

	for (;;) {
		uint32_t tok = be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE: {
			const char *n = (const char *)p;
			depth++;
			p = align4(skip_str(p));
			if (depth == 2 && !found && is_memory_node(n)) {
				int i = 0;
				while (i < namecap - 1 && n[i]) {
					name[i] = n[i];
					i++;
				}
				name[i] = 0;
				mem_depth = depth;
			}
			break;
		}
		case FDT_END_NODE:
			if (mem_depth && depth == mem_depth) {
				*addr_cells = ac;
				*size_cells = sc;
				*reglen = (int)len;
				for (int i = 0; i < maxwords && i * 4 < (int)len; i++)
					words[i] = be32(val + 4 * i);
				for (int i = (int)(len / 4); i < maxwords; i++)
					words[i] = 0;
				found = 1;
				mem_depth = 0;	/* later nodes must not overwrite this */
			}
			depth--;
			break;
		case FDT_PROP: {
			uint32_t l = be32(p), nameoff = be32(p + 4);
			const char *pn = (const char *)(strings + nameoff);
			uintptr_t v = p + 8;
			if (depth == 1) {
				if (streq(pn, "#address-cells") && l == 4)
					ac = (int)be32(v);
				else if (streq(pn, "#size-cells") && l == 4)
					sc = (int)be32(v);
			} else if (mem_depth && depth == mem_depth && streq(pn, "reg")) {
				len = l;
				val = v;
			}
			p = align4(v + l);
			break;
		}
		case FDT_NOP:
			break;
		default:
			return found;
		}
	}
}
