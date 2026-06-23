#!/usr/bin/env python3
import re
import shutil
import sys
from pathlib import Path


VIAL_CONFIG = """#define DYNAMIC_KEYMAP_LAYER_COUNT {layer_count}
#define VIAL_COMBO_ENTRIES {combo_count}
#define VIAL_KEYBOARD_UID {{0xFD, 0x2F, 0x7F, 0x8A, 0x39, 0x07, 0xF7, 0xDB}}
#define VIAL_UNLOCK_COMBO_ROWS {{ 11, 11 }}
#define VIAL_UNLOCK_COMBO_COLS {{ 5, 6 }}
"""


def fail(message):
    print(f"prepare-vial-keymap: {message}", file=sys.stderr)
    sys.exit(1)


def count_layers(keymap):
    layers = [int(match) for match in re.findall(r"^\s*\[(\d+)\]\s*=\s*LAYOUT", keymap, re.MULTILINE)]
    if not layers:
        fail("could not find any LAYOUT layers in keymap.c")
    return max(layers) + 1


def find_combos(keymap):
    combo_arrays = {}
    combo_array_re = re.compile(r"const\s+uint16_t\s+PROGMEM\s+(\w+)\[\]\s*=\s*\{\s*(.*?)\s*\};")
    for name, body in combo_array_re.findall(keymap):
        keys = [key.strip() for key in body.split(",")]
        keys = [key for key in keys if key and key != "COMBO_END"]
        combo_arrays[name] = keys

    combo_table_re = re.compile(r"combo_t\s+key_combos\s*\[[^\]]*\]\s*=\s*\{(?P<body>.*?)\n\};", re.DOTALL)
    match = combo_table_re.search(keymap)
    if not match:
        return [], keymap

    combos = []
    for line in match.group("body").splitlines():
        if "COMBO(" not in line:
            continue
        inner = line[line.index("COMBO(") + len("COMBO("):].strip()
        if inner.endswith(","):
            inner = inner[:-1].rstrip()
        if inner.endswith(")"):
            inner = inner[:-1].rstrip()
        combo_name, output = inner.split(",", 1)
        combo_name = combo_name.strip()
        output = output.strip()
        keys = combo_arrays.get(combo_name)
        if keys is None:
            fail(f"combo table references unknown combo array {combo_name}")
        if len(keys) > 4:
            fail(f"Vial combo {combo_name} has {len(keys)} keys; Vial supports up to 4")
        combos.append((keys, output))

    return combos, keymap


def transform_config(config, layer_count, combo_count):
    config = re.sub(r"^#define\s+COMBO_COUNT\s+\d+\n", "", config, flags=re.MULTILINE)

    serial_re = re.compile(r"^#define\s+SERIAL_NUMBER\s+.+$", re.MULTILINE)
    serial = serial_re.search(config)
    if serial:
        config = config[:serial.start()] + "#undef SERIAL_NUMBER\n" + config[serial.start():]

    insert_after = serial_re.search(config)
    if not insert_after:
        insert_after = re.search(r"^#define\s+LAYER_STATE_8BIT\s*$", config, re.MULTILINE)
    if not insert_after:
        fail("could not find a stable insertion point in config.h")

    vial_config = VIAL_CONFIG.format(layer_count=layer_count, combo_count=combo_count)
    return config[:insert_after.end()] + "\n" + vial_config + config[insert_after.end():]


def transform_rules(rules):
    output = []
    for line in rules.splitlines():
        if re.match(r"^\s*(ORYX_ENABLE|RGB_MATRIX_CUSTOM_KB)\s*=", line):
            continue
        output.append(line)

    if not any(re.match(r"^\s*VIA_ENABLE\s*=", line) for line in output):
        output.append("VIA_ENABLE = yes")
    if not any(re.match(r"^\s*VIAL_ENABLE\s*=", line) for line in output):
        output.append("VIAL_ENABLE = yes")

    return "\n".join(output) + "\n"


def transform_keymap(keymap, combos):
    if "void eeconfig_init_user(void)" in keymap:
        fail("keymap.c already defines eeconfig_init_user; seed Vial combos there manually")

    keymap = keymap.replace(
        "bool rgb_matrix_indicators_user(void) {\n"
        "  if (rawhid_state.rgb_control) {\n"
        "      return false;\n"
        "  }\n",
        "bool rgb_matrix_indicators_user(void) {\n"
        "#ifdef ORYX_ENABLE\n"
        "  if (rawhid_state.rgb_control) {\n"
        "      return false;\n"
        "  }\n"
        "#endif\n",
    )

    combo_table_re = re.compile(r"combo_t\s+key_combos\s*\[[^\]]*\]\s*=\s*\{(?P<body>.*?)\n\};", re.DOTALL)
    match = combo_table_re.search(keymap)
    if not match or not combos:
        return keymap

    seed_entries = []
    for keys, output in combos:
        padded = keys + ["COMBO_END"] * (4 - len(keys))
        seed_entries.append(f"    {{{{{', '.join(padded)}}}, {output}}},")

    replacement = (
        "#ifndef VIAL_COMBO_ENABLE\n"
        + match.group(0)
        + "\n#else\n"
        "static bool seed_vial_combos_on_init;\n\n"
        "static const vial_combo_entry_t vial_default_combos[] = {\n"
        + "\n".join(seed_entries)
        + "\n};\n\n"
        "static void seed_vial_default_combos(void) {\n"
        "    for (uint8_t i = 0; i < ARRAY_SIZE(vial_default_combos) && i < VIAL_COMBO_ENTRIES; i++) {\n"
        "        dynamic_keymap_set_combo(i, &vial_default_combos[i]);\n"
        "    }\n"
        "}\n\n"
        "void eeconfig_init_user(void) {\n"
        "    seed_vial_combos_on_init = true;\n"
        "    seed_vial_default_combos();\n"
        "}\n"
        "#endif"
    )
    keymap = keymap[:match.start()] + replacement + keymap[match.end():]

    return keymap.replace(
        "void keyboard_post_init_user(void) {\n  rgb_matrix_enable();\n}",
        "void keyboard_post_init_user(void) {\n"
        "#ifdef VIAL_COMBO_ENABLE\n"
        "  if (seed_vial_combos_on_init) {\n"
        "    vial_init();\n"
        "    seed_vial_combos_on_init = false;\n"
        "  }\n"
        "#endif\n"
        "  rgb_matrix_enable();\n"
        "}",
    )


def main():
    if len(sys.argv) != 3:
        fail("usage: prepare-vial-keymap.py <oryx-layout-dir> <vial-keymap-dir>")

    source = Path(sys.argv[1])
    dest = Path(sys.argv[2])
    if not source.is_dir():
        fail(f"{source} is not a layout directory")
    if not (source / "vial.json").is_file():
        fail(f"{source / 'vial.json'} is missing")

    keymap = (source / "keymap.c").read_text()
    combos, keymap = find_combos(keymap)
    layer_count = count_layers(keymap)

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    shutil.copy2(source / "vial.json", dest / "vial.json")
    (dest / "config.h").write_text(transform_config((source / "config.h").read_text(), layer_count, len(combos)))
    (dest / "rules.mk").write_text(transform_rules((source / "rules.mk").read_text()))
    (dest / "keymap.c").write_text(transform_keymap(keymap, combos))


if __name__ == "__main__":
    main()
