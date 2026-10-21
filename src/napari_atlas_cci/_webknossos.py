"""Build upload datasets in the project without staging a second source array."""

import hashlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from webknossos import Dataset


def prepare_dataset(
    source_path: Path,
    dataset_path: Path,
    dataset_name: str,
    voxel_size: tuple[float, float, float],
) -> "Dataset":
    import zarr
    from webknossos import BoundingBox, Dataset
    from webknossos.dataset.sampling_modes import SamplingModes
    from webknossos.dataset_properties import DataFormat
    from webknossos.geometry import Mag

    source_path, dataset_path = Path(source_path), Path(dataset_path)
    if len(voxel_size) != 3 or not all(
        np.isfinite(v) and v > 0 for v in voxel_size
    ):
        raise ValueError(
            "Voxel size must contain three positive finite values."
        )
    source = zarr.open_array(str(source_path), mode="r")
    if source.ndim != 3 or any(size == 0 for size in source.shape):
        raise ValueError(
            f"Expected nonempty 3D Zarr data, got shape {source.shape}."
        )

    # Chunk writes do not necessarily update the source directory's mtime.
    # Include every file so edits to an existing aligned array invalidate reuse.
    digest = hashlib.sha256()
    for path in sorted(source_path.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest.update(
                f"{path.relative_to(source_path).as_posix()}:{stat.st_size}:"
                f"{stat.st_mtime_ns}\n".encode()
            )
    signature = {
        "format": 1,
        "source": str(source_path.absolute()),
        "files": digest.hexdigest(),
        "shape": list(source.shape),
        "dtype": str(source.dtype),
        "voxel_size": list(voxel_size),
    }
    marker = dataset_path / ".atlas_cci_ready"
    try:
        ready = json.loads(marker.read_text(encoding="utf-8")) == signature
    except (OSError, ValueError):
        ready = False
    if ready:
        print(f"Reusing WebKnossos dataset at {dataset_path}.")
        return Dataset.open(dataset_path)

    # This is derived output only. Legacy system-temp caches and saved source
    # copies are deliberately left untouched.
    if dataset_path.exists():
        shutil.rmtree(dataset_path)
    print(f"Building WebKnossos dataset directly at {dataset_path}...")
    dataset = Dataset(dataset_path, voxel_size=voxel_size, name=dataset_name)
    layer = dataset.add_layer(
        "color",
        category="color",
        dtype=source.dtype,
        data_format=DataFormat.Zarr3,
    )
    layer.bounding_box = BoundingBox((0, 0, 0), source.shape[::-1])
    # Write one complete shard at a time, keeping RAM bounded for large stacks.
    # The source is ZYX; WEBKNOSSOS uses XYZ (plus a singleton channel).
    shard_shape = (256, 256, 256)
    mag = layer.add_mag(
        1,
        chunk_shape=(32, 32, 32),
        shard_shape=shard_shape,
        compress=True,
    )
    for box in layer.bounding_box.chunk(shard_shape):
        x, y, z = box.to_slices()
        block = np.asarray(source[z, y, x]).transpose(2, 1, 0)
        mag.write(block, absolute_bounding_box=box)
    dataset.downsample(
        sampling_mode=SamplingModes.ANISOTROPIC,
        coarsest_mag=Mag(32),
    )
    # Write only after the full pyramid succeeds; an interrupted build is rebuilt
    # on retry, while a failed upload can reuse this completed local dataset.
    marker.write_text(json.dumps(signature, sort_keys=True), encoding="utf-8")
    return dataset
