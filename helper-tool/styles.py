"""Marker colors/categories for ground types — ported from the original
landsoflords-helper project's gui.py so this repo doesn't depend on it."""
from lol_api import GND_TYPES, MINERAL_GROUND_TYPES

CATEGORY_STYLES = {
    "deposit": "#e63946",
    "water": "#4a90d9",
    "wet": "#4a7a6a",
    "volcanic": "#b34700",
    "mountain": "#8a8a8a",
    "rocky": "#a89a7a",
    "arid": "#d9b56a",
    "flat": "#8fbf5f",
}
MINERAL_COLORS = {
    "iron": "#b5651d", "tin": "#c0c0c0", "copper": "#d2691e",
    "coal": "#ff7043", "salt": "#f5f5f5", "lead": "#9fb0c9",
    "silver": "#c9c9c9", "gold": "#ffd700", "emerald": "#50c878",
    "ruby": "#e0115f", "diamond": "#b9f2ff", "saphir": "#0f52ba", "sulfur": "#ffef00",
}
FAILED_COLOR = "#ff3b3b"
OWN_COLOR = "#6b8f5a"

# The 3-way grouping the legend organizes by, per the ground type's own
# category (see lol_api.GND_TYPES) — not a separate classification.
GROUP_WATER = {"water", "wet"}
GROUP_MINERAL = {"deposit"}


def marker_color(type_code):
    category = GND_TYPES.get(type_code, {}).get("category", "flat")
    if type_code in MINERAL_COLORS:
        return MINERAL_COLORS[type_code]
    return CATEGORY_STYLES.get(category, CATEGORY_STYLES["flat"])


def legend_group(type_code):
    if type_code in MINERAL_GROUND_TYPES:
        return "Минералы"
    category = GND_TYPES.get(type_code, {}).get("category", "flat")
    if category in GROUP_WATER:
        return "Вода"
    return "Суша"


def type_name(type_code):
    return GND_TYPES.get(type_code, {}).get("name", type_code)


def owner_color(owner_idx):
    hue = (owner_idx * 47) % 360
    return f"#{_hsl_to_hex(hue, 0.65, 0.5)}"


def _hsl_to_hex(h, s, l):
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h / 360, l, s)
    return f"{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
