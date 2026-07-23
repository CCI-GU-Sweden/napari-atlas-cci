from pathlib import Path
from typing import Any

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem


class LocalFolderTree(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setColumnCount(1)
        self.setHeaderLabels(["Atlas Project"])
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

    def populate_from_project(self, project_root: Path, series_folders: list[Path]) -> None:
        """Display one root item (project) and one child item per S_ folder."""
        self.clear()

        root_item = QTreeWidgetItem([project_root.name])
        root_item.setToolTip(0, str(project_root))
        root_item.setData(0, Qt.ItemDataRole.UserRole, str(project_root))
        root_item.setFlags(root_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)

        for folder in sorted(series_folders, key=lambda p: p.name):
            child_item = QTreeWidgetItem([folder.name])
            child_item.setToolTip(0, str(folder))
            child_item.setData(0, Qt.ItemDataRole.UserRole, str(folder))
            root_item.addChild(child_item)

        self.addTopLevelItem(root_item)
        root_item.setExpanded(True)
        self.resizeColumnToContents(0)


class ZarrImageViewer:
    def __init__(self, viewer: Any):
        self.viewer = viewer

    def remove_layer(self, layer_name: str) -> bool:
        for layer in list(self.viewer.layers):
            if layer.name == layer_name:
                self.viewer.layers.remove(layer)
                return True
        return False

    def display_ome_zarr(
        self,
        ome_zarr_path: Path,
        layer_name: str | None = None,
        contrast_limits: tuple[float, float] | None = None,
    ) -> tuple[bool, str]:
        import dask.array as da
        import zarr

        if not ome_zarr_path.exists():
            return False, f"Zarr output not found: {ome_zarr_path}"

        try:
            root_group = zarr.open_group(str(ome_zarr_path), mode="r")
            multiscales = root_group.attrs.get("multiscales", [])
            if not multiscales:
                return False, "Zarr output has no multiscales metadata."

            datasets = multiscales[0].get("datasets", [])
            if not datasets:
                return False, "Zarr output has no pyramid datasets."

            pyramid = [
                da.from_zarr(str(ome_zarr_path.joinpath(str(dataset["path"]))))
                for dataset in datasets
            ]
            display_name = layer_name or ome_zarr_path.stem

            self.remove_layer(display_name)

            add_kwargs = {
                "multiscale": True,
                "name": display_name,
            }
            if contrast_limits is not None:
                add_kwargs["contrast_limits"] = contrast_limits

            self.viewer.add_image(pyramid, **add_kwargs)
            return True, ""
        except Exception as exc:
            return False, f"Failed to display Zarr output: {exc}"
