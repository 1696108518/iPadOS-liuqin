/*
 * The handful of C library routines the shim uses, so the image links
 * freestanding on any cross toolchain. GCC used to inline strcmp() against
 * the constant partition name; clang does not, and -nostdlib leaves no libc.
 */
#include <stddef.h>
#include <stdint.h>

void *
memcpy(void *dst, const void *src, size_t n)
{
	uint8_t *d = dst;
	const uint8_t *s = src;
	while (n--) {
		*d++ = *s++;
	}
	return dst;
}

void *
memmove(void *dst, const void *src, size_t n)
{
	uint8_t *d = dst;
	const uint8_t *s = src;
	if (d < s) {
		while (n--) {
			*d++ = *s++;
		}
	} else {
		while (n--) {
			d[n] = s[n];
		}
	}
	return dst;
}

void *
memset(void *dst, int c, size_t n)
{
	uint8_t *d = dst;
	while (n--) {
		*d++ = (uint8_t)c;
	}
	return dst;
}

int
memcmp(const void *a, const void *b, size_t n)
{
	const uint8_t *x = a, *y = b;
	for (; n; n--, x++, y++) {
		if (*x != *y) {
			return *x - *y;
		}
	}
	return 0;
}

int
strcmp(const char *a, const char *b)
{
	while (*a && *a == *b) {
		a++;
		b++;
	}
	return (unsigned char)*a - (unsigned char)*b;
}

int
strncmp(const char *a, const char *b, size_t n)
{
	for (; n; n--, a++, b++) {
		if (*a != *b) {
			return (unsigned char)*a - (unsigned char)*b;
		}
		if (!*a) {
			break;
		}
	}
	return 0;
}

size_t
strlen(const char *s)
{
	size_t n = 0;
	while (s[n]) {
		n++;
	}
	return n;
}
