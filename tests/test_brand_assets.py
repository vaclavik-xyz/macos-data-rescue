from helpers import ROOT


def test_discoverable_project_icon_matches_brand_master():
    # assets/icon.svg exists only so tools such as T3 Code find the project icon;
    # assets/brand/Favicon.svg is the editable master (see assets/brand/README.md)
    master = ROOT / "assets" / "brand" / "Favicon.svg"
    copy = ROOT / "assets" / "icon.svg"
    assert copy.read_bytes() == master.read_bytes(), "copy assets/brand/Favicon.svg to assets/icon.svg"
