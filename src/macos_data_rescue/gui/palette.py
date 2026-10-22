"""Named text colors for light and dark macOS appearance.

The 700 shades read well on the light Aqua background but nearly vanish on the
dark one (dark red/blue/violet on dark gray), so widgets use color names and the
console resolves them once at startup for the current appearance.
``on_accent`` is the text color on a filled status pill of any other color.
"""
from __future__ import annotations

from typing import Any

LIGHT = {
    "green": "#15803d", "cyan": "#0e7490", "blue": "#1d4ed8", "amber": "#b45309", "red": "#b91c1c",
    "violet": "#7c3aed", "gray": "#4b5563", "orange": "#c2410c", "muted": "#6b7280",
    "on_accent": "#ffffff",
}
DARK = {
    "green": "#4ade80", "cyan": "#22d3ee", "blue": "#60a5fa", "amber": "#fbbf24", "red": "#f87171",
    "violet": "#a78bfa", "gray": "#9ca3af", "orange": "#fb923c", "muted": "#a1a1aa",
    "on_accent": "#111827",
}


def palette(dark: bool) -> dict[str, str]:
    return DARK if dark else LIGHT


def is_dark_appearance(root: Any) -> bool:
    """True when the Tk root renders in macOS dark mode; False elsewhere or on error."""
    try:
        return bool(int(root.tk.call("tk::unsupported::MacWindowStyle", "isdark", root)))
    except Exception:  # not Aqua / older Tk: assume light
        return False
