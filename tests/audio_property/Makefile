CC ?= cc
CFLAGS ?= -std=c11 -O2 -g -Wall -Wextra
SAN_FLAGS = -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer
FP_FLAGS = -ffp-contract=off
INCLUDES = -I../..

.PHONY: all san clean test

all: ayt_harness

ayt_harness: ayt_harness.c ../../chips/ay38910.h
	$(CC) $(CFLAGS) $(FP_FLAGS) $(INCLUDES) -o $@ ayt_harness.c

ayt_harness_san: ayt_harness.c ../../chips/ay38910.h
	$(CC) $(CFLAGS) $(FP_FLAGS) $(SAN_FLAGS) $(INCLUDES) -o $@ ayt_harness.c

san: ayt_harness_san

test: ayt_harness_san
	ASAN_OPTIONS=detect_leaks=1:abort_on_error=1:strict_string_checks=1 \
	UBSAN_OPTIONS=print_stacktrace=1:halt_on_error=1 \
	python3 run_tests.py

clean:
	rm -f ayt_harness ayt_harness_san
