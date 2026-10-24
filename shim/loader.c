/*
 * Milestone 4: act as iBoot for xnu.
 *
 * mkpayload.py already resolved the kernel's chained fixups and built the
 * ADT, so this only has to: find the payload in the ABL ramdisk, copy the
 * segments to phys_base + (vmaddr - virt_base), place the ADT and
 * boot_args behind the kernel, and jump to the entry point with the MMU
 * and caches off, x0 = boot_args (physical).
 */
#include <stdint.h>
#include "console.h"
#include "fdt.h"
#include "loader.h"

/* shim.c: shown on the blue panic screen, so a fault can be located */
void stage_set(const char *s);

#define BOOT_LINE_LENGTH	608
#define PAGE_16K		0x4000UL

struct payload_seg {
	uint64_t vmaddr, vmsize, file_off, file_size;
};

struct payload {
	char magic[8];
	uint32_t version, nsegs;
	uint64_t virt_base, entry_va, adt_off, adt_size, kernel_end;
	uint64_t phys_base, mem_size;
	char cmdline[BOOT_LINE_LENGTH];
	struct payload_seg segs[];
};

/* pexpert/pexpert/arm64/boot.h, Revision/Version 2 */
struct boot_video {
	uint64_t v_baseAddr, v_display, v_rowBytes, v_width, v_height, v_depth;
};

struct boot_args {
	uint16_t Revision, Version;
	uint64_t virtBase, physBase, memSize, topOfKernelData;
	struct boot_video Video;
	uint32_t machineType;
	uint64_t deviceTreeP;
	uint32_t deviceTreeLength;
	char CommandLine[BOOT_LINE_LENGTH];
	uint64_t bootFlags, memSizeActual;
};

_Static_assert(__builtin_offsetof(struct payload, segs) == 0x2a8, "payload header layout");
_Static_assert(__builtin_offsetof(struct boot_args, Video) == 40, "boot_args layout");
_Static_assert(__builtin_offsetof(struct boot_args, CommandLine) == 108, "boot_args layout");
_Static_assert(sizeof(struct boot_args) == 736, "boot_args layout");

/* MMU off: everything is Device memory, so copy in aligned 8-byte words. */
static void copy64(uintptr_t dst, uintptr_t src, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;
	volatile const uint64_t *s = (volatile const uint64_t *)src;
	for (uint64_t i = 0; i < (n + 7) / 8; i++)
		d[i] = s[i];
}

static void zero64(uintptr_t dst, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;
	for (uint64_t i = 0; i < n / 8; i++)
		d[i] = 0;
}

static int magic_ok(const char *m)
{
	static const char want[] = "LIUQIN01";
	for (int i = 0; i < 8; i++)
		if (m[i] != want[i])
			return 0;
	return 1;
}

/* Bounded substring search over the payload cmdline (no libc in the shim). */
static int has_word(const char *s, int n, const char *w)
{
	for (int i = 0; i < n && s[i]; i++) {
		int j = 0;
		while (w[j] && i + j < n && s[i + j] == w[j])
			j++;
		if (!w[j])
			return 1;
	}
	return 0;
}

typedef void (*xnu_entry_t)(uint64_t boot_args_pa);

/* Unrolled: a pointer table in .rodata would hold link-time addresses. */
static void print_bank(int i, uint64_t base, uint64_t size)
{
	if (i == 0) {
		con_hex("MEM0BASE", base); con_hex("MEM0SIZE", size);
	} else if (i == 1) {
		con_hex("MEM1BASE", base); con_hex("MEM1SIZE", size);
	} else if (i == 2) {
		con_hex("MEM2BASE", base); con_hex("MEM2SIZE", size);
	} else if (i == 3) {
		con_hex("MEM3BASE", base); con_hex("MEM3SIZE", size);
	} else if (i == 4) {
		con_hex("MEM4BASE", base); con_hex("MEM4SIZE", size);
	} else if (i == 5) {
		con_hex("MEM5BASE", base); con_hex("MEM5SIZE", size);
	} else if (i == 6) {
		con_hex("MEM6BASE", base); con_hex("MEM6SIZE", size);
	} else {
		con_hex("MEM7BASE", base); con_hex("MEM7SIZE", size);
	}
}

