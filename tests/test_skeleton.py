import unittest
from datetime import datetime, timedelta, timezone
import os
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from storage_dashboard.classifier import (
    CLASS_CAUTION,
    CLASS_DO_NOT_TOUCH,
    CLASS_SAFE_TO_REVIEW,
    UNKNOWN_CATEGORY,
    classify_path,
)
from storage_dashboard.scanner import DENIED_ROOTS, DEFAULT_ROOTS, ScanOptions, normalize_roots, scan
from storage_dashboard.server import run
from storage_dashboard.store import state_file_path


class SkeletonTests(unittest.TestCase):
    def test_scanner_defaults_are_read_only_and_skip_symlinks(self) -> None:
        options = ScanOptions()

        self.assertTrue(options.read_only)
        self.assertFalse(hasattr(options, "follow_symlinks"))
        self.assertEqual(
            DEFAULT_ROOTS,
            (
                Path.home() / "Downloads",
                Path.home() / "Desktop",
                Path.home() / "Documents",
                Path.home() / "Movies",
                Path.home() / "Pictures",
            ),
        )
        self.assertIn(Path("/System"), DENIED_ROOTS)

    def test_roots_are_expanded_without_resolution(self) -> None:
        roots = normalize_roots(("~", "/tmp"))

        self.assertEqual(roots[0], Path.home())
        self.assertEqual(roots[1], Path("/tmp"))

    def test_scanner_emits_progress_for_roots_and_paths(self) -> None:
        events = []
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            folder = root / "folder"
            folder.mkdir()
            (folder / "file.txt").write_text("data")

            result = scan(ScanOptions(roots=(root,)), progress=events.append)

        phases = [event["phase"] for event in events]
        self.assertIn("starting", phases)
        self.assertIn("root", phases)
        self.assertIn("directory", phases)
        self.assertIn("file", phases)
        self.assertIn("complete", phases)
        self.assertTrue(any(event.get("active_path") == str(folder) for event in events))
        self.assertEqual(events[-1]["entries_seen"], len(result.entries))

    def test_unknown_classifier_defaults_to_do_not_touch(self) -> None:
        classification = classify_path("example.txt")

        self.assertEqual(classification.classification, UNKNOWN_CATEGORY)
        self.assertEqual(classification.classification, CLASS_DO_NOT_TOUCH)
        self.assertEqual(classification.risk, "high")

    def test_classifier_safe_download_examples(self) -> None:
        classification = classify_path(
            Path.home() / "Downloads" / "installer.dmg",
            root=Path.home() / "Downloads",
            is_dir=False,
            size_bytes=1024,
            modified_at=datetime.now(timezone.utc) - timedelta(days=31),
        )

        self.assertEqual(classification.classification, CLASS_SAFE_TO_REVIEW)

    def test_recent_download_archives_are_caution(self) -> None:
        classification = classify_path(
            Path.home() / "Downloads" / "archive.zip",
            root=Path.home() / "Downloads",
            is_dir=False,
            size_bytes=1024,
            modified_at=datetime.now(timezone.utc) - timedelta(days=1),
        )

        self.assertEqual(classification.classification, CLASS_CAUTION)

    def test_classifier_caution_examples(self) -> None:
        classification = classify_path(Path.home() / "Pictures" / "Photos Library.photoslibrary", is_dir=True)

        self.assertEqual(classification.classification, CLASS_CAUTION)

    def test_classifier_uses_source_marker_metadata_without_filesystem_probe(self) -> None:
        classification = classify_path("project", is_dir=True, source_marker_present=True)

        self.assertEqual(classification.classification, CLASS_CAUTION)

    def test_scanner_reports_denied_roots_without_traversal(self) -> None:
        result = scan(ScanOptions(roots=(Path("/System"),)))

        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].classification, CLASS_DO_NOT_TOUCH)
        self.assertEqual(result.logs[0].event, "denied_root")

    def test_scanner_skips_symlinked_directories_and_logs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "kept.txt").write_text("data")
            link = root / "linked"
            link.symlink_to(target, target_is_directory=True)

            result = scan(ScanOptions(roots=(root,)))

        self.assertTrue(any(log.event == "symlink_skipped" for log in result.logs))
        self.assertFalse(any(entry.path.name == "linked" for entry in result.entries))

    def test_scanner_skips_symlinked_files_and_logs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.txt"
            target.write_text("data")
            link = root / "linked.txt"
            link.symlink_to(target)

            result = scan(ScanOptions(roots=(root,)))

        self.assertTrue(any(log.event == "symlink_skipped" for log in result.logs))
        self.assertFalse(any(entry.path.name == "linked.txt" for entry in result.entries))

    def test_denied_root_matching_normalizes_parent_segments(self) -> None:
        result = scan(ScanOptions(roots=(Path("/tmp/../System"),)))

        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].classification, CLASS_DO_NOT_TOUCH)
        self.assertEqual(result.logs[0].event, "denied_root")

    def test_symlinked_root_to_denied_target_is_skipped_not_followed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            link = root / "usr-link"
            link.symlink_to(Path("/usr"), target_is_directory=True)

            result = scan(ScanOptions(roots=(link,)))

        self.assertEqual(result.entries, ())
        self.assertTrue(any(log.event == "symlink_skipped" for log in result.logs))

    def test_scanner_logs_permission_errors_and_continues(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "ok.txt").write_text("data")

            with patch("storage_dashboard.scanner.os.listdir", side_effect=PermissionError("no access")):
                result = scan(ScanOptions(roots=(root,)))

        self.assertEqual(result.entries[0].path, root)
        self.assertEqual(result.entries[0].file_count, 0)
        self.assertTrue(any(log.event == "permission_denied" for log in result.logs))

    def test_scanner_folder_totals_include_nested_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            (root / "one.bin").write_bytes(b"123")
            (nested / "two.bin").write_bytes(b"4567")

            result = scan(ScanOptions(roots=(root,)))

        root_entry = next(entry for entry in result.entries if entry.path == root)
        nested_entry = next(entry for entry in result.entries if entry.path.name == "nested")
        self.assertEqual(root_entry.size_bytes, 7)
        self.assertEqual(root_entry.file_count, 2)
        self.assertEqual(nested_entry.size_bytes, 4)
        self.assertEqual(nested_entry.file_count, 1)

    def test_scanner_passes_source_marker_presence_from_safe_directory_listing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            project.mkdir()
            (project / "package.json").write_text("{}")

            result = scan(ScanOptions(roots=(root,)))

        project_entry = next(entry for entry in result.entries if entry.path.name == "project")
        self.assertEqual(project_entry.classification, CLASS_CAUTION)
        self.assertEqual(project_entry.reason, "source code project")


    def test_scanner_measures_excluded_folder_without_exploding_results(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node_modules = root / "node_modules"
            nested = node_modules / "package"
            nested.mkdir(parents=True)
            (node_modules / "index.js").write_bytes(b"123")
            (nested / "nested.js").write_bytes(b"4567")

            result = scan(ScanOptions(roots=(root,)))

        node_modules_entry = next(entry for entry in result.entries if entry.path.name == "node_modules")
        self.assertEqual(node_modules_entry.size_bytes, 7)
        self.assertEqual(node_modules_entry.file_count, 2)
        self.assertEqual(node_modules_entry.classification, CLASS_SAFE_TO_REVIEW)
        self.assertFalse(any(entry.path.name == "nested.js" for entry in result.entries))

    def test_scanner_skips_symlinks_inside_collapsed_measured_folder(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node_modules = root / "node_modules"
            node_modules.mkdir()
            target = root / "target.txt"
            target.write_bytes(b"123456789")
            (node_modules / "real.js").write_bytes(b"12")
            (node_modules / "linked.js").symlink_to(target)

            result = scan(ScanOptions(roots=(root,)))

        node_modules_entry = next(entry for entry in result.entries if entry.path.name == "node_modules")
        self.assertEqual(node_modules_entry.size_bytes, 2)
        self.assertEqual(node_modules_entry.file_count, 1)
        self.assertTrue(any(log.event == "symlink_skipped" for log in result.logs))

    def test_scanner_passes_modified_time_for_download_archive_age(self) -> None:
        with TemporaryDirectory() as temp_dir:
            downloads = Path(temp_dir) / "Downloads"
            downloads.mkdir()
            old_archive = downloads / "old.zip"
            recent_archive = downloads / "recent.zip"
            old_archive.write_bytes(b"old")
            recent_archive.write_bytes(b"recent")
            old_timestamp = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
            recent_timestamp = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
            os.utime(old_archive, (old_timestamp, old_timestamp))
            os.utime(recent_archive, (recent_timestamp, recent_timestamp))

            result = scan(ScanOptions(roots=(downloads,)))

        by_name = {entry.path.name: entry for entry in result.entries}
        self.assertEqual(by_name["old.zip"].classification, CLASS_SAFE_TO_REVIEW)
        self.assertEqual(by_name["recent.zip"].classification, CLASS_CAUTION)

    def test_store_path_is_local_user_state(self) -> None:
        self.assertEqual(state_file_path().name, "dashboard.json")
        self.assertIn(".storage-dashboard", state_file_path().parts)

    def test_server_rejects_non_loopback_host(self) -> None:
        with self.assertRaises(ValueError):
            run(host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
