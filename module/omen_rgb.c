// SPDX-License-Identifier: GPL-2.0-only
/*
 * Safe four-zone RGB control for one explicitly validated HP OMEN platform.
 *
 * The WMI packet layout is derived from the GPL-licensed Linux hp-wmi driver
 * and the upstream HP keyboard-backlight patch series. No raw WMI interface
 * is exposed to userspace.
 */
#include <linux/acpi.h>
#include <linux/dmi.h>
#include <linux/hex.h>
#include <linux/jiffies.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/wmi.h>

#include "omen_rgb_protocol.h"

#define DRIVER_NAME "omen-rgb"
#define DRIVER_VERSION "0.1.0"
#define ABI_VERSION "1"

#define HPWMI_BIOS_GUID "5FB7F034-2C63-45E9-BE91-3D44E2C707E4"
#define HPWMI_GM 0x20008
#define HPWMI_BACKLIGHT 0x20009
#define HPWMI_GET_KEYBOARD_TYPE_QUERY 0x2b
#define HPWMI_COLOR_GET_QUERY 0x02
#define HPWMI_COLOR_SET_QUERY 0x03
#define HPWMI_STATE_GET_QUERY 0x04
#define HPWMI_STATE_OFF 0x64
#define HPWMI_STATE_ON 0xe4
#define HP_KEYBOARD_TYPE_FOURZONE_WITHOUT_NUMPAD 0x02
#define HP_BIOS_SIGNATURE 0x55434553
#define WRITE_INTERVAL_MS 25

struct bios_args {
	u32 signature;
	u32 command;
	u32 commandtype;
	u32 datasize;
	u8 data[];
};

struct bios_return {
	u32 sigpass;
	u32 return_code;
};

static DEFINE_MUTEX(firmware_lock);
static struct platform_device *omen_rgb_device;
static u8 original_table[OMEN_RGB_TABLE_SIZE];
static unsigned long last_write;
static bool ready;

static const struct dmi_system_id omen_rgb_dmi_table[] = {
	{
		.ident = "HP OMEN 16-wf0xxx B21E3PA BIOS F.26",
		.matches = {
			DMI_EXACT_MATCH(DMI_SYS_VENDOR, "HP"),
			DMI_EXACT_MATCH(DMI_PRODUCT_NAME,
					"OMEN by HP Gaming Laptop 16-wf0xxx"),
			DMI_EXACT_MATCH(DMI_BOARD_NAME, "8BAB"),
			DMI_EXACT_MATCH(DMI_PRODUCT_SKU, "B21E3PA#ACJ"),
		},
	},
	{ }
};
MODULE_DEVICE_TABLE(dmi, omen_rgb_dmi_table);

static int output_method_id(size_t output_size)
{
	if (output_size > 4096)
		return -EINVAL;
	if (output_size > 1024)
		return 5;
	if (output_size > 128)
		return 4;
	if (output_size > 4)
		return 3;
	if (output_size > 0)
		return 2;
	return 1;
}

/*
 * method_output_size selects HP's output-buffer method. copy_output controls
 * whether that payload is required and copied. The validated 128-byte
 * color-table SET uses method 3.
 */
static int hp_query(u32 query, u32 command, void *buffer, size_t input_size,
		    size_t method_output_size, bool copy_output)
{
	struct acpi_buffer input;
	struct acpi_buffer output = { ACPI_ALLOCATE_BUFFER, NULL };
	struct bios_args *args;
	struct bios_return *reply;
	union acpi_object *obj;
	size_t allocation_size, request_data_size, required_size;
	acpi_status status;
	int method_id, ret = 0;

	if (!buffer || input_size > OMEN_RGB_TABLE_SIZE ||
	    method_output_size > OMEN_RGB_TABLE_SIZE)
		return -EINVAL;

	method_id = output_method_id(method_output_size);
	if (method_id < 0)
		return method_id;

	request_data_size = max_t(size_t, input_size, OMEN_RGB_TABLE_SIZE);
	allocation_size = struct_size(args, data, request_data_size);
	args = kzalloc(allocation_size, GFP_KERNEL);
	if (!args)
		return -ENOMEM;

	args->signature = HP_BIOS_SIGNATURE;
	args->command = command;
	args->commandtype = query;
	args->datasize = input_size;
	memcpy(args->data, buffer, input_size);

	input.length = allocation_size;
	input.pointer = args;
	status = wmi_evaluate_method(HPWMI_BIOS_GUID, 0, method_id,
				     &input, &output);
	kfree(args);
	if (ACPI_FAILURE(status)) {
		kfree(output.pointer);
		return -EIO;
	}

	obj = output.pointer;
	if (!obj) {
		ret = -EPROTO;
		goto out;
	}
	if (obj->type != ACPI_TYPE_BUFFER || !obj->buffer.pointer ||
	    obj->buffer.length < sizeof(*reply)) {
		ret = -EPROTO;
		goto out;
	}

	reply = (struct bios_return *)obj->buffer.pointer;
	if (reply->return_code) {
		pr_err_ratelimited("firmware rejected query 0x%x (code 0x%x)\n",
				   query, reply->return_code);
		ret = -EIO;
		goto out;
	}

	if (copy_output) {
		required_size = sizeof(*reply) + method_output_size;
		if (obj->buffer.length < required_size) {
			ret = -EPROTO;
			goto out;
		}
		memcpy(buffer, obj->buffer.pointer + sizeof(*reply),
		       method_output_size);
	}
out:
	kfree(obj);
	return ret;
}

