"""
pantone_2026.py
---------------
Pantone Colour of the Year 2026 — seven trend palettes with approximate
hex conversions for digital/screen use.

Used by image_overlay.py when selecting accent colours for split/solid
banner templates so generated creatives stay on-trend.

Hex values are closest-match sRGB approximations of the Pantone TCX codes.
"""
from __future__ import annotations

PALETTES: dict[str, dict] = {
    "Powdered Pastels": {
        "mood": "soft, delicate, airy — light breakfast menus, wellness, morning campaigns",
        "colors": {
            "Lemon Icing":        "#F5F4C0",
            "Nimbus Cloud":       "#D4D2CE",
            "Raindrops on Roses": "#F2EBE3",
            "Cloud Dancer":       "#F4F5F0",
            "Ice Melt":           "#C8CFCD",
            "Peach Dust":         "#EDD9C8",
            "Almost Aqua":        "#C4D3CC",
            "Orchid Tint":        "#DDD1D9",
        },
    },
    "Take a Break": {
        "mood": "warm, cosy, earthy — coffee, brunch, comfort food, afternoon specials",
        "colors": {
            "Iced Coffee":   "#B8916E",
            "Mango Mojito":  "#C5A04E",
            "Cocoa Crème":   "#8B6650",
            "Pink Lemonade": "#E89080",
            "Tea":           "#B8A894",
            "Papaya":        "#F09858",
            "Caramel":       "#C07840",
        },
    },
    "Atmospheric": {
        "mood": "calm, open, cool-professional — events, new openings, corporate dining",
        "colors": {
            "Nantucket Breeze": "#C0C8D0",
            "Cloud Dancer":     "#EEF0F0",
            "Alaskan Blue":     "#8BA8B8",
            "Cosmic Sky":       "#A0B8D0",
            "Aqua Gray":        "#A8C0C0",
            "Regatta":          "#2E5888",
            "Rinsing Rivulet":  "#9ABCB8",
            "Dusky Citron":     "#D4C878",
        },
    },
    "Comfort Zone": {
        "mood": "grounded, nurturing, natural — pastries, artisan bakes, farm-to-table",
        "colors": {
            "Shifting Sand": "#D8C0A8",
            "Coral Haze":    "#E0A890",
            "Mountain Trail":"#8A7E72",
            "Amberlight":    "#DEC898",
            "Ashes of Roses":"#C8B8A8",
            "Woodrose":      "#A88880",
            "Rose Brown":    "#B89888",
        },
    },
    "Tropic Tonalities": {
        "mood": "bold, vivid, energetic — cocktails, summer specials, happy hour",
        "colors": {
            "Iris Orchid":     "#9080A8",
            "Capri":           "#60B0C8",
            "Kiwi Colada":     "#A8D048",
            "Sunny Lime":      "#E8EE70",
            "Bright Marigold": "#F09028",
            "Paradise Pink":   "#E84860",
            "Blazing Yellow":  "#F8E040",
        },
    },
    "Light & Shadow": {
        "mood": "sophisticated, editorial, moody — evening dining, premium launches",
        "colors": {
            "Veiled Vista": "#E0E4DC",
            "Baltic Sea":   "#90B0C8",
            "Golden Mist":  "#C8B880",
            "Quiet Violet": "#9888A8",
            "Cloud Cover":  "#A8A0A0",
            "Hematite":     "#686060",
            "Blue Fusion":  "#4870A0",
        },
    },
    "Glamour & Gleam": {
        "mood": "luxury, dramatic, high-impact — gala nights, premium product launches",
        "colors": {
            "Stretch Limo":  "#2A2828",
            "Scarlet Smile": "#A02820",
            "Bordeaux":      "#783040",
            "Dragonfly":     "#204858",
            "Graphite":      "#484848",
            "Satin Slipper": "#D0C8C0",
        },
    },
}


def palette_prompt_block() -> str:
    """
    Compact multi-line string for inclusion in a Claude prompt.
    Lists each palette name, its mood hook, and the hex values.
    """
    lines = [
        "PANTONE 2026 TREND PALETTES — pick ONE hex as the accent colour for split/solid templates:",
    ]
    for name, data in PALETTES.items():
        hexes = ", ".join(data["colors"].values())
        lines.append(f'  "{name}" [{data["mood"]}]: {hexes}')
    return "\n".join(lines)


def all_hex_colors() -> list[str]:
    """Flat list of every 2026 palette hex value, for validation."""
    return [h for p in PALETTES.values() for h in p["colors"].values()]
