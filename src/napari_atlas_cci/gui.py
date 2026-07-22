from pathlib import Path

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