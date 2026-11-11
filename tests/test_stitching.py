import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import tifffile

from napari_atlas_cci import AtlasCCIWidget


@pytest.mark.parametrize("with_metadata", [False, True])
def test_process_series_supports_backend_return_versions(
    tmp_path, monkeypatch, with_metadata
):
    import atlas.stitching

    series = tmp_path / "S_1"
    series.mkdir()
    (series / "tile_S_1.tif").touch()
    (series / "tiles.ve-mif").touch()
    image = np.array([[1, 2], [3, 4]], dtype=np.uint8)
    metadata = {"tiles": {"0": {"origin_in_stitched": [1, 2]}}}
    result = (image, pd.DataFrame({"tile": [1]}), {"t00": np.array([0, 1])})
    if with_metadata:
        result += (metadata,)
    monkeypatch.setattr(atlas.stitching, "stitch_ATLAS_tiles", lambda *a, **kw: result)
    widget = SimpleNamespace(
        main_directory=tmp_path, settings=SimpleNamespace(max_shift_pixels=200)
    )

    assert AtlasCCIWidget._process_one_series(widget, series) == (True, "")
    np.testing.assert_array_equal(
        tifffile.imread(tmp_path / "stitched_image_S_1.tiff"), np.flipud(image)
    )
    assert json.loads((tmp_path / "transforms_S_1.json").read_text()) == {
        "t00": [0, 1]
    }
    assert (tmp_path / "phaseCC_stitching_S_1.csv").exists()
    metadata_path = tmp_path / "stitched_image_S_1_metadata.json"
    assert metadata_path.exists() == with_metadata
    if with_metadata:
        assert json.loads(metadata_path.read_text()) == metadata
        with tifffile.TiffFile(tmp_path / "stitched_image_S_1.tiff") as tif:
            assert json.loads(tif.pages[0].description)["stitching_metadata"] == metadata


        # Exercise the actual backend reader on the TIFF saved by the plugin.
        from atlas.alignment import ROI
        from atlas.io import apply_alignment

        path = tmp_path / "stitched_image_S_1.tiff"
        roi = ROI(0, 2, 0, 2)
        frame = pd.DataFrame({
            "moving_path": [path], "reference_path": [path],
            "moving_ROI": [roi], "cumulative_ROI": [roi], "down_scale": [1],
        })
        for preview in (True, False):
            aligned, _ = apply_alignment(
                frame, buffer_pixels=0, use_down_sample=preview
            )
            assert aligned.shape == (2, 2, 1)
            saved = aligned.attrs["AtlasMetadata"]["images"]["0"]
            assert saved["stitching_metadata"] == metadata
