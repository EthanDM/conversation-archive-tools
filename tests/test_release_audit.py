from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


AUDIT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "release_audit.py"
SPEC = importlib.util.spec_from_file_location("release_audit", AUDIT_PATH)
assert SPEC and SPEC.loader
release_audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_audit
SPEC.loader.exec_module(release_audit)


class ReleaseAuditTests(unittest.TestCase):
    def test_repository_files_includes_unignored_additions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            (root / ".gitignore").write_text(".env\n", encoding="utf-8")
            visible = root / "README.md"
            visible.write_text("Public documentation.\n", encoding="utf-8")
            (root / ".env").write_text("secret", encoding="utf-8")

            files = release_audit.repository_files(root)
            self.assertIn(visible, files)
            self.assertNotIn(root / ".env", files)

    def test_accepts_generic_documentation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "README.md"
            document.write_text("Stores data in ~/Library/Application Support/Conversation Archive Tools.\n", encoding="utf-8")
            self.assertEqual(release_audit.audit_paths(root, [document]), [])

    def test_flags_runtime_files_and_private_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "auth.json"
            runtime.write_text("{}", encoding="utf-8")
            document = root / "notes.md"
            home_path = "/" + "Users" + "/example/private"
            token = "gh" + "p_abcdefghijklmnopqrstuvwxyz"
            document.write_text(f"Machine: {home_path}\nToken: {token}\n", encoding="utf-8")

            findings = release_audit.audit_paths(root, [runtime, document])

            self.assertEqual(
                findings,
                [
                    release_audit.Finding(Path("auth.json"), "forbidden runtime-state filename"),
                    release_audit.Finding(Path("notes.md"), "personal home path"),
                    release_audit.Finding(Path("notes.md"), "GitHub token"),
                ],
            )


if __name__ == "__main__":
    unittest.main()
