import pytest

from macos_data_rescue.gui.palette import DARK, LIGHT, is_dark_appearance, palette
from macos_data_rescue.gui.state import STATE_INFO


def test_light_and_dark_palettes_define_the_same_names():
    assert set(LIGHT) == set(DARK)
    assert palette(True) is DARK and palette(False) is LIGHT


def test_every_state_color_is_a_palette_name():
    assert {item.color for item in STATE_INFO.values()} <= set(LIGHT)


def test_badge_and_level_colors_are_palette_names():
    pytest.importorskip("tkinter")
    from macos_data_rescue.gui.app import BADGES, LEVEL_COLORS

    assert {color for _, color in BADGES} | set(LEVEL_COLORS.values()) <= set(LIGHT)


def test_appearance_probe_falls_back_to_light():
    class NoAqua:
        class tk:
            @staticmethod
            def call(*args):
                raise RuntimeError("not aqua")

    assert is_dark_appearance(NoAqua()) is False
