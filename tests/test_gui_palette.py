import pytest

from macos_data_rescue.gui.palette import DARK, LIGHT, is_dark_appearance, palette
from macos_data_rescue.gui.state import STATE_INFO


def test_light_and_dark_palettes_define_the_same_names():
    assert set(LIGHT) == set(DARK)
    assert palette(True) is DARK and palette(False) is LIGHT


def test_every_state_color_is_a_palette_name():
    assert {item.color for item in STATE_INFO.values()} <= set(LIGHT)


def test_counter_and_level_colors_are_palette_names():
    from macos_data_rescue.gui.status import PROBLEM_COLORS

    assert set(PROBLEM_COLORS.values()) | {"muted", "on_accent"} <= set(LIGHT)
    pytest.importorskip("tkinter")
    from macos_data_rescue.gui.app import LEVEL_COLORS

    assert set(LEVEL_COLORS.values()) <= set(LIGHT)


def test_appearance_probe_falls_back_to_light():
    class NoAqua:
        class tk:
            @staticmethod
            def call(*args):
                raise RuntimeError("not aqua")

    assert is_dark_appearance(NoAqua()) is False
