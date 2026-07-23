import napari
import re
import textwrap
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
from typing import cast
import tifffile as tiff
import pandas as pd
import numpy as np
from napari.utils.notifications import show_error
from napari.utils.notifications import show_info
from qtpy.QtCore import Qt, QTimer
from qtpy.QtGui import QBrush, QColor, QDoubleValidator
from qtpy.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QComboBox
)

from .gui import (
    LocalFolderTree,
    OptionsDialog,
    UploadDataDialog,
    ZarrImageViewer,
)

from atlas.io import extract_s_number

#status colors
PROCESSED = QColor("#66CC66")
ONGOING = QColor("#FF9566")
PENDING = QColor("#FFCC66")
FAILED = QColor("#FF3333")
UNPROCESSED = QColor("#BEBEBE")

#TODO: Consider making these parameters adjustable in the UI, possibly through an options menu for stitching parameters.
THREAD_COUNT = 4  # Number of threads for parallel processing (if applicable)
MAX_SHIFT_PIXELS = 500  # Maximum shift in pixels for stitching (if applicable)
AXIAL_PIXEL_SIZE = 0.300
AXIAL_PIXEL_SIZE_UNITS = "µm" #drop down, nm and micron
AXIAL_PIXEL_SIZE_UNITS_OPTIONS = ["nm", "µm"]
DOWNSCALE = 10
ZALIGN_STATUS_MAX_CHARS = 120

class AtlasCCIWidget(QWidget):
    def __init__(self, viewer: napari.Viewer):
        super().__init__()
        self.viewer = viewer
        self.zarr_viewer = ZarrImageViewer(viewer)
        self.main_directory = Path.cwd()
        self.pixel_size = {}

        self.pending_series: set[Path] = set()
        self.ongoing_series: set[Path] = set()
        self.failed_series: set[Path] = set()
        self._is_processing = False
        self._executor: ThreadPoolExecutor | None = None
        self._series_futures: dict[Path, Future] = {}
        self._processing_order: list[Path] = []
        self._next_queue_index = 0
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(150)
        self._status_timer.timeout.connect(self._poll_processing_futures)

        self._zalign_executor = ThreadPoolExecutor(max_workers=1)
        self._zalign_future: Future | None = None
        self._zalign_active_action: str | None = None
        self._zalign_spinner_frames = ("|", "/", "-", "\\")
        self._zalign_spinner_index = 0
        self._zalign_timer = QTimer(self)
        self._zalign_timer.setInterval(120)
        self._zalign_timer.timeout.connect(self._poll_zalign_future)
        self.setWindowTitle("Atlas CCI")

        self.main_layout = QVBoxLayout(self)

        # --- path row ---
        self.path_layout = QHBoxLayout()
        self.path_layout.addWidget(QLabel("Path:"))
        self.path_edit = QLineEdit()
        self.path_edit.setFixedWidth(200)
        self.path_edit.setText(str(self.main_directory))
        self.path_layout.addWidget(self.path_edit)

        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self.browse_for_atlas_project)
        self.path_layout.addWidget(self.browse_btn)

        self.options_button = QPushButton("Options...")
        self.options_button.clicked.connect(self.open_options_dialog)
        self.path_layout.addWidget(self.options_button)

        self.main_layout.addLayout(self.path_layout)

        # Add the local folder tree widget
        self.local_folder_tree = LocalFolderTree()
        self.local_folder_tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        self.main_layout.addWidget(self.local_folder_tree)
    
        # --- Tile stitching row ---
        self.stitching_layout = QHBoxLayout()
        self.stitching_all_btn = QPushButton("Stitch ALL Series")
        self.stitching_all_btn.clicked.connect(self.process_all_series)
        self.stitching_one_btn = QPushButton("Stitch Selected Series")
        self.stitching_one_btn.clicked.connect(self.process_selected_series)
        self.stitching_layout.addWidget(self.stitching_all_btn)
        self.stitching_layout.addWidget(self.stitching_one_btn)
        self.main_layout.addLayout(self.stitching_layout)

        # --- Tile display row ---
        self.tile_display_layout = QHBoxLayout()

        self.display_btn = QPushButton("Display Stitched Image")
        self.display_btn.clicked.connect(self.display_stitched_image)
        self.display_btn.setEnabled(False)
        self.tile_display_layout.addWidget(self.display_btn)

        self.main_layout.addLayout(self.tile_display_layout)

        # --- Z align part ---
        self.z_align_layout_pixel = QVBoxLayout()
        self.pixel_size_input = QHBoxLayout()
        self.axial_pixel_size_input = QLineEdit(str(AXIAL_PIXEL_SIZE)) #pixel size input
        self.axial_pixel_size_input.setFixedWidth(200)
        self.axial_pixel_size_input.setToolTip("Axial pixel size in microns (µm) or nanometers (nm).")
        self.axial_pixel_size_input.setValidator(QDoubleValidator(0.01, 1000.0, 3))
        self.axial_pixel_size_input.textChanged.connect(self.update_pixel_size_from_input)

        self.axial_pixel_unit_dropdown = QComboBox()
        self.axial_pixel_unit_dropdown.addItems(AXIAL_PIXEL_SIZE_UNITS_OPTIONS)
        self.axial_pixel_unit_dropdown.setCurrentText(AXIAL_PIXEL_SIZE_UNITS)
        self.axial_pixel_unit_dropdown.currentTextChanged.connect(self.update_pixel_size_from_input)

        self.pixel_size_input_widget = QWidget()
        self.pixel_size_input.addWidget(self.axial_pixel_size_input)
        self.pixel_size_input.addWidget(self.axial_pixel_unit_dropdown)
        self.pixel_size_input_widget.setLayout(self.pixel_size_input)
        self.z_align_layout_pixel.addWidget(self.pixel_size_input_widget)

        self.initial_zalign_layout = QHBoxLayout()

        self.zalign_zshift_button = QPushButton("Initial Z Alignment")
        self.zalign_zshift_button.setToolTip("Calculate Z shifts and apply a downsampled Z alignment.")
        self.zalign_zshift_button.clicked.connect(self.initial_alignement)
        self.zalign_zshift_button.setEnabled(False)
        self.initial_zalign_layout.addWidget(self.zalign_zshift_button)

        self.zalign_preview_button = QPushButton("Preview")
        self.zalign_preview_button.setToolTip("Display the downsampled Z alignment Zarr output.")
        self.zalign_preview_button.clicked.connect(self.display_initial_zalign_preview)
        self.zalign_preview_button.setEnabled(False)
        self.initial_zalign_layout.addWidget(self.zalign_preview_button)

        self.initial_zalign_widget = QWidget()
        self.initial_zalign_widget.setLayout(self.initial_zalign_layout)
        self.z_align_layout_pixel.addWidget(self.initial_zalign_widget)

        self.correction_layout = QHBoxLayout()
        self.correction_Z_btn = QPushButton("Enter Correction")
        self.correction_Z_btn.setToolTip("Create points layer for Z alignment.")
        self.correction_Z_btn.clicked.connect(self.create_correction_points_layer)
        self.correction_Z_btn.setEnabled(False)

        self.correction_apply_btn = QPushButton("Apply Correction")
        self.correction_apply_btn.setToolTip("Apply manual Z correction to the downsampled preview Zarr output.")
        self.correction_apply_btn.clicked.connect(self.apply_correction)
        self.correction_apply_btn.setEnabled(False)

        self.correction_layout.addWidget(self.correction_Z_btn)
        self.correction_layout.addWidget(self.correction_apply_btn)
        self.correction_widget = QWidget()
        self.correction_widget.setLayout(self.correction_layout)
        self.z_align_layout_pixel.addWidget(self.correction_widget)

        self.final_zalign_layout = QHBoxLayout()

        self.zalign_final_button = QPushButton("Final Z Alignment")
        self.zalign_final_button.setToolTip("Apply previously calculated Z shifts to the stitched series and save as Zarr output.")
        self.zalign_final_button.clicked.connect(self.finalize_alignement)
        self.zalign_final_button.setEnabled(False)
        self.final_zalign_layout.addWidget(self.zalign_final_button)

        self.zalign_view_button = QPushButton("View")
        self.zalign_view_button.setToolTip("Display the final Z alignment Zarr output.")
        self.zalign_view_button.clicked.connect(self.display_final_zalign_output)
        self.zalign_view_button.setEnabled(False)
        self.final_zalign_layout.addWidget(self.zalign_view_button)

        self.final_zalign_widget = QWidget()
        self.final_zalign_widget.setLayout(self.final_zalign_layout)
        self.z_align_layout_pixel.addWidget(self.final_zalign_widget)

        self.zalign_status_label = QLabel("Z Align: [UNPROCESSED] Idle")
        self.zalign_status_label.setWordWrap(True)
        self.zalign_status_label.setMinimumWidth(0)
        self.zalign_status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.z_align_layout_pixel.addWidget(self.zalign_status_label)
        self._set_zalign_status("UNPROCESSED", "Idle", UNPROCESSED)

        self.main_layout.addLayout(self.z_align_layout_pixel)

        self.export_layout = QHBoxLayout()

        self.export_czi_button = QPushButton("Export to CZI")
        self.export_czi_button.setToolTip("Export the final Z alignment Zarr output to CZI format.")
        self.export_czi_button.clicked.connect(self.export_to_czi)
        self.export_czi_button.setEnabled(False)
        self.export_layout.addWidget(self.export_czi_button)

        self.upload_wkn_button = QPushButton("Upload to Webknossos...")
        self.upload_wkn_button.setToolTip("Upload the final Z alignment Zarr output to Webknossos.")
        self.upload_wkn_button.clicked.connect(self.upload_to_webknossos)
        self.upload_wkn_button.setEnabled(False)
        self.export_layout.addWidget(self.upload_wkn_button)

        self.export_widget = QWidget()
        self.export_widget.setLayout(self.export_layout)
        self.main_layout.addWidget(self.export_widget)

        self.refresh_local_folder_tree()

    def search_for_atlas_project(self, path: str|Path) -> list[Path]:
        from atlas.io import get_valid_slice_folders
        series_list, _ = get_valid_slice_folders(Path(path))
        return series_list

    def refresh_local_folder_tree(self) -> None:
        path_text = self.path_edit.text().strip()
        if not path_text:
            show_error("Please provide an atlas project folder path.")
            self.local_folder_tree.clear()
            return

        project_root = Path(path_text).expanduser()
        if not project_root.exists() or not project_root.is_dir():
            show_error("Invalid path. Please provide an existing folder.")
            self.local_folder_tree.clear()
            return

        try:
            series_folders = self.search_for_atlas_project(project_root)
        except OSError as exc:
            show_error(f"Could not read folder: {exc}")
            self.local_folder_tree.clear()
            return

        # Reset transient runtime statuses on project reload.
        self.pending_series.clear()
        self.ongoing_series.clear()
        self.failed_series.clear()

        self.local_folder_tree.populate_from_project(project_root, series_folders)
        self.update_series_status_indicators(project_root)

    def browse_for_atlas_project(self) -> None:
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "Select Atlas Project Folder",
            self.path_edit.text().strip() or str(self.main_directory),
        )
        if not selected_dir:
            return

        self.main_directory = Path(selected_dir)

        self.path_edit.setText(str(self.main_directory))
        self.refresh_local_folder_tree()

    def _series_identifier_candidates(self, series_folder: Path) -> list[str]:
        """Return likely identifiers for output files across naming conventions."""
        candidates: list[str] = [series_folder.name]

        try:
            extracted_id = str(extract_s_number(series_folder.name)).strip()
        except Exception:
            extracted_id = ""

        if extracted_id and extracted_id not in candidates:
            candidates.append(extracted_id)

        if extracted_id.startswith("S_"):
            numeric_part = extracted_id[2:]
            if numeric_part and numeric_part not in candidates:
                candidates.append(numeric_part)
        elif extracted_id:
            prefixed = f"S_{extracted_id}"
            if prefixed not in candidates:
                candidates.append(prefixed)

        match = re.match(r"^S_(\d+)", series_folder.name)
        if match:
            normalized_num = str(int(match.group(1)))
            normalized_prefixed = f"S_{normalized_num}"
            if normalized_num not in candidates:
                candidates.append(normalized_num)
            if normalized_prefixed not in candidates:
                candidates.append(normalized_prefixed)

        return candidates

    def _series_sort_key(self, tiff_path: Path) -> tuple[int, str]:
        """Sort stitched series files robustly across naming convention variants."""
        try:
            extracted = str(extract_s_number(tiff_path)).strip()
        except Exception:
            extracted = tiff_path.stem

        match = re.search(r"S_(\d+)", extracted)
        if match is None:
            match = re.search(r"S_(\d+)", tiff_path.stem)
        if match is None:
            match = re.search(r"(\d+)", extracted)
        if match is None:
            match = re.search(r"(\d+)", tiff_path.stem)

        if match is None:
            return (10**12, tiff_path.name.lower())

        return (int(match.group(1)), tiff_path.name.lower())

    def _has_expected_output_files(self, root_folder: Path, series_folder: Path) -> tuple[bool, bool, bool]:
        """Check if stitched TIFF, phaseCC CSV, and transforms JSON exist for a series."""
        ids = self._series_identifier_candidates(series_folder)

        has_tiff = False
        has_csv = False
        has_json = False

        for series_id in ids:
            if any(root_folder.glob(f"stitched_image_{series_id}.tif*")):
                has_tiff = True
            if any(root_folder.glob(f"phaseCC_stitching_{series_id}.csv")):
                has_csv = True
            if any(root_folder.glob(f"transforms_{series_id}.json")):
                has_json = True

            if has_tiff and has_csv and has_json:
                break

        return has_tiff, has_csv, has_json

    def _get_stitched_image_path(self, root_folder: Path, series_folder: Path) -> Path | None:
        """Return the stitched image path for a series if present."""
        ids = self._series_identifier_candidates(series_folder)
        for series_id in ids:
            tiff_path = root_folder.joinpath(f"stitched_image_{series_id}.tiff")
            if tiff_path.exists():
                return tiff_path

            tif_path = root_folder.joinpath(f"stitched_image_{series_id}.tif")
            if tif_path.exists():
                return tif_path

        return None

    def _evaluate_series_status(self, root_folder: Path, series_folder: Path) -> tuple[str, QColor]:
        if series_folder in self.ongoing_series:
            return "ONGOING", ONGOING
        if series_folder in self.pending_series:
            return "PENDING", PENDING
        if series_folder in self.failed_series:
            return "FAILED", FAILED

        has_tiff, has_csv, has_json = self._has_expected_output_files(root_folder, series_folder)
        has_any_output = has_tiff or has_csv or has_json

        has_mif = any(
            file.is_file() and file.suffix.lower() == ".ve-mif"
            for file in series_folder.iterdir()
        )
        has_raw_tif = any(
            file.is_file() and file.suffix.lower() in {".tif", ".tiff"}
            for file in series_folder.iterdir()
        )

        if has_tiff and has_csv and has_json:
            return "PROCESSED", PROCESSED
        if (has_csv or has_json) and not has_tiff:
            return "FAILED", FAILED
        if has_any_output:
            return "ONGOING", ONGOING
        if has_mif and has_raw_tif:
            return "UNPROCESSED", UNPROCESSED
        return "UNPROCESSED", UNPROCESSED

    def _is_series_processed(self, root_folder: Path, series_folder: Path) -> bool:
        has_tiff, has_csv, has_json = self._has_expected_output_files(root_folder, series_folder)
        return has_tiff and has_csv and has_json

    def _has_z_alignment_results(self, root_folder: Path) -> bool:
        return root_folder.joinpath("alignment_results", "z_alignment_results.pkl").exists()

    def _zarr_output_path(self, root_folder: Path, use_downsample: bool) -> Path:
        expected_base = root_folder.name
        suffix = "_downsample" if use_downsample else ""
        return root_folder.joinpath(f"{expected_base}{suffix}.zarr")

    def _has_preview_zarr_output(self, root_folder: Path) -> bool:
        return self._zarr_output_path(root_folder, use_downsample=True).exists()

    def _has_aligned_zarr_output(self, root_folder: Path) -> bool:
        return self._zarr_output_path(root_folder, use_downsample=False).exists()

    def _all_series_processed(self, root_folder: Path) -> bool:
        root_item = self.local_folder_tree.topLevelItem(0)
        if root_item is None or root_item.childCount() == 0:
            return False

        for idx in range(root_item.childCount()):
            child_item = root_item.child(idx)
            series_path = self._get_series_path_from_item(child_item)
            if series_path is None:
                continue
            if not self._is_series_processed(root_folder, series_path):
                return False

        return True

    def _update_project_leaf_and_zalign_controls(self, root_folder: Path) -> None:
        root_item = self.local_folder_tree.topLevelItem(0)
        if root_item is None:
            return

        root_path_text = root_item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(root_path_text, str) and root_path_text:
            project_name = Path(root_path_text).name
        else:
            project_name = root_folder.name

        all_processed = self._all_series_processed(root_folder)
        has_zcalc = self._has_z_alignment_results(root_folder)
        has_preview_zarr = self._has_preview_zarr_output(root_folder)
        has_zarr = self._has_aligned_zarr_output(root_folder)

        if has_zarr:
            project_state_label = "[Completed]"
            project_state_color = PROCESSED
            project_state_details = "Zarr output present"
        elif has_zcalc:
            project_state_label = "[Z-Calculation READY]"
            project_state_color = ONGOING
            project_state_details = "Z-shift results ready"
        elif all_processed:
            project_state_label = "[Z-Align READY]"
            project_state_color = PENDING
            project_state_details = "All 2D stitching outputs found"
        else:
            project_state_label = "[Waiting 2D Stitch]"
            project_state_color = UNPROCESSED
            project_state_details = "Waiting for all series to be processed"

        root_item.setText(0, f"{project_state_label} {project_name}")
        root_item.setForeground(0, QBrush(project_state_color))
        root_item.setToolTip(
            0,
            (
                f"{root_folder}\n"
                f"State: {project_state_label} {project_state_details}\n"
                f"all stitched: {'yes' if all_processed else 'no'}\n"
                f"z_alignment_results.pkl: {'yes' if has_zcalc else 'no'}\n"
                f"preview zarr output: {'yes' if has_preview_zarr else 'no'}\n"
                f"zarr output: {'yes' if has_zarr else 'no'}"
            ),
        )

        if not self._is_zalign_running():
            self.zalign_zshift_button.setEnabled(all_processed)
            self.zalign_preview_button.setEnabled(has_preview_zarr)
            self.correction_Z_btn.setEnabled(has_zcalc)
            self.correction_apply_btn.setEnabled(has_zcalc)
            self.zalign_final_button.setEnabled(has_zcalc)
            self.zalign_view_button.setEnabled(has_zarr)
            self.export_czi_button.setEnabled(has_zarr)
            self.upload_wkn_button.setEnabled(has_zarr)
            self._update_zalign_status_from_outputs(
                all_processed=all_processed,
                has_zcalc=has_zcalc,
                has_preview_zarr=has_preview_zarr,
                has_zarr=has_zarr,
            )

    def update_series_status_indicators(self, root_folder: Path) -> None:
        """Update status labels/colors for each S_ child in the tree."""
        root_item = self.local_folder_tree.topLevelItem(0)
        if root_item is None:
            return

        for idx in range(root_item.childCount()):
            child_item = root_item.child(idx)
            series_path = self._get_series_path_from_item(child_item)
            if series_path is None:
                continue
            status_text, status_color = self._evaluate_series_status(root_folder, series_path)

            child_item.setText(0, f"[{status_text}] {series_path.name}")
            child_item.setForeground(0, QBrush(status_color))

            has_tiff, has_csv, has_json = self._has_expected_output_files(root_folder, series_path)
            child_item.setToolTip(
                0,
                (
                    f"{series_path}\n"
                    f"Status: {status_text}\n"
                    f"stitched tiff: {'yes' if has_tiff else 'no'}\n"
                    f"phaseCC csv: {'yes' if has_csv else 'no'}\n"
                    f"transform json: {'yes' if has_json else 'no'}"
                ),
            )

        self.local_folder_tree.viewport().update()
        QApplication.processEvents()
        self._update_project_leaf_and_zalign_controls(root_folder)
        self._update_display_button_state(root_folder)

    def _start_processing_queue(self, series_paths: list[Path]) -> None:
        if self._is_processing:
            show_error("Processing is already running.")
            return

        unique_series_paths: list[Path] = []
        for series_path in series_paths:
            normalized_path = Path(series_path)
            if normalized_path not in unique_series_paths:
                unique_series_paths.append(normalized_path)

        root_folder = Path(self.main_directory)
        to_process = [
            series_path
            for series_path in unique_series_paths
            if not self._is_series_processed(root_folder, series_path)
        ]

        if not to_process:
            show_error("Selected series are already processed. Nothing to run.")
            self.update_series_status_indicators(root_folder)
            return

        worker_count = max(1, int(THREAD_COUNT))

        # Queue bookkeeping for status labels.
        self._is_processing = True
        self._processing_order = to_process
        self._next_queue_index = min(worker_count, len(to_process))
        self.pending_series = set(to_process[self._next_queue_index:])
        self.ongoing_series = set(to_process[:self._next_queue_index])
        self.failed_series.difference_update(set(to_process))

        self._executor = ThreadPoolExecutor(max_workers=worker_count)
        self._series_futures = {
            series_path: self._executor.submit(self._process_one_series, series_path)
            for series_path in to_process
        }

        self.update_series_status_indicators(root_folder)
        self._status_timer.start()

    def _poll_processing_futures(self) -> None:
        if not self._series_futures:
            self._finish_processing_queue()
            return

        root_folder = Path(self.main_directory)
        done_series: list[Path] = []

        for series_path, future in self._series_futures.items():
            if not future.done():
                continue

            done_series.append(series_path)
            success = False
            error_message = ""
            try:
                success, error_message = future.result()
            except Exception as exc:
                success = False
                error_message = str(exc)

            self.ongoing_series.discard(series_path)
            self.pending_series.discard(series_path)
            if not success:
                self.failed_series.add(series_path)
                print(f"Series failed for {series_path.name}: {error_message}")

            if self._next_queue_index < len(self._processing_order):
                next_series = self._processing_order[self._next_queue_index]
                self._next_queue_index += 1
                if next_series in self.pending_series:
                    self.pending_series.discard(next_series)
                    self.ongoing_series.add(next_series)

        for series_path in done_series:
            self._series_futures.pop(series_path, None)

        if done_series:
            self.update_series_status_indicators(root_folder)

        if not self._series_futures:
            self._finish_processing_queue()

    def _finish_processing_queue(self) -> None:
        self._status_timer.stop()
        self.pending_series.clear()
        self.ongoing_series.clear()
        self._processing_order = []
        self._next_queue_index = 0
        self._series_futures.clear()

        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

        self._is_processing = False
        self.update_series_status_indicators(Path(self.main_directory))

    def _is_zalign_running(self) -> bool:
        return self._zalign_future is not None and not self._zalign_future.done()

    def _set_zalign_status(self, state: str, details: str, color: QColor) -> None:
        full_text = f"Z Align: [{state}] {details}"
        compact_details = textwrap.shorten(
            " ".join(str(details).split()),
            width=ZALIGN_STATUS_MAX_CHARS,
            placeholder="...",
        )
        self.zalign_status_label.setText(f"Z Align: [{state}] {compact_details}")
        self.zalign_status_label.setToolTip(full_text)
        self.zalign_status_label.setStyleSheet(f"color: {color.name()}; font-weight: 600;")

    def _update_zalign_status_from_outputs(
        self,
        all_processed: bool,
        has_zcalc: bool,
        has_preview_zarr: bool,
        has_zarr: bool,
    ) -> None:
        if has_zarr:
            self._set_zalign_status("PROCESSED", "Final Z alignment complete", PROCESSED)
        elif has_preview_zarr:
            self._set_zalign_status("PROCESSED", "Preview Z alignment complete; final alignment ready", PROCESSED)
        elif has_zcalc:
            self._set_zalign_status("PENDING", "Initial Z alignment complete; final alignment ready", PENDING)
        elif all_processed:
            self._set_zalign_status("PENDING", "Ready for initial Z alignment", PENDING)
        else:
            self._set_zalign_status("UNPROCESSED", "Waiting for 2D stitching outputs", UNPROCESSED)

    def _set_zalign_buttons_busy(self) -> None:
        self.zalign_zshift_button.setEnabled(False)
        self.zalign_preview_button.setEnabled(False)
        self.correction_Z_btn.setEnabled(False)
        self.correction_apply_btn.setEnabled(False)
        self.zalign_final_button.setEnabled(False)
        self.zalign_view_button.setEnabled(False)
        self.export_czi_button.setEnabled(False)
        self.upload_wkn_button.setEnabled(False)
        if self._zalign_active_action == "initial":
            self._set_zalign_status("ONGOING", "Running initial Z alignment", ONGOING)
        elif self._zalign_active_action == "correction":
            self._set_zalign_status("ONGOING", "Applying manual Z correction preview", ONGOING)
        elif self._zalign_active_action == "final":
            self._set_zalign_status("ONGOING", "Running final Z alignment", ONGOING)
        self._zalign_spinner_index = 0
        self._update_zalign_busy_text()
        self._zalign_timer.start()

    def _reset_zalign_buttons_idle(self) -> None:
        self._zalign_timer.stop()
        self.zalign_zshift_button.setText("Initial Z Alignment")
        self.zalign_preview_button.setText("Preview")
        self.correction_Z_btn.setText("Enter Correction")
        self.correction_apply_btn.setText("Apply Correction")
        self.zalign_final_button.setText("Final Z Alignment")
        self.zalign_view_button.setText("View")
        self._update_project_leaf_and_zalign_controls(Path(self.main_directory))

    def open_options_dialog(self) -> None:
        dialog = OptionsDialog(self)
        dialog.set_values(
            {
                "thread_count": THREAD_COUNT,
                "max_shift_pixels": MAX_SHIFT_PIXELS,
                "downsampling_factor": DOWNSCALE,
            }
        )
        dialog.options_applied.connect(self.apply_options)
        self._exec_dialog(dialog)

    def apply_options(self, values: dict) -> None:
        global THREAD_COUNT, MAX_SHIFT_PIXELS, DOWNSCALE

        THREAD_COUNT = int(values["thread_count"])
        MAX_SHIFT_PIXELS = int(values["max_shift_pixels"])
        DOWNSCALE = int(values["downsampling_factor"])
        show_info("Atlas CCI options updated.")

    def _exec_dialog(self, dialog) -> int:
        if hasattr(dialog, "exec"):
            return int(dialog.exec())
        return int(dialog.exec_())

    def _update_zalign_busy_text(self) -> None:
        frame = self._zalign_spinner_frames[self._zalign_spinner_index]
        self._zalign_spinner_index = (self._zalign_spinner_index + 1) % len(self._zalign_spinner_frames)

        if self._zalign_active_action == "initial":
            self.zalign_zshift_button.setText(f"Initial Z Alignment {frame}")
            self.zalign_preview_button.setText("Preview")
            self.correction_apply_btn.setText("Apply Correction")
            self.zalign_final_button.setText("Final Z Alignment")
            self.zalign_view_button.setText("View")
        elif self._zalign_active_action == "correction":
            self.correction_apply_btn.setText(f"Apply Correction {frame}")
            self.zalign_zshift_button.setText("Initial Z Alignment")
            self.zalign_preview_button.setText("Preview")
            self.zalign_final_button.setText("Final Z Alignment")
            self.zalign_view_button.setText("View")
        elif self._zalign_active_action == "final":
            self.zalign_final_button.setText(f"Final Z Alignment {frame}")
            self.zalign_zshift_button.setText("Initial Z Alignment")
            self.zalign_preview_button.setText("Preview")
            self.correction_apply_btn.setText("Apply Correction")
            self.zalign_view_button.setText("View")

    def _poll_zalign_future(self) -> None:
        future = self._zalign_future
        if future is None:
            self._reset_zalign_buttons_idle()
            return

        if not future.done():
            self._update_zalign_busy_text()
            return

        action = self._zalign_active_action
        self._zalign_future = None
        self._zalign_active_action = None
        self._reset_zalign_buttons_idle()

        try:
            success, message, payload = future.result()
        except Exception as exc:
            self._set_zalign_status("FAILED", str(exc), FAILED)
            show_error(f"Z alignment task failed: {exc}")
            return

        if not success:
            self._set_zalign_status("FAILED", message, FAILED)
            show_error(message)
            return

        if action in {"initial", "correction", "final"}:
            typed_payload = cast(dict[str, str | float | list[str]], payload if isinstance(payload, dict) else {})
            if action == "initial":
                axial_value = typed_payload.get("Axial")
                axial_unit = typed_payload.get("Unit")
                value = typed_payload.get("Value")
                if isinstance(axial_value, float) and isinstance(value, float) and isinstance(axial_unit, str):
                    self.pixel_size = {
                        "Value": value,
                        "Axial": axial_value,
                        "Unit": axial_unit,
                    }
            finalize_success, finalize_message = self._finalize_apply_z_shifts(typed_payload)
            if not finalize_success:
                self._set_zalign_status("FAILED", finalize_message, FAILED)
                show_error(finalize_message)
                self.update_series_status_indicators(Path(self.main_directory))
                return
            self.update_series_status_indicators(Path(self.main_directory))
            if action == "initial":
                self._set_zalign_status("PROCESSED", "Initial Z alignment complete", PROCESSED)
            elif action == "correction":
                points_count = int(cast(float, typed_payload.get("points_count", 0)))
                self._set_zalign_status(
                    "PROCESSED",
                    f"Manual correction preview applied from {points_count} point pairs",
                    PROCESSED,
                )
            else:
                self._set_zalign_status("PROCESSED", "Final Z alignment complete", PROCESSED)

    def _get_series_path_from_item(self, item) -> Path | None:
        """Read canonical series path from tree item data, with tooltip fallback."""
        data_path = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data_path, str) and data_path:
            return Path(data_path)

        tooltip_text = item.toolTip(0)
        if tooltip_text:
            first_line = tooltip_text.splitlines()[0].strip()
            if first_line:
                return Path(first_line)

        return None

    def _on_tree_selection_changed(self) -> None:
        self._update_display_button_state(Path(self.main_directory))

    def _update_display_button_state(self, root_folder: Path) -> None:
        selected_items = self.local_folder_tree.selectedItems()
        if len(selected_items) != 1:
            self.display_btn.setEnabled(False)
            return

        series_path = self._get_series_path_from_item(selected_items[0])
        if series_path is None or not series_path.is_dir() or not series_path.name.startswith("S_"):
            self.display_btn.setEnabled(False)
            return

        self.display_btn.setEnabled(self._is_series_processed(root_folder, series_path))

    def _process_one_series(self, series_path: Path) -> tuple[bool, str]:
        from atlas.stitching import stitch_ATLAS_tiles
        import json

        buffer_in_microns = 1
        found_mif = False

        print(f"Processing series folder: {series_path}")
        first_tif_path = next(series_path.glob("*.tif"))
        series_id = extract_s_number(first_tif_path)
        output_tif_path = Path(self.main_directory).joinpath(f"stitched_image_S_{series_id}.tiff")
        output_cc_path = Path(self.main_directory).joinpath(f"phaseCC_stitching_S_{series_id}.csv")
        output_jason_path = Path(self.main_directory).joinpath(f"transforms_S_{series_id}.json")

        for file in series_path.iterdir():
            if file.is_file() and file.suffix.lower() in {".ve-mif"}:
                found_mif = True
                print(f"File with '.ve-mif' extension found: {file.name}")
                mif_file = Path(file)
                print(f"Output TIFF path: {output_tif_path}")
                print(f"Output CSV path: {output_cc_path}")
                print(f"Output JSON path: {output_jason_path}")
                print(f"🔄 Stitching image for {series_id}...")
                
                try:
                    stitched_img, mif_tile_df, transform_dict = stitch_ATLAS_tiles(
                        mif_file,
                        buffer_microns=buffer_in_microns,
                        max_shift_pixels=MAX_SHIFT_PIXELS,
                    )

                    # Save the full image as a TIFF file
                    tiff.imwrite(output_tif_path, np.flipud(stitched_img))
                    
                    mif_tile_df.to_csv(output_cc_path, index=False)

                    # Convert NumPy arrays to lists for JSON compatibility
                    json_ready_dict = {k: v.tolist() for k, v in transform_dict.items()}

                    # Save to JSON file
                    with open(output_jason_path, "w") as f:
                        json.dump(json_ready_dict, f, indent=2)
                    return True, ""
                        
                except Exception as e:
                    # Handle any error
                    print(f"Unexpected error with item {file}: {e}")
                    return False, str(e)

        if not found_mif:
            return False, "No .ve-mif file found in series folder"

        return True, ""

    def process_selected_series(
        self,
        series_paths: list[Path] | None | bool = None,
    ) -> None:
        # QPushButton.clicked emits a bool; ignore it and use tree selection.
        if isinstance(series_paths, bool):
            series_paths = None

        if series_paths is None:
            selected_items = self.local_folder_tree.selectedItems()
            if not selected_items:
                show_error("Please select one or more series folders to process.")
                return

            series_paths = []
            for item in selected_items:
                item_path = self._get_series_path_from_item(item)
                if item_path is not None and item_path.is_dir() and item_path.name.startswith("S_"):
                    series_paths.append(item_path)

            if not series_paths:
                show_error("No valid S_ series folder selected.")
                return

        self._start_processing_queue([Path(series_path) for series_path in series_paths])

    def process_all_series(self) -> None:
        all_series_paths = self.search_for_atlas_project(self.main_directory)
        if not all_series_paths:
            show_error("No S_ series folders found in the atlas project.")
            return

        # Reuse selected-series workflow for consistent behavior.
        self._start_processing_queue(all_series_paths)


    def display_stitched_image(self) -> None:
        selected_items = self.local_folder_tree.selectedItems()
        if not selected_items:
            show_error("Please select one series folder to display.")
            return

        if len(selected_items) > 1:
            show_error("Please select only one series folder to display.")
            return

        series_path = self._get_series_path_from_item(selected_items[0])
        if series_path is None or not series_path.is_dir() or not series_path.name.startswith("S_"):
            show_error("Selected item is not a valid S_ series folder.")
            return

        root_folder = Path(self.main_directory)
        if not self._is_series_processed(root_folder, series_path):
            show_error("Only PROCESSED series can be displayed.")
            return

        series_id = extract_s_number(series_path.name)
        stitched_image_path = self._get_stitched_image_path(root_folder, series_path)

        if stitched_image_path is None:
            show_error(f"Stitched image file does not exist for {series_id}.")
            return

        try:
            stitched_image = tiff.imread(stitched_image_path)
            self.viewer.add_image(stitched_image, name=f"Stitched Image {series_id}")
        except Exception as e:
            show_error(f"Failed to load stitched image: {e}")

    def calculate_z_shifts(self, tif_list_sorted: list[Path], output_root: Path | None = None) -> None:
        from atlas.io import create_empty_folder
        from atlas.alignment import initialize_alignment_df, pairwise_alignment, calculate_cumulative_shifts

        output_path = (output_root if output_root is not None else Path(self.main_directory)).joinpath("alignment_results")
        create_empty_folder(output_path)
        z_align_df = initialize_alignment_df(tif_list_sorted, DOWNSCALE)
        z_align_df = pairwise_alignment(z_align_df)
        z_align_df = calculate_cumulative_shifts(z_align_df)

        z_align_df_path = output_path.joinpath("z_alignment_results.pkl")
        z_align_df.to_pickle(z_align_df_path)

    def _prepare_alignement_worker(
        self,
        root_folder: Path,
        axial_value: float,
        axial_unit: str,
    ) -> tuple[bool, str, dict[str, float | str] | None]:
        csv_candidates = sorted(root_folder.glob("phaseCC_stitching_*.csv"))
        if not csv_candidates:
            csv_candidates = sorted(root_folder.glob("PhaseCC_stitching_*.csv"))

        csv_file = csv_candidates[0] if csv_candidates else None
        if csv_file is None:
            return False, "No PhaseCC CSV file found in the main directory.", None

        df = pd.read_csv(csv_file)
        pix_size_micron = float(AXIAL_PIXEL_SIZE)
        if "PixelSizeMicron" in df.columns:
            pix_size_micron = float(df["PixelSizeMicron"].max())

        tif_list = [
            file
            for file in root_folder.iterdir()
            if file.is_file() and file.name.endswith(".tiff")
        ]
        if not tif_list:
            return False, "No stitched TIFF files found for Z shift calculation.", None

        tif_list_sorted = sorted(tif_list, key=self._series_sort_key)
        self.calculate_z_shifts(tif_list_sorted, output_root=root_folder)

        return (
            True,
            "",
            {
                "Value": pix_size_micron,
                "Axial": float(axial_value),
                "Unit": axial_unit,
            },
        )

    def _apply_z_shifts_worker(
        self,
        root_folder: Path,
        use_downsample: bool,
    ) -> tuple[bool, str, dict[str, str | float | list[str]] | None]:
        from atlas.io import apply_alignment
        from ome_zarr.io import parse_url
        from ome_zarr.scale import Scaler
        from ome_zarr.writer import write_image
        import shutil
        import zarr

        z_align_df_path = root_folder.joinpath("alignment_results", "z_alignment_results.pkl")
        if not z_align_df_path.exists():
            return False, "Z alignment results not found. Please run the alignment first.", None

        z_align_df = pd.read_pickle(z_align_df_path)

        print(z_align_df.head(5))

        internal_zarr_path = self._zarr_output_path(root_folder, use_downsample=False)
        preserved_final_zarr_path = internal_zarr_path.with_name(
            f"{internal_zarr_path.name}.preserved"
        )
        if use_downsample:
            if not internal_zarr_path.exists() and preserved_final_zarr_path.exists():
                preserved_final_zarr_path.rename(internal_zarr_path)
            if internal_zarr_path.exists():
                if preserved_final_zarr_path.exists():
                    shutil.rmtree(preserved_final_zarr_path)
                internal_zarr_path.rename(preserved_final_zarr_path)

        zarr_array = None
        try:
            zarr_array, _ = apply_alignment(
                z_align_df,
                buffer_pixels=20,
                percentile_low=2.0,
                percentile_high=99.95,
                use_down_sample=use_downsample,
            )
            data = np.asarray(zarr_array)
        finally:
            zarr_array = None
            if use_downsample:
                if internal_zarr_path.exists():
                    shutil.rmtree(internal_zarr_path)
                if preserved_final_zarr_path.exists():
                    preserved_final_zarr_path.rename(internal_zarr_path)

        if data.ndim not in (2, 3):
            return False, f"Expected 2D or 3D aligned data, got shape {data.shape}.", None

        # Normalize 3D data to ZYX for writing/viewing.
        if data.ndim == 3:
            z_axis = int(np.argmin(data.shape))
            if z_axis != 0:
                data = np.moveaxis(data, z_axis, 0)

        ome_zarr_path = self._zarr_output_path(root_folder, use_downsample=use_downsample)
        temp_ome_zarr_path = ome_zarr_path.with_name(f"{ome_zarr_path.name}.tmp")
        if temp_ome_zarr_path.exists():
            shutil.rmtree(temp_ome_zarr_path)

        location = parse_url(str(temp_ome_zarr_path), mode="w")
        if location is None:
            return False, f"Could not create OME-Zarr store at {temp_ome_zarr_path}", None

        root_group = zarr.group(store=location.store)

        # Keep Z as a single chunk while chunking Y/X for efficient reads.
        if data.ndim == 3:
            chunk_shape = (
                int(data.shape[0]),
                int(min(512, data.shape[1])),
                int(min(512, data.shape[2])),
            )
            axes = "zyx"
        else:
            chunk_shape = (
                int(min(1024, data.shape[0])),
                int(min(1024, data.shape[1])),
            )
            axes = "yx"

        write_image(
            image=data,
            group=root_group,
            scaler=Scaler(max_layer=4),
            axes=axes,
            storage_options={"chunks": chunk_shape},
        )

        print(f"Wrote OME-Zarr pyramid: {temp_ome_zarr_path}")

        multiscales = root_group.attrs.get("multiscales", [])
        if not multiscales:
            return False, "OME-Zarr file was written, but no multiscales metadata was found.", None

        datasets = multiscales[0].get("datasets", [])
        if not datasets:
            return False, "OME-Zarr file was written, but no pyramid datasets were found.", None

        if ome_zarr_path.exists():
            shutil.rmtree(ome_zarr_path)
        temp_ome_zarr_path.rename(ome_zarr_path)

        if np.issubdtype(data.dtype, np.integer):
            p_low, p_high = np.percentile(data, [1.0, 99.8])
        else:
            p_low, p_high = np.percentile(data, [0.5, 99.5])
        if float(p_low) == float(p_high):
            p_low = float(data.min())
            p_high = float(data.max())

        layer_name = root_folder.name + ("_downsample" if use_downsample else "")

        return (
            True,
            "",
            {
                "ome_zarr_path": str(ome_zarr_path),
                "dataset_paths": [dataset["path"] for dataset in datasets],
                "layer_name": layer_name,
                "p_low": float(p_low),
                "p_high": float(p_high),
            },
        )

    def _initial_alignement_worker(
        self,
        root_folder: Path,
        axial_value: float,
        axial_unit: str,
    ) -> tuple[bool, str, dict[str, str | float | list[str]] | None]:
        prepare_success, prepare_message, prepare_payload = self._prepare_alignement_worker(
            root_folder,
            axial_value,
            axial_unit,
        )
        if not prepare_success or prepare_payload is None:
            return False, prepare_message, None

        apply_success, apply_message, apply_payload = self._apply_z_shifts_worker(
            root_folder,
            use_downsample=True,
        )
        if not apply_success or apply_payload is None:
            return False, apply_message, None

        return True, "", {**prepare_payload, **apply_payload}

    def _apply_correction_worker(
        self,
        root_folder: Path,
        fixed_points: np.ndarray,
        moving_points: np.ndarray,
    ) -> tuple[bool, str, dict[str, str | float | list[str]] | None]:
        from atlas.alignment import correct_z_alignment_from_points
        import shutil

        z_align_df_path = root_folder.joinpath("alignment_results", "z_alignment_results.pkl")
        if not z_align_df_path.exists():
            return False, "Z alignment results not found. Please run initial Z alignment first.", None

        z_align_df = pd.read_pickle(z_align_df_path)
        corrected_z_align_df = correct_z_alignment_from_points(
            z_align_df,
            fixed_points,
            moving_points,
        )
        if corrected_z_align_df is None:
            corrected_z_align_df = z_align_df

        backup_path = z_align_df_path.with_name("z_alignment_results_before_manual_correction.pkl")
        if not backup_path.exists():
            shutil.copy2(z_align_df_path, backup_path)

        corrected_z_align_df.to_pickle(z_align_df_path)

        apply_success, apply_message, apply_payload = self._apply_z_shifts_worker(
            root_folder,
            use_downsample=True,
        )
        if not apply_success or apply_payload is None:
            return False, apply_message, None

        return True, "", {**apply_payload, "points_count": float(len(fixed_points))}

    def _finalize_apply_z_shifts(self, payload: dict[str, str | float | list[str]]) -> tuple[bool, str]:
        try:
            ome_zarr_path = Path(str(payload.get("ome_zarr_path", "")))
            layer_name = str(payload.get("layer_name", "aligned"))
            p_low = float(str(payload.get("p_low", 0.0)))
            p_high = float(str(payload.get("p_high", 1.0)))

            return self.zarr_viewer.display_ome_zarr(
                ome_zarr_path,
                layer_name=layer_name,
                contrast_limits=(p_low, p_high),
            )
        except Exception as exc:
            return False, f"Failed to display aligned image: {exc}"
    
    def update_pixel_size_from_input(self) -> bool:
        try:
            new_pixel_size = float(self.axial_pixel_size_input.text())
            if new_pixel_size <= 0:
                raise ValueError("Pixel size must be positive.")
            self.pixel_size['Axial'] = new_pixel_size

            new_pixel_unit = self.axial_pixel_unit_dropdown.currentText()
            if new_pixel_unit not in AXIAL_PIXEL_SIZE_UNITS_OPTIONS:
                raise ValueError(f"Invalid pixel size unit: {new_pixel_unit}")

            print(f"Updated axial pixel size to: {new_pixel_size} {new_pixel_unit}")
            return True
        except ValueError as e:
            show_error(f"Invalid pixel size input: {e}")
            return False

    def initial_alignement(self):
        if not self.update_pixel_size_from_input():
            return
        if self._is_zalign_running():
            show_error("A Z alignment task is already running.")
            return

        root_folder = Path(self.main_directory)
        if not self._all_series_processed(root_folder):
            show_error("Initial Z alignment requires all series to be processed first.")
            self.update_series_status_indicators(root_folder)
            return

        axial_value = self.pixel_size.get("Axial", AXIAL_PIXEL_SIZE)
        axial_unit = self.axial_pixel_unit_dropdown.currentText()

        self._remove_zalign_image_layer(use_downsample=True)

        self._zalign_active_action = "initial"
        self._zalign_future = self._zalign_executor.submit(
            self._initial_alignement_worker,
            root_folder,
            float(axial_value),
            str(axial_unit),
        )
        self._set_zalign_buttons_busy()

    def finalize_alignement(self):
        if not self.update_pixel_size_from_input():
            return
        if self._is_zalign_running():
            show_error("A Z alignment task is already running.")
            return

        root_folder = Path(self.main_directory)
        if not self._has_z_alignment_results(root_folder):
            show_error("Z alignment results not found. Please run initial Z alignment first.")
            self.update_series_status_indicators(root_folder)
            return

        self._remove_zalign_image_layer(use_downsample=False)

        self._zalign_active_action = "final"
        self._zalign_future = self._zalign_executor.submit(
            self._apply_z_shifts_worker,
            root_folder,
            False,
        )
        self._set_zalign_buttons_busy()

    def _display_zalign_zarr(self, use_downsample: bool) -> None:
        root_folder = Path(self.main_directory)
        zarr_path = self._zarr_output_path(root_folder, use_downsample=use_downsample)
        if not zarr_path.exists():
            output_type = "preview" if use_downsample else "final"
            show_error(f"No {output_type} Z alignment Zarr output found.")
            self.update_series_status_indicators(root_folder)
            return

        success, message = self.zarr_viewer.display_ome_zarr(
            zarr_path,
            layer_name=root_folder.name + ("_downsample" if use_downsample else ""),
        )
        if not success:
            self._set_zalign_status("FAILED", message, FAILED)
            show_error(message)
            return

        self.update_series_status_indicators(root_folder)
        if use_downsample:
            self._set_zalign_status("PROCESSED", "Preview Z alignment displayed", PROCESSED)
        else:
            self._set_zalign_status("PROCESSED", "Final Z alignment displayed", PROCESSED)

    def display_initial_zalign_preview(self) -> None:
        self._display_zalign_zarr(use_downsample=True)

    def display_final_zalign_output(self) -> None:
        self._display_zalign_zarr(use_downsample=False)

    def _remove_zalign_image_layer(self, use_downsample: bool) -> None:
        import gc

        root_folder = Path(self.main_directory)
        layer_name = root_folder.name + ("_downsample" if use_downsample else "")
        if self.zarr_viewer.remove_layer(layer_name):
            QApplication.processEvents()
            gc.collect()

    def _get_viewer_layer(self, name: str):
        for layer in self.viewer.layers:
            if layer.name == name:
                return layer
        return None

    def _is_points_layer(self, layer) -> bool:
        return layer.__class__.__name__ == "Points"

    def _viewer_ndim(self) -> int:
        active_layer = self.viewer.layers.selection.active
        if active_layer is not None and hasattr(active_layer, "ndim"):
            return int(active_layer.ndim)
        return int(self.viewer.dims.ndim)

    def _set_points_layer_style(self, layer, face_color: str) -> None:
        layer.size = 8
        layer.face_color = face_color
        if hasattr(layer, "border_color"):
            layer.border_color = "black"
        elif hasattr(layer, "edge_color"):
            layer.edge_color = "black"

    def _add_empty_points_layer(self, name: str, face_color: str):
        ndim = self._viewer_ndim()
        add_kwargs = {
            "data": None,
            "ndim": ndim,
            "name": name,
            "size": 8,
            "face_color": face_color,
            "border_color": "black",
        }
        try:
            return self.viewer.add_points(**add_kwargs) # pyright: ignore[reportAttributeAccessIssue]
        except TypeError:
            add_kwargs.pop("border_color", None)
            add_kwargs["edge_color"] = "black"
            try:
                return self.viewer.add_points(**add_kwargs) # pyright: ignore[reportAttributeAccessIssue]
            except TypeError:
                add_kwargs.pop("edge_color", None)
                try:
                    return self.viewer.add_points(**add_kwargs) # pyright: ignore[reportAttributeAccessIssue]
                except TypeError:
                    add_kwargs.pop("ndim", None)
                    add_kwargs["data"] = np.empty((0, ndim))
                    return self.viewer.add_points(**add_kwargs) # pyright: ignore[reportAttributeAccessIssue]

    def _get_or_create_correction_points_layer(
        self,
        name: str,
        face_color: str,
    ):
        layer = self._get_viewer_layer(name)
        if layer is None:
            layer = self._add_empty_points_layer(name, face_color)
        else:
            if not self._is_points_layer(layer):
                show_error(f"Layer '{name}' already exists but is not a Points layer.")
                return None
            self._set_points_layer_style(layer, face_color)

        layer.mode = "add"
        return layer

    def create_correction_points_layer(self) -> None:
        root_folder = Path(self.main_directory)
        if not self._has_z_alignment_results(root_folder):
            show_error("Z alignment results not found. Please run initial Z alignment first.")
            self.update_series_status_indicators(root_folder)
            return

        fixed_layer = self._get_or_create_correction_points_layer(
            "Fixed Points",
            "red",
        )
        moving_layer = self._get_or_create_correction_points_layer(
            "Moving Points",
            "blue",
        )
        if fixed_layer is None or moving_layer is None:
            return

        self.viewer.layers.selection.active = fixed_layer
        self._set_zalign_status(
            "PENDING",
            "Add matching points in Fixed Points and Moving Points, then apply correction",
            PENDING,
        )

    def apply_correction(self) -> None:
        if self._is_zalign_running():
            show_error("A Z alignment task is already running.")
            return

        root_folder = Path(self.main_directory)
        if not self._has_z_alignment_results(root_folder):
            show_error("Z alignment results not found. Please run initial Z alignment first.")
            self.update_series_status_indicators(root_folder)
            return

        fixed_layer = self._get_viewer_layer("Fixed Points")
        moving_layer = self._get_viewer_layer("Moving Points")
        if fixed_layer is None or moving_layer is None:
            show_error("Create correction point layers before applying correction.")
            return
        if not self._is_points_layer(fixed_layer) or not self._is_points_layer(moving_layer):
            show_error("Fixed Points and Moving Points must be napari Points layers.")
            return

        fixed_points = np.asarray(fixed_layer.data, dtype=float)
        moving_points = np.asarray(moving_layer.data, dtype=float)

        print(f"Applying correction with {len(fixed_points)} fixed points and {len(moving_points)} moving points.")
        print(f"Fixed points: {fixed_points}")
        print(f"Moving points: {moving_points}")

        if fixed_points.ndim != 2 or moving_points.ndim != 2:
            show_error("Correction point layers must contain coordinate arrays.")
            return

        if len(fixed_points) == 0:
            show_error("Add at least one correction point pair before applying correction.")
            return

        if len(fixed_points) != len(moving_points):
            show_error("The number of points in the Fixed Points layer and the Moving Points layer must be the same.")
            return

        if fixed_points.shape != moving_points.shape:
            show_error("Fixed and Moving correction points must have the same dimensionality.")
            return

        self._remove_zalign_image_layer(use_downsample=True)

        self._zalign_active_action = "correction"
        self._zalign_future = self._zalign_executor.submit(
            self._apply_correction_worker,
            root_folder,
            fixed_points.copy(),
            moving_points.copy(),
        )
        self._set_zalign_buttons_busy()

        #delete the points layers after applying correction
        self.viewer.layers.remove(fixed_layer)
        self.viewer.layers.remove(moving_layer)

    def _final_zarr_path(self) -> Path:
        return self._zarr_output_path(Path(self.main_directory), use_downsample=False)

    def _aligned_czi_path(self) -> Path:
        root_folder = Path(self.main_directory)
        return root_folder.joinpath(f"{root_folder.name}_aligned.czi")

    def _ome_zarr_dataset_path(self, ome_zarr_path: Path, level: int = 0) -> Path:
        import zarr

        root_group = zarr.open_group(str(ome_zarr_path), mode="r")
        multiscales = root_group.attrs.get("multiscales", [])
        if not multiscales:
            raise ValueError("Zarr output has no multiscales metadata.")

        datasets = multiscales[0].get("datasets", [])
        if not datasets:
            raise ValueError("Zarr output has no pyramid datasets.")
        if level >= len(datasets):
            raise ValueError(f"Zarr output has no pyramid level {level}.")

        return ome_zarr_path.joinpath(str(datasets[level]["path"]))

    def _axial_pixel_size_nm(self) -> float:
        axial_value = float(self.axial_pixel_size_input.text())
        axial_unit = self.axial_pixel_unit_dropdown.currentText()
        if axial_unit == "µm":
            return axial_value * 1000.0
        return axial_value

    def export_to_czi(self) -> Path | None:
        from pylibCZIrw import czi as pyczi
        import zarr

        zarr_path = self._final_zarr_path()
        if not zarr_path.exists():
            show_error("Final Z alignment Zarr output not found.")
            return None

        if not self.update_pixel_size_from_input():
            return None

        try:
            dataset_path = self._ome_zarr_dataset_path(zarr_path)
            zarr_array = zarr.open_array(str(dataset_path), mode="r")
            if len(zarr_array.shape) != 3:
                raise ValueError(f"Expected a 3D OME-Zarr dataset, got shape {zarr_array.shape}")

            czi_path = self._aligned_czi_path()
            pix_xy = float(self.pixel_size.get("Value", 1.0))
            pix_z = float(self.pixel_size.get("Axial", AXIAL_PIXEL_SIZE))

            with pyczi.create_czi(czi_path, exist_ok=True) as czidoc_w:
                for frame in range(zarr_array.shape[0]):
                    tmp_plane = np.asarray(zarr_array[frame, :, :]).squeeze()
                    czidoc_w.write(data=tmp_plane[..., np.newaxis], plane={"Z": frame})

                czidoc_w.write_metadata(
                    document_name=czi_path.stem,
                    channel_names={0: "White"},
                    scale_x=pix_xy * 10 ** -6,
                    scale_y=pix_xy * 10 ** -6,
                    scale_z=pix_z * 10 ** -6,
                )
        except Exception as exc:
            show_error(f"Failed to export CZI: {exc}")
            return None

        show_info(f"Exported CZI: {czi_path}")
        return czi_path

    def upload_to_webknossos(self) -> None:
        final_zarr_path = self._final_zarr_path()
        if not final_zarr_path.exists():
            show_error("Final Z alignment Zarr output not found.")
            return

        if not self.update_pixel_size_from_input():
            return

        dialog = UploadDataDialog(self)
        dialog.set_values(
            {
                "xy_pixel_size": 1.0,
                "z_pixel_size": self._axial_pixel_size_nm(),
                "dataset_name": Path(self.main_directory).name,
                "wkn_token": "",
            }
        )
        dialog.upload_requested.connect(self._upload_to_webknossos)
        self._exec_dialog(dialog)

    def _upload_to_webknossos(self, values: dict) -> None:
        import dask.array as da
        import tempfile
        from upath import UPath
        from webknossos import Dataset, webknossos_context
        from webknossos.cli.convert_zarr import convert_zarr
        from webknossos.dataset.defaults import DEFAULT_CHUNK_SHAPE, DEFAULT_SHARD_SHAPE
        from webknossos.dataset.sampling_modes import SamplingModes
        from webknossos.dataset_properties import DataFormat, VoxelSize
        from webknossos.geometry.mag import Mag

        token = str(values.get("wkn_token", "")).strip()
        dataset_name = str(values.get("dataset_name", "")).strip()
        if not token:
            show_error("Please provide a WebKnossos API token.")
            return
        if not dataset_name:
            show_error("Please provide a WebKnossos dataset name.")
            return

        final_zarr_path = self._final_zarr_path()
        if not final_zarr_path.exists():
            show_error("Final Z alignment Zarr output not found.")
            return

        voxel_size = (
            1.0,
            1.0,
            float(values.get("z_pixel_size", self._axial_pixel_size_nm())),
        )

        try:
            with tempfile.TemporaryDirectory(prefix="atlas_cci_wk_") as temp_dir:
                temp_root = Path(temp_dir)
                upload_source_path = temp_root.joinpath(f"{dataset_name}_source.zarr")
                output_path = temp_root.joinpath(dataset_name)

                dataset_path = self._ome_zarr_dataset_path(final_zarr_path)
                source_array = da.from_zarr(str(dataset_path))
                if source_array.ndim != 3:
                    raise ValueError(f"Expected 3D Zarr data, got shape {source_array.shape}.")

                # OME-Zarr is written as zyx. WebKnossos conversion expects xyz.
                da.to_zarr(
                    source_array.transpose(2, 1, 0),
                    str(upload_source_path),
                    overwrite=True,
                )

                with webknossos_context(token=token):
                    convert_zarr(
                        source_zarr_path=UPath(upload_source_path),
                        target_path=UPath(output_path),
                        layer_name="color",
                        data_format=DataFormat.Zarr3,
                        chunk_shape=DEFAULT_CHUNK_SHAPE,
                        shard_shape=DEFAULT_SHARD_SHAPE,
                        is_segmentation_layer=False,
                        voxel_size_with_unit=VoxelSize(voxel_size),
                        compress=True,
                    )

                    dataset = Dataset(output_path, voxel_size=voxel_size, exist_ok=True)
                    dataset.downsample(
                        sampling_mode=SamplingModes.ANISOTROPIC,
                        coarsest_mag=Mag(32),
                    )

                    print(f"Uploading dataset to WebKnossos from {output_path}...")
                    remote_dataset = dataset.upload()
                    show_info(f"Successfully uploaded {remote_dataset.url}")
                    print(f"Successfully uploaded {remote_dataset.url}")
        except Exception as exc:
            show_error(f"Failed to upload to WebKnossos: {exc}")
            return

        self.update_series_status_indicators(Path(self.main_directory))
