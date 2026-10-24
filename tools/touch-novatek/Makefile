CC ?= cc
CFLAGS ?= -std=c99 -Wall -Wextra -Werror -O2

all: test_nt36523

test_nt36523: nt36523.c test_nt36523.c nt36523.h
	$(CC) $(CFLAGS) -o $@ nt36523.c test_nt36523.c

clean:
	rm -f test_nt36523

.PHONY: all clean
