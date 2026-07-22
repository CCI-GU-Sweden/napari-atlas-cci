import napari
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
import tifffile as tiff
import xmltodict
from dateutil import parser
import pandas as pd
import numpy as np
from napari.utils.notifications import show_error
from qtpy.QtCore import Qt, QTimer
from qtpy.QtGui import QBrush, QColor
from qtpy.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .gui import (
    LocalFolderTree,
)

#status colors
PROCESSED = QColor("#66CC66")
ONGOING = QColor("#FF9566")
PENDING = QColor("#FFCC66")
FAILED = QColor("#FF3333")
UNPROCESSED = QColor("#BEBEBE")

THREAD_COUNT = 4  # Number of threads for parallel processing (if applicable)

class AtlasCCIWidget(QWidget):
    def __init__(self, viewer: napari.Viewer):
        super().__init__()
        self.viewer = viewer
        self.main_directory = Path.cwd()
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
        self.setWindowTitle("Atlas CCI")

        self.main_layout = QVBoxLayout(self)

        # --- path row ---
        self.path_layout = QHBoxLayout()
        self.path_layout.addWidget(QLabel("Path:"))
        self.path_edit = QLineEdit()
        self.path_edit.setFixedWidth(200)
        self.path_edit.setText(str(self.main_directory))
        self.path_layout.addWidget(self.path_edit)

        self.browse_btn = QPushButton("Browse")
        self.browse_btn.clicked.connect(self.browse_for_atlas_project)
        self.path_layout.addWidget(self.browse_btn)

        self.main_layout.addLayout(self.path_layout)

        # Add the local folder tree widget
        self.local_folder_tree = LocalFolderTree()
        self.local_folder_tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        self.main_layout.addWidget(self.local_folder_tree)

        self.refresh_local_folder_tree()
    
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

        # --- Z align row ---


    def search_for_atlas_project(self, path: str|Path, debug: bool = False) -> list[Path]:
        """
        Search for S_ series folders that contain at least one tif/tiff image.
        """
        series_list = []
        for folder in Path(path).iterdir():  # Iterate over all items in the folder
            if folder.is_dir() and folder.name.startswith("S_"):
                tif_files = [
                    file
                    for file in folder.iterdir()
                    if file.is_file() and file.suffix.lower() in {".tif", ".tiff"}
                ]
                if tif_files:
                    if debug:
                        print(f"Found series folder: {folder.name} (contains {len(tif_files)} .tif files)")
                    series_list.append(folder)

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

        if not self.check_for_ve_tie_files(project_root):
            proceed = QMessageBox.warning(
                self,
                "Potentially Invalid Atlas Project",
                "No ve-tie file detected. The folder may not be a valid atlas project. "
                "Do you want to proceed?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if proceed != QMessageBox.StandardButton.Yes:
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

    def check_for_ve_tie_files(self, main_path: Path) -> bool:
        for file in main_path.iterdir():
            if file.is_file() and file.name.lower().endswith(".ve-tie"):
                return True
        return False

    def _series_identifier_candidates(self, series_folder: Path) -> list[str]:
        """Use only the full series folder name as identifier."""
        return [series_folder.name]

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

    def calculate_mask_roi(self, mask):
        """
        Compute the tight bounding box (ROI) around valid pixels in a boolean mask.

        Parameters
        ----------
        mask : np.ndarray (bool)
            Boolean array where True marks valid pixels and False invalid pixels.

        Returns
        -------
        x0 : int
            Left (minimum column index) of the ROI (inclusive).
        x1 : int
            Right (maximum column index) of the ROI (exclusive, suitable for slicing).
        y0 : int
            Top (minimum row index) of the ROI (inclusive).
        y1 : int
            Bottom (maximum row index) of the ROI (exclusive, suitable for slicing).

        Raises
        ------
        ValueError
            If the mask contains no valid (True) pixels.

        Notes
        -----
        - To crop an image `img` using this ROI, use:

            `img_cropped = img[y0:y1, x0:x1]`

        (row = y, col = x).
        """
        assert isinstance(mask, np.ndarray), "mask must be a numpy array"
        assert mask.dtype == bool, "mask must be a boolean array"

        xs, ys = np.nonzero(mask)
        if ys.size == 0:
            raise ValueError("Mask contains no valid (True) pixels.")

        y0, y1 = ys.min(), ys.max() + 1  # +1 to make it slice-exclusive
        x0, x1 = xs.min(), xs.max() + 1

        return x0, x1, y0, y1


    def _process_one_series(self, series_path: Path) -> tuple[bool, str]:
        from atlas.stitching import add_tile_overlap_columns, match_tiles, build_adjacency_matrix_from_costs, build_transform_dict_from_mst, apply_transforms_and_stitch
        from atlas.stitching import get_tiles_dataframe, mask_low_and_saturation
        import json
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import minimum_spanning_tree

        buffer_in_microns = 1
        max_shift_in_pixels = 500
        found_mif = False

        print(f"Processing series folder: {series_path}")

        for file in series_path.iterdir():
            if file.is_file() and file.suffix.lower() in {".ve-mif"}:
                found_mif = True
                print(f"File with '.ve-mif' extension found: {file.name}")
                mif_file = file
                mif_tile_df = get_tiles_dataframe(mif_file, buffer_microns=buffer_in_microns)
                series_id = series_path.name

                # Define the output file path
                output_tif_path = Path(self.main_directory).joinpath(f"stitched_image_{series_id}.tiff")
                output_cc_path = Path(self.main_directory).joinpath(f"phaseCC_stitching_{series_id}.csv")
                output_jason_path = Path(self.main_directory).joinpath(f"transforms_{series_id}.json")
                print(f"Output TIFF path: {output_tif_path}")
                print(f"Output CSV path: {output_cc_path}")
                print(f"Output JSON path: {output_jason_path}")
                if output_tif_path.exists():
                    print(f"✅ Skipping: {output_tif_path.name} already exists.")
                else:
                    print(f"🔄 Stitching image for {series_id}...")
                
                try:
                    mif_tile_df = add_tile_overlap_columns(mif_tile_df)
                    # add info to the DF so we know where to find the images after they have been moved out of the scope
                    mif_tile_df['raw_data_folder'] = Path(series_path)  # type: ignore[assignment]

                    # Calculate the costs of matching each tile to those that it overlaps with
                    n = len(mif_tile_df)
                    all_costs = []
                    all_shifts = []

                    for current_idx in range(n):
                        costs, shifts = match_tiles(mif_tile_df, reference_idx=current_idx, min_overlap_percent = 2,)
                        # here I do the max displacement rule
                        for i, shift_i in enumerate(shifts):
                            d = np.linalg.norm(shift_i)
                            if d > max_shift_in_pixels:
                                costs[i] = 0.9
                                shifts[i] = np.array([0.0, 0.0])
                        
                        all_costs.append(costs)
                        all_shifts.append(shifts)

                    mif_tile_df["stitching_costs"] = all_costs
                    mif_tile_df["stitching_shifts"] = all_shifts

                    # Step 1: Build the cost matrix which I will use as adjacency for the min span tree
                    adj_matrix = build_adjacency_matrix_from_costs(mif_tile_df, cost_column='stitching_costs')

                    # Step 2: Create sparse matrix and compute MST
                    graph_sparse = csr_matrix(adj_matrix)
                    mst = minimum_spanning_tree(graph_sparse)
                    # The MST will be used to calculate the transofrmation matrices between each tile and a reference tile.
                    # For the moment I just pick 0 as reference but maybe there is a better way, in general I dont think it matters much.                
                    transform_dict = build_transform_dict_from_mst(mif_tile_df, mst, reference_tile=0)
                    
                    # apply transform, user inputs are transform_dict and mif_tile_df, output is the stitched_img
                    stitched_img = apply_transforms_and_stitch(mif_tile_df, transform_dict, reference_tile=0)
                    
                    # check if there is large dark areas around the obj
                    mask_valid = ~mask_low_and_saturation(stitched_img)
                    x0, x1, y0, y1 = self.calculate_mask_roi(mask_valid)
                    if stitched_img is not None:
                        crop_img = stitched_img[x0:x1, y0:y1]

                    # Save the full image as a TIFF file
                    #tiff.imwrite(output_tif_path, np.flipud(stitched_img))
                    tiff.imwrite(output_tif_path, np.flipud(crop_img))
                    
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

        series_id = series_path.name
        stitched_image_path = self._get_stitched_image_path(root_folder, series_path)

        if stitched_image_path is None:
            show_error(f"Stitched image file does not exist for {series_id}.")
            return

        try:
            stitched_image = tiff.imread(stitched_image_path)
            self.viewer.add_image(stitched_image, name=f"Stitched Image {series_id}")
        except Exception as e:
            show_error(f"Failed to load stitched image: {e}")
            