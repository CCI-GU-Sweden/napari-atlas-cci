from pathlib import Path
from typing import Any
from uuid import uuid4
import os

from qtpy.QtCore import Qt, QUrl, Signal
from qtpy.QtGui import QDesktopServices
from qtpy.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from qtpy.QtGui import QIntValidator
from qtpy.QtGui import QDoubleValidator

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


class OptionsDialog(QDialog):
    options_applied = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Atlas CCI Options")

        self.main_layout = QVBoxLayout(self)
        self.form_layout = QFormLayout()

        self.thread_count_input = QLineEdit(str(4))
        self.thread_count_input.setToolTip("Number of threads to use for processing.")
        self.thread_count_input.setValidator(QIntValidator(1, 100))
        self.form_layout.addRow("Threads:", self.thread_count_input)

        self.max_shift_pixels_input = QLineEdit(str(500))
        self.max_shift_pixels_input.setToolTip("Maximum shift in pixels for stitching.")
        self.max_shift_pixels_input.setValidator(QIntValidator(0, 1000))
        self.form_layout.addRow("Max shift (px):", self.max_shift_pixels_input)

        self.downsampling_factor_input = QLineEdit(str(10))
        self.downsampling_factor_input.setToolTip("Downsampling factor for alignment.")
        self.downsampling_factor_input.setValidator(QIntValidator(0, 100))
        self.form_layout.addRow("Downsampling factor:", self.downsampling_factor_input)

        self.compression_enabled_input = QCheckBox()
        self.compression_enabled_input.setToolTip("Enable ZSTD compression for CZI output.")
        self.compression_enabled_input.setChecked(True)
        self.compression_enabled_input.toggled.connect(
            self._update_compression_level_enabled
        )
        self.form_layout.addRow("Use CZI compression:", self.compression_enabled_input)

        self.compression_level_input = QLineEdit(str(3))
        self.compression_level_input.setToolTip("Compression level for CZI output as ZSTD compression.")
        self.compression_level_input.setValidator(QIntValidator(0, 22))
        self.form_layout.addRow("ZSTD compression level:", self.compression_level_input)

        self.main_layout.addLayout(self.form_layout)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self._accept_options)
        self.button_box.rejected.connect(self.reject)
        self.main_layout.addWidget(self.button_box)

    def values(self) -> dict:
        if os.cpu_count() is not None:
            if int(self.thread_count_input.text()) >= os.cpu_count():
                max_threads = os.cpu_count() - 1 if os.cpu_count() > 1 else 1
                self.thread_count_input.setText(str(max_threads))
        return {
            "thread_count": int(self.thread_count_input.text()),
            "max_shift_pixels": int(self.max_shift_pixels_input.text()),
            "downsampling_factor": int(self.downsampling_factor_input.text()),
            "compression_enabled": self.compression_enabled_input.isChecked(),
            "compression_level": int(self.compression_level_input.text()),
        }

    def set_values(self, values: dict) -> None:
        self.thread_count_input.setText(str(values.get("thread_count", 4)))
        self.max_shift_pixels_input.setText(str(values.get("max_shift_pixels", 500)))
        self.downsampling_factor_input.setText(str(values.get("downsampling_factor", 10)))
        self.compression_enabled_input.setChecked(
            bool(values.get("compression_enabled", True))
        )
        self.compression_level_input.setText(str(values.get("compression_level", 3)))
        self._update_compression_level_enabled(
            self.compression_enabled_input.isChecked()
        )

    def _accept_options(self) -> None:
        self.options_applied.emit(self.values())
        self.accept()

    def _update_compression_level_enabled(self, enabled: bool) -> None:
        self.compression_level_input.setEnabled(enabled)


class UploadDataDialog(QDialog):
    upload_requested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Upload Data to Webknossos")

        self.main_layout = QVBoxLayout(self)
        self.token_button = QPushButton("Open WebKnossos token page")
        self.token_button.setToolTip("Open the WebKnossos account token page in your browser.")
        self.token_button.clicked.connect(self._open_webknossos_token_page)
        self.main_layout.addWidget(self.token_button)

        self.form_layout = QFormLayout()

        self.xy_pixel_size_input = QLineEdit(str(1.0))  # Can read from the metadata
        self.xy_pixel_size_input.setToolTip("XY pixel size in nanometers.")
        self.xy_pixel_size_input.setValidator(QDoubleValidator(0.001, 100000.0, 3))
        self.form_layout.addRow("XY pixel size (nm):", self.xy_pixel_size_input)

        self.z_pixel_size_input = QLineEdit(str(1.0))  # Can read from the user input in the main UI
        self.z_pixel_size_input.setToolTip("Z pixel size in nanometers.")
        self.z_pixel_size_input.setValidator(QDoubleValidator(0.001, 100000.0, 3))
        self.form_layout.addRow("Z pixel size (nm):", self.z_pixel_size_input)

        self.dataset_name_input = QLineEdit("MyDataset")
        self.dataset_name_input.setToolTip("Name given to the dataset to upload.")
        self.form_layout.addRow("Dataset name:", self.dataset_name_input)

        self.wkn_token_input = QLineEdit()
        self.wkn_token_input.setToolTip("Webknossos API token for authentication.")
        if hasattr(QLineEdit, "EchoMode"):
            self.wkn_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        else:
            self.wkn_token_input.setEchoMode(QLineEdit.Password)
        self.form_layout.addRow("WebKnossos token:", self.wkn_token_input)

        self.main_layout.addLayout(self.form_layout)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self._request_upload)
        self.button_box.rejected.connect(self.reject)
        self.main_layout.addWidget(self.button_box)

    def values(self) -> dict:
        return {
            "xy_pixel_size": float(self.xy_pixel_size_input.text()),
            "z_pixel_size": float(self.z_pixel_size_input.text()),
            "dataset_name": self.dataset_name_input.text(),
            "wkn_token": self.wkn_token_input.text(),
        }

    def set_values(self, values: dict) -> None:
        self.xy_pixel_size_input.setText(str(values.get("xy_pixel_size", 1.0)))
        self.z_pixel_size_input.setText(str(values.get("z_pixel_size", 1.0)))
        self.dataset_name_input.setText(values.get("dataset_name", "MyDataset"))
        self.wkn_token_input.setText(values.get("wkn_token", ""))

    def _request_upload(self) -> None:
        self.upload_requested.emit(self.values())
        self.accept()

    def _open_webknossos_token_page(self) -> None:
        QDesktopServices.openUrl(QUrl("https://webknossos.org/account/token"))


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
            attrs = dict(root_group.attrs)
                    
            multiscales = attrs.get("multiscales", [])
            
            if not multiscales:
                ome_metadata = attrs.get("ome", {})
                if isinstance(ome_metadata, dict):
                    multiscales = ome_metadata.get("multiscales", [])
            
            if not multiscales:
                return False, "Zarr output has no multiscales metadata."

            datasets = multiscales[0].get("datasets", [])
            if not datasets:
                return False, "Zarr output has no pyramid datasets."

            display_name = layer_name or ome_zarr_path.stem
            load_id = uuid4().hex

            pyramid = [
                da.from_zarr(
                    str(ome_zarr_path.joinpath(str(dataset["path"]))),
                    name=f"{display_name}-{load_id}-{idx}",
                )
                for idx, dataset in enumerate(datasets)
            ]

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
