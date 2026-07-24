from importlib import import_module
from importlib.resources import files


def test_package_imports():
    module = import_module("napari_atlas_cci")

    assert hasattr(module, "AtlasCCIWidget")


def test_napari_manifest_is_packaged():
    manifest = files("napari_atlas_cci").joinpath("napari.yaml")

    assert manifest.is_file()
    assert "napari-atlas-cci.open_widget" in manifest.read_text()