static int get_color_table(u8 table[OMEN_RGB_TABLE_SIZE])
{
	int ret;

	memset(table, 0, OMEN_RGB_TABLE_SIZE);
	ret = hp_query(HPWMI_COLOR_GET_QUERY, HPWMI_BACKLIGHT, table,
		       OMEN_RGB_TABLE_SIZE, OMEN_RGB_TABLE_SIZE, true);
	if (ret)
		return ret;
	return omen_rgb_table_valid(table, OMEN_RGB_TABLE_SIZE) ? 0 : -EPROTO;
}

static int set_color_table(const u8 table[OMEN_RGB_TABLE_SIZE])
{
	u8 payload[OMEN_RGB_TABLE_SIZE];

	if (!omen_rgb_table_valid(table, OMEN_RGB_TABLE_SIZE))
		return -EPROTO;
	memcpy(payload, table, sizeof(payload));
	return hp_query(HPWMI_COLOR_SET_QUERY, HPWMI_BACKLIGHT, payload,
			OMEN_RGB_TABLE_SIZE, OMEN_RGB_TABLE_SIZE, false);
}

static int check_write_rate(void)
{
	unsigned long next = last_write + msecs_to_jiffies(WRITE_INTERVAL_MS);

	if (last_write && time_before(jiffies, next))
		return -EBUSY;
	return 0;
}

static int parse_colors(const char *buf, size_t count,
			u8 colors[OMEN_RGB_BYTES])
{
	size_t zone, length = count;

	if (length && buf[length - 1] == '\n')
		length--;
	if (length != 27)
		return -EINVAL;

	for (zone = 0; zone < OMEN_RGB_ZONES; zone++) {
		size_t input_offset = zone * 7;

		if (zone && buf[input_offset - 1] != ',')
			return -EINVAL;
		if (hex2bin(colors + zone * OMEN_RGB_CHANNELS,
			    buf + input_offset, OMEN_RGB_CHANNELS))
			return -EINVAL;
	}
	return 0;
}

static ssize_t abi_version_show(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, ABI_VERSION "\n");
}

static ssize_t colors_show(struct device *dev,
			   struct device_attribute *attr, char *buf)
{
	u8 table[OMEN_RGB_TABLE_SIZE];
	int ret;

	mutex_lock(&firmware_lock);
	ret = ready ? get_color_table(table) : -ENODEV;
	mutex_unlock(&firmware_lock);
	if (ret)
		return ret;

	return sysfs_emit(buf,
		"%02X%02X%02X,%02X%02X%02X,%02X%02X%02X,%02X%02X%02X\n",
		table[25], table[26], table[27],
		table[28], table[29], table[30],
		table[31], table[32], table[33],
		table[34], table[35], table[36]);
}

static ssize_t colors_store(struct device *dev,
			    struct device_attribute *attr,
			    const char *buf, size_t count)
{
	u8 requested[OMEN_RGB_BYTES];
	u8 table[OMEN_RGB_TABLE_SIZE];
	u8 verify[OMEN_RGB_TABLE_SIZE];
	int ret;

	ret = parse_colors(buf, count, requested);
	if (ret)
		return ret;

	mutex_lock(&firmware_lock);
	if (!ready) {
		ret = -ENODEV;
		goto out;
	}
	ret = check_write_rate();
	if (ret)
		goto out;
	ret = get_color_table(table);
	if (ret)
		goto out;
	ret = omen_rgb_apply_colors(table, sizeof(table), requested);
	if (ret) {
		ret = -EPROTO;
		goto out;
	}
	ret = set_color_table(table);
	if (ret)
		goto out;
	last_write = jiffies;
	ret = get_color_table(verify);
	if (ret)
		goto out;
	if (memcmp(verify, table, OMEN_RGB_TABLE_SIZE))
		ret = -EIO;
out:
	mutex_unlock(&firmware_lock);
	return ret ? ret : count;
}

static ssize_t original_show(struct device *dev,
			     struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf,
		"%02X%02X%02X,%02X%02X%02X,%02X%02X%02X,%02X%02X%02X\n",
		original_table[25], original_table[26], original_table[27],
		original_table[28], original_table[29], original_table[30],
		original_table[31], original_table[32], original_table[33],
		original_table[34], original_table[35], original_table[36]);
}

static ssize_t state_show(struct device *dev,
			  struct device_attribute *attr, char *buf)
{
	u8 state = 0;
	int ret;

