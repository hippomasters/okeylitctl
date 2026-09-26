/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef OMEN_RGB_PROTOCOL_H
#define OMEN_RGB_PROTOCOL_H

#ifdef __KERNEL__
#include <linux/stddef.h>
#include <linux/types.h>
typedef u8 omen_rgb_u8;
#else
#include <stddef.h>
#include <stdint.h>
typedef uint8_t omen_rgb_u8;
#endif

#define OMEN_RGB_TABLE_SIZE 128U
#define OMEN_RGB_TABLE_MARKER 0x03U
#define OMEN_RGB_ZONES 4U
#define OMEN_RGB_CHANNELS 3U
#define OMEN_RGB_OFFSET 25U
#define OMEN_RGB_BYTES (OMEN_RGB_ZONES * OMEN_RGB_CHANNELS)
#define OMEN_RGB_END (OMEN_RGB_OFFSET + OMEN_RGB_BYTES)

#if OMEN_RGB_END > OMEN_RGB_TABLE_SIZE
#error "RGB fields exceed the firmware color table"
#endif

static inline int omen_rgb_table_valid(const omen_rgb_u8 *table, size_t length)
{
	size_t i;

	if (!table || length != OMEN_RGB_TABLE_SIZE ||
	    table[0] != OMEN_RGB_TABLE_MARKER)
		return 0;
	for (i = 1; i < OMEN_RGB_OFFSET; i++)
		if (table[i] != 0)
			return 0;
	for (i = OMEN_RGB_END; i < OMEN_RGB_TABLE_SIZE; i++)
		if (table[i] != 0)
			return 0;
	return 1;
}

static inline int omen_rgb_apply_colors(
	omen_rgb_u8 *table,
	size_t length,
	const omen_rgb_u8 colors[OMEN_RGB_BYTES])
{
	size_t i;

	if (!colors || !omen_rgb_table_valid(table, length))
		return -1;
	for (i = 0; i < OMEN_RGB_BYTES; i++)
		table[OMEN_RGB_OFFSET + i] = colors[i];
	return 0;
}

#endif /* OMEN_RGB_PROTOCOL_H */
