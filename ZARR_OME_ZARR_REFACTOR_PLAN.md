# Zarr and OME-Zarr Refactor Plan

## Summary

The current pipeline uses a `.zarr` path for data that is actually written as an OME-Zarr multiscale pyramid. That makes the naming confusing and also forces CZI export and WebKnossos upload to read the OME-Zarr metadata, resolve pyramid level 0, and convert that back into a plain Zarr-like array before doing their real work.

The proposed change is to make the final aligned plain Zarr array the canonical output of the alignment step, and to create OME-Zarr only as a derived visualization artifact for napari.

Target flow:

```text
final alignment
  -> project_name_aligned.zarr          # plain Zarr array, canonical data
  -> project_name_aligned.ome.zarr      # derived OME-Zarr pyramid for napari

CZI export
  -> reads project_name_aligned.zarr directly

WebKnossos upload
  -> reads project_name_aligned.zarr directly
```

This separates storage format from visualization format, removes the OME-Zarr dependency from export/upload, makes the file names honest, and avoids fragile pyramid metadata lookups in non-visualization code.

## Why

- Plain `.zarr` should mean a direct Zarr array.
- `.ome.zarr` should mean an OME-Zarr store with multiscales metadata and pyramid levels.
- The final alignment output is processing data, not only a visualization pyramid.
- CZI export and WebKnossos upload only need the full-resolution aligned array, so they should not depend on OME-Zarr metadata.
- The OME-Zarr pyramid can always be regenerated from the plain aligned Zarr if needed.
- The current code has one misleading helper, `_ome_zarr_dataset_path()`, that returns a tuple in some error cases even though callers expect a `Path`.

## Current Code Locations

- Path naming is currently centralized in `src/napari_atlas_cci/Atlas_CCI_Widget.py:413` with `_zarr_output_path()`.
- Final and preview output existence checks are at `src/napari_atlas_cci/Atlas_CCI_Widget.py:418` and `src/napari_atlas_cci/Atlas_CCI_Widget.py:421`.
- Alignment output generation is in `src/napari_atlas_cci/Atlas_CCI_Widget.py:1076`, `_apply_z_shifts_worker()`.
- OME-Zarr writing currently happens inside `_apply_z_shifts_worker()` around `src/napari_atlas_cci/Atlas_CCI_Widget.py:1161`.
- Previous output deletion/replacement currently happens around `src/napari_atlas_cci/Atlas_CCI_Widget.py:1187`.
- Napari display uses OME-Zarr in `src/napari_atlas_cci/gui.py:212`, `display_ome_zarr()`.
- Final output display is triggered from `src/napari_atlas_cci/Atlas_CCI_Widget.py:1354`, `_display_zalign_zarr()`.
- CZI export starts at `src/napari_atlas_cci/Atlas_CCI_Widget.py:1588`, with the worker at `src/napari_atlas_cci/Atlas_CCI_Widget.py:1621`.
- WebKnossos upload starts at `src/napari_atlas_cci/Atlas_CCI_Widget.py:1657`, with the worker at `src/napari_atlas_cci/Atlas_CCI_Widget.py:1717`.

## Proposed Naming

Use separate helpers for each output type:

```python
def _aligned_zarr_path(self, root_folder: Path) -> Path:
    return root_folder / f"{root_folder.name}_aligned.zarr"

def _aligned_ome_zarr_path(self, root_folder: Path) -> Path:
    return root_folder / f"{root_folder.name}_aligned.ome.zarr"

def _preview_ome_zarr_path(self, root_folder: Path) -> Path:
    return root_folder / f"{root_folder.name}_downsample.ome.zarr"
```

Suggested file outputs:

```text
project_name_aligned.zarr
project_name_aligned.ome.zarr
project_name_downsample.ome.zarr
project_name_aligned.czi
```

## Proposed Function Changes

### 1. Replace generic `_zarr_output_path()`

Current function:

- `src/napari_atlas_cci/Atlas_CCI_Widget.py:413`
- `_zarr_output_path(root_folder, use_downsample)`

Replace with explicit path helpers:

- `_aligned_zarr_path(root_folder)`
- `_aligned_ome_zarr_path(root_folder)`
- `_preview_ome_zarr_path(root_folder)`

Then update callers:

- `_has_preview_zarr_output()` should become `_has_preview_ome_zarr_output()`.
- `_has_aligned_zarr_output()` should become `_has_aligned_zarr_output()` and check the plain aligned Zarr.
- Add `_has_aligned_ome_zarr_output()` if the UI needs to know whether the visualization pyramid exists separately.

### 2. Split alignment from OME-Zarr creation

Current function:

- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1076`
- `_apply_z_shifts_worker(root_folder, use_downsample)`

Refactor into two responsibilities:

```python
def _write_aligned_zarr_worker(
    self,
    root_folder: Path,
) -> tuple[bool, str, dict[str, str] | None]:
    ...
```

This should:

- Read `alignment_results/z_alignment_results.pkl`.
- Call `atlas.io.apply_alignment(..., use_down_sample=False)`.
- Save or preserve the resulting aligned data as `project_name_aligned.zarr`.
- Avoid converting the full result to `np.asarray()` unless `apply_alignment()` gives no direct way to preserve/write the Zarr store.
- Return the plain aligned Zarr path.

Add a separate helper:

```python
def _write_ome_zarr_pyramid_from_zarr(
    self,
    source_zarr_path: Path,
    output_ome_zarr_path: Path,
    *,
    layer_name: str,
) -> tuple[bool, str, dict[str, str | float | list[str]] | None]:
    ...
```

This should:

- Open the plain Zarr array.
- Write an OME-Zarr pyramid with `ome_zarr.writer.write_image()`.
- Use `.tmp` output and rename into place only after metadata validation succeeds.
- Return the OME-Zarr path and display metadata for napari.

For initial/downsample preview, either keep using `apply_alignment(..., use_down_sample=True)` and write only `project_name_downsample.ome.zarr`, or write a temporary plain downsampled Zarr first and then derive the OME-Zarr. The simpler first step is acceptable because the preview is only a visualization artifact.

### 3. Update final alignment button behavior

Current function:

- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1331`
- `finalize_alignement()`

New behavior:

- Run the final alignment and save `project_name_aligned.zarr`.
- Generate or refresh `project_name_aligned.ome.zarr` for napari visualization.
- Mark final alignment complete based on the plain Zarr existing.
- Enable CZI export and WebKnossos upload based on the plain Zarr existing, not the OME-Zarr.

### 4. Update napari display helpers

Current functions:

- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1354`, `_display_zalign_zarr()`
- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1378`, `display_initial_zalign_preview()`
- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1381`, `display_final_zalign_output()`
- `src/napari_atlas_cci/gui.py:212`, `display_ome_zarr()`

Proposed behavior:

- Keep `display_ome_zarr()` because napari should display the OME-Zarr pyramid.
- Rename `_display_zalign_zarr()` to `_display_zalign_ome_zarr()` or similar.
- `display_initial_zalign_preview()` should display `project_name_downsample.ome.zarr`.
- `display_final_zalign_output()` should display `project_name_aligned.ome.zarr`.
- If the final plain Zarr exists but the final OME-Zarr is missing, either show a clear error or regenerate the OME-Zarr before display.

### 5. Update CZI export to read plain Zarr

Current functions:

- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1542`, `_final_zarr_path()`
- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1588`, `export_to_czi()`
- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1621`, `_export_to_czi_worker()`

Proposed changes:

- Rename `_final_zarr_path()` to `_aligned_zarr_path_current_project()` or use `_aligned_zarr_path(Path(self.main_directory))`.
- `export_to_czi()` should check `project_name_aligned.zarr`.
- `_export_to_czi_worker()` should open the plain Zarr array directly:

```python
zarr_array = zarr.open_array(str(aligned_zarr_path), mode="r")
```

Remove this current OME-Zarr lookup:

```python
dataset_path = self._ome_zarr_dataset_path(zarr_path)
zarr_array = zarr.open_array(str(dataset_path), mode="r")
```

### 6. Update WebKnossos upload to read plain Zarr

Current functions:

- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1657`, `upload_to_webknossos()`
- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1682`, `_upload_to_webknossos()`
- `src/napari_atlas_cci/Atlas_CCI_Widget.py:1717`, `_upload_to_webknossos_worker()`

Proposed changes:

- Check `project_name_aligned.zarr`, not the OME-Zarr path.
- In `_upload_to_webknossos_worker()`, read the plain aligned Zarr directly:

```python
source_array = da.from_zarr(str(aligned_zarr_path))
```

Remove this current OME-Zarr lookup:

```python
dataset_path = self._ome_zarr_dataset_path(final_zarr_path)
source_array = da.from_zarr(str(dataset_path))
```

Keep the temporary WebKnossos source Zarr if `convert_zarr()` still requires XYZ axis order:

```python
da.to_zarr(
    source_array.transpose(2, 1, 0),
    str(upload_source_path),
    overwrite=True,
)
```

That temporary Zarr is still acceptable because it is an upload staging artifact, not the canonical aligned output.

### 7. Update UI labels and messages

Current wording often says `Zarr output` when the output is actually OME-Zarr.

Recommended wording:

- Final alignment status: `Final aligned Zarr complete`
- Napari view button tooltip: `Display the final aligned OME-Zarr pyramid`
- Export CZI tooltip: `Export the final aligned Zarr output to CZI`
- Upload tooltip: `Upload the final aligned Zarr output to WebKnossos`
- Missing export/upload error: `Final aligned Zarr output not found.`
- Missing napari display error: `Final aligned OME-Zarr output not found.`

## Functions To Delete

Delete or replace these functions after callers are migrated:

- `_zarr_output_path()` at `src/napari_atlas_cci/Atlas_CCI_Widget.py:413`
  - Replace with explicit plain Zarr and OME-Zarr path helpers.

- `_ome_zarr_dataset_path()` at `src/napari_atlas_cci/Atlas_CCI_Widget.py:1549`
  - CZI export and WebKnossos upload should no longer need OME-Zarr dataset resolution.
  - Napari display already resolves OME-Zarr pyramid paths inside `display_ome_zarr()`.

Potentially rename rather than delete:

- `_has_preview_zarr_output()` at `src/napari_atlas_cci/Atlas_CCI_Widget.py:418`
  - Rename to `_has_preview_ome_zarr_output()`.

- `_display_zalign_zarr()` at `src/napari_atlas_cci/Atlas_CCI_Widget.py:1354`
  - Rename to `_display_zalign_ome_zarr()`.

- `_final_zarr_path()` at `src/napari_atlas_cci/Atlas_CCI_Widget.py:1542`
  - Replace with `_aligned_zarr_path(Path(self.main_directory))`, or rename to clarify that it returns the plain aligned Zarr.

## Checks To Do After Implementation

### Static checks

- Run tests:

```powershell
pytest
```

- Run a syntax/import check if full tests are limited by napari GUI dependencies:

```powershell
python -m compileall src tests
```

- Search for old ambiguous naming:

```powershell
rg -n "_zarr_output_path|_ome_zarr_dataset_path|Final Z alignment Zarr|Zarr output" src README.md
```

### Manual workflow checks

- Run initial Z alignment and confirm the preview creates:

```text
project_name_downsample.ome.zarr
```

- Run final Z alignment and confirm it creates:

```text
project_name_aligned.zarr
project_name_aligned.ome.zarr
```

- Confirm napari preview opens `project_name_downsample.ome.zarr`.
- Confirm napari final view opens `project_name_aligned.ome.zarr`.
- Confirm CZI export works when only `project_name_aligned.zarr` is required.
- Confirm WebKnossos upload works from `project_name_aligned.zarr`.
- Delete or temporarily rename `project_name_aligned.ome.zarr` and verify CZI export still works.
- Delete or temporarily rename `project_name_aligned.zarr` and verify CZI export/upload fail with a clear missing plain Zarr message.

### Data checks

- Confirm the plain aligned Zarr shape is 3D and ordered as ZYX.
- Confirm the OME-Zarr level 0 shape matches the plain aligned Zarr.
- Confirm CZI metadata still uses the expected XY and Z pixel sizes.
- Confirm WebKnossos axis transpose is still correct by checking orientation after upload.

## Compatibility Notes

Existing projects may already contain the old final output:

```text
project_name.zarr
```

That path may actually be an OME-Zarr store. The implementation should ignore old outputs and require users to regenerate final alignment.

The cleaner approach is to regenerate with the new naming, because it avoids guessing whether an old `.zarr` path is plain Zarr or OME-Zarr.
