#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "../module/omen_rgb_protocol.h"

static void test_valid_table_changes_only_rgb_bytes(void)
{
	uint8_t table[OMEN_RGB_TABLE_SIZE];
	uint8_t before[OMEN_RGB_TABLE_SIZE];
	const uint8_t colors[OMEN_RGB_BYTES] = {
		0xff, 0x00, 0x00, 0x00, 0xff, 0x00,
		0x00, 0x00, 0xff, 0xff, 0xff, 0xff,
	};
	size_t i;

	memset(table, 0, sizeof(table));
	table[0] = OMEN_RGB_TABLE_MARKER;
	memcpy(before, table, sizeof(table));

	assert(omen_rgb_apply_colors(table, sizeof(table), colors) == 0);
	assert(memcmp(table + OMEN_RGB_OFFSET, colors, OMEN_RGB_BYTES) == 0);
	for (i = 0; i < sizeof(table); i++) {
		if (i >= OMEN_RGB_OFFSET && i < OMEN_RGB_OFFSET + OMEN_RGB_BYTES)
			continue;
		assert(table[i] == before[i]);
	}
}

static void test_invalid_table_is_never_modified(void)
{
	uint8_t table[OMEN_RGB_TABLE_SIZE] = { 0 };
	uint8_t before[OMEN_RGB_TABLE_SIZE];
	const uint8_t colors[OMEN_RGB_BYTES] = { 0 };

	memcpy(before, table, sizeof(table));
	assert(omen_rgb_apply_colors(table, sizeof(table), colors) != 0);
	assert(memcmp(table, before, sizeof(table)) == 0);

	table[0] = OMEN_RGB_TABLE_MARKER;
	memcpy(before, table, sizeof(table));
	assert(omen_rgb_apply_colors(table, OMEN_RGB_TABLE_SIZE - 1, colors) != 0);
	assert(memcmp(table, before, sizeof(table)) == 0);
}

static void test_nonzero_reserved_byte_is_rejected(void)
{
	uint8_t table[OMEN_RGB_TABLE_SIZE] = { 0 };

	table[0] = OMEN_RGB_TABLE_MARKER;
	table[1] = 1;
	assert(!omen_rgb_table_valid(table, sizeof(table)));
	table[1] = 0;
	table[OMEN_RGB_END] = 1;
	assert(!omen_rgb_table_valid(table, sizeof(table)));
}

int main(void)
{
	test_valid_table_changes_only_rgb_bytes();
	test_invalid_table_is_never_modified();
	test_nonzero_reserved_byte_is_rejected();
	puts("protocol tests passed");
	return 0;
}