const char *loader_boot(uintptr_t dtb)
{
	uint64_t rd_start, rd_end;

	if (!fdt_chosen_u64(dtb, "linux,initrd-start", &rd_start) ||
	    !fdt_chosen_u64(dtb, "linux,initrd-end", &rd_end))
		return "no initrd in /chosen";
	con_hex2("INITRD", "lo=", rd_start, "hi=", rd_end);

	stage_set("ldr-dtb");
	/* No pointer tables here: the shim is linked at 0 but ABL loads it at an
	 * arbitrary physical address, so a table of string pointers in .rodata
	 * would carry link-time absolute addresses and fault on first use. */
	{
		char model[96];
		uint64_t banks[16];

		if (fdt_root_string(dtb, "model", model, sizeof(model)))
			con_str("MODEL", model);
		int nb = fdt_memory_banks(dtb, banks, 8);
		con_hex("MEMBANKS", nb);
		for (int i = 0; i < nb; i++)
			print_bank(i, banks[2 * i], banks[2 * i + 1]);
	}

	stage_set("ldr-payload");
	const struct payload *p = (const struct payload *)rd_start;
	if (!magic_ok(p->magic))
		return "no LIUQIN01 payload";
	if (p->version != 1 || (p->adt_off & 7) || (rd_start & 7))
		return "bad payload header";

	stage_set("ldr-phys");
	uint64_t pa_of_va_base = p->phys_base - p->virt_base;	/* pa = va + this */
	con_hex("PHYSBASE", p->phys_base);
	con_hex("PHYSSIZE", p->mem_size);
	con_hex("VIRT", p->virt_base);
	con_hex("ENTRY", p->entry_va);

	/* Diagnostic payloads stop here so the memory banks stay on screen. */
	if (has_word(p->cmdline, BOOT_LINE_LENGTH, "liuqin_diag=1")) {
		uint32_t w[16];
		char nm[24];
		int ac = 0, sc = 0, rl = 0;

		stage_set("diag-dump");
		if (fdt_memory_raw(dtb, nm, sizeof(nm), w, 16, &ac, &sc, &rl)) {
			con_str("MNAME", nm);
			con_hex2("MCELLS", "a=", ac, "s=", sc);
			con_hex("MREGLEN", rl);
			con_hex2("RAW0", "w0=", w[0], "w1=", w[1]);
			con_hex2("RAW1", "w2=", w[2], "w3=", w[3]);
			con_hex2("RAW2", "w4=", w[4], "w5=", w[5]);
			con_hex2("RAW3", "w6=", w[6], "w7=", w[7]);
			con_hex2("RAW4", "w8=", w[8], "w9=", w[9]);
			con_hex2("RAW5", "w10=", w[10], "w11=", w[11]);
			con_hex2("RAW6", "w12=", w[12], "w13=", w[13]);
			con_hex2("RAW7", "w14=", w[14], "w15=", w[15]);
		}
		stage_set("diag-halt");
		con_str("DIAG", "halt before segment copy; reset to leave");
		for (;;)
			__asm__ volatile("wfi");
	}

	/* The payload must not overlap where the kernel goes. */
	if (rd_end > p->phys_base && rd_start < p->phys_base + p->mem_size)
		return "ramdisk overlaps kernel memory";

	stage_set("ldr-copy");
	for (uint32_t i = 0; i < p->nsegs; i++) {
		const struct payload_seg *s = &p->segs[i];
		uintptr_t dst = s->vmaddr + pa_of_va_base;
		copy64(dst, rd_start + s->file_off, s->file_size);
		if (s->vmsize > s->file_size)
			zero64(dst + ((s->file_size + 7) & ~7UL), s->vmsize - ((s->file_size + 7) & ~7UL));
	}
	con_hex2("SEGS", "n=", p->nsegs, "end=", p->kernel_end + pa_of_va_base);

	/* ADT, then boot_args, behind the kernel; everything below topOfKernelData is ours. */
	stage_set("ldr-adt");
	uintptr_t adt_pa = p->kernel_end + pa_of_va_base;
	copy64(adt_pa, rd_start + p->adt_off, p->adt_size);
	uintptr_t ba_pa = (adt_pa + p->adt_size + PAGE_16K - 1) & ~(PAGE_16K - 1);
	uintptr_t top = ba_pa + PAGE_16K;

	stage_set("ldr-args");
	zero64(ba_pa, PAGE_16K);
	volatile struct boot_args *ba = (volatile struct boot_args *)ba_pa;
	ba->Revision = 2;
	ba->Version = 2;
	ba->virtBase = p->virt_base;
	ba->physBase = p->phys_base;
	ba->memSize = p->mem_size;
	ba->topOfKernelData = top;
	ba->Video.v_baseAddr = FB_BASE;
	ba->Video.v_display = 1;
	ba->Video.v_rowBytes = FB_STRIDE;
	ba->Video.v_width = FB_W;
	ba->Video.v_height = FB_H;
	ba->Video.v_depth = 32;
	ba->deviceTreeP = adt_pa - pa_of_va_base;	/* xnu reads it with the MMU on: a VA */
	ba->deviceTreeLength = (uint32_t)p->adt_size;
	for (int i = 0; i < BOOT_LINE_LENGTH; i++)
		ba->CommandLine[i] = p->cmdline[i];
	ba->memSizeActual = p->mem_size;
	con_hex2("BOOTARG", "pa=", ba_pa, "top=", top);

	stage_set("xnu");
	uintptr_t entry_pa = p->entry_va + pa_of_va_base;
	con_hex("JUMP", entry_pa);
	con_str("XNU", "jumping (fbcon clears screen)");

	/* Everything was written with the MMU and caches off; drop any stale I-cache lines too. */
	__asm__ volatile("dsb sy; ic iallu; dsb sy; isb" ::: "memory");
	((xnu_entry_t)entry_pa)(ba_pa);
	return "xnu returned";
}