	mutex_lock(&firmware_lock);
	ret = ready ? hp_query(HPWMI_STATE_GET_QUERY, HPWMI_BACKLIGHT,
			       &state, sizeof(state), sizeof(state), true) : -ENODEV;
	mutex_unlock(&firmware_lock);
	if (ret)
		return ret;
	if (state != HPWMI_STATE_ON && state != HPWMI_STATE_OFF)
		return -EPROTO;
	return sysfs_emit(buf, "%s\n",
			  state == HPWMI_STATE_ON ? "on" : "off");
}


static ssize_t restore_store(struct device *dev,
			     struct device_attribute *attr,
			     const char *buf, size_t count)
{
	u8 table[OMEN_RGB_TABLE_SIZE];
	u8 verify[OMEN_RGB_TABLE_SIZE];
	int ret;

	if (!sysfs_streq(buf, "1"))
		return -EINVAL;

	mutex_lock(&firmware_lock);
	if (!ready) {
		ret = -ENODEV;
		goto out;
	}
	ret = check_write_rate();
	if (ret)
		goto out;
	ret = get_color_table(table);
	if (ret)
		goto out;
	ret = omen_rgb_apply_colors(table, sizeof(table),
				    original_table + OMEN_RGB_OFFSET);
	if (ret) {
		ret = -EPROTO;
		goto out;
	}
	ret = set_color_table(table);
	if (ret)
		goto out;
	last_write = jiffies;
	ret = get_color_table(verify);
	if (ret)
		goto out;
	if (memcmp(verify, table, OMEN_RGB_TABLE_SIZE))
		ret = -EIO;
out:
	mutex_unlock(&firmware_lock);
	return ret ? ret : count;
}

static struct device_attribute dev_attr_abi_version =
	__ATTR(abi_version, 0444, abi_version_show, NULL);
static struct device_attribute dev_attr_colors =
	__ATTR(colors, 0600, colors_show, colors_store);
static struct device_attribute dev_attr_original =
	__ATTR(original, 0444, original_show, NULL);
static struct device_attribute dev_attr_state =
	__ATTR(state, 0400, state_show, NULL);
static struct device_attribute dev_attr_restore =
	__ATTR(restore, 0200, NULL, restore_store);

static struct attribute *omen_rgb_attrs[] = {
	&dev_attr_abi_version.attr,
	&dev_attr_colors.attr,
	&dev_attr_original.attr,
	&dev_attr_state.attr,
	&dev_attr_restore.attr,
	NULL,
};
ATTRIBUTE_GROUPS(omen_rgb);

static int __init omen_rgb_init(void)
{
	const char *bios_version;
	u8 keyboard_type = 0xff;
	int ret;

	if (!dmi_check_system(omen_rgb_dmi_table)) {
		pr_err("unsupported hardware; refusing to issue firmware commands\n");
		return -ENODEV;
	}
	bios_version = dmi_get_system_info(DMI_BIOS_VERSION);
	if (!bios_version || strcmp(bios_version, "F.26")) {
		pr_err("unsupported BIOS; refusing to issue firmware commands\n");
		return -ENODEV;
	}
	if (!wmi_has_guid(HPWMI_BIOS_GUID))
		return -ENODEV;

	ret = hp_query(HPWMI_GET_KEYBOARD_TYPE_QUERY, HPWMI_GM,
		       &keyboard_type, sizeof(keyboard_type),
		       sizeof(keyboard_type), true);
	if (ret)
		return ret;
	if (keyboard_type != HP_KEYBOARD_TYPE_FOURZONE_WITHOUT_NUMPAD)
		return -ENODEV;

	ret = get_color_table(original_table);
	if (ret)
		return ret;

	omen_rgb_device = platform_device_register_simple(DRIVER_NAME,
						   PLATFORM_DEVID_NONE,
						   NULL, 0);
	if (IS_ERR(omen_rgb_device))
		return PTR_ERR(omen_rgb_device);

	mutex_lock(&firmware_lock);
	ready = true;
	mutex_unlock(&firmware_lock);
	ret = sysfs_create_groups(&omen_rgb_device->dev.kobj, omen_rgb_groups);
	if (ret) {
		mutex_lock(&firmware_lock);
		ready = false;
		mutex_unlock(&firmware_lock);
		platform_device_unregister(omen_rgb_device);
		return ret;
	}

	pr_info("ready on validated hardware; ABI %s, driver %s\n",
		ABI_VERSION, DRIVER_VERSION);
	return 0;
}

static void __exit omen_rgb_exit(void)
{
	sysfs_remove_groups(&omen_rgb_device->dev.kobj, omen_rgb_groups);
	mutex_lock(&firmware_lock);
	ready = false;
	mutex_unlock(&firmware_lock);
	platform_device_unregister(omen_rgb_device);
	pr_info("unloaded; keyboard state left unchanged\n");
}

module_init(omen_rgb_init);
module_exit(omen_rgb_exit);

MODULE_AUTHOR("OMEN RGB Linux contributors");
MODULE_DESCRIPTION("Restricted HP OMEN 16-wf0xxx four-zone RGB controller");
MODULE_LICENSE("GPL");
MODULE_VERSION(DRIVER_VERSION);
MODULE_SOFTDEP("pre: hp_wmi");
