#pragma once
#include <stdint.h>

/* Read a u32/u64 (cell-size agnostic) property of /chosen. Returns 0 if absent. */
int fdt_chosen_u64(uintptr_t fdt, const char *prop, uint64_t *out);

/* Root "model" string, e.g. "Liuqin based on Qualcomm Technologies, Inc SM8475".
 * Returns 0 if the DTB is missing or the property is absent. */
int fdt_root_string(uintptr_t fdt, const char *prop, char *buf, int cap);

/* /memory banks as base,size pairs; returns the bank count (0 if unknown). */
int fdt_memory_banks(uintptr_t fdt, uint64_t *out, int max);

/* Diagnostic dump of the first /memory node: name, cells, reg length and raw
 * 32-bit words. Returns 0 if no memory node was found. */
int fdt_memory_raw(uintptr_t fdt, char *name, int namecap, uint32_t *words,
                   int maxwords, int *addr_cells, int *size_cells, int *reglen);
