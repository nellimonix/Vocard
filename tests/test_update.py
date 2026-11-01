import io
import pathlib
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import update


class UpdateTests(unittest.TestCase):
    def test_updater_targets_the_fork(self):
        self.assertEqual(update.GITHUB_API_URL, "https://api.github.com/repos/nellimonix/Vocard/releases/latest")
        self.assertEqual(update.VOCARD_URL, "https://github.com/nellimonix/Vocard/archive/")

    def test_install_preserves_local_runtime_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary).resolve()
            protected = {"settings.json": "local settings", ".env": "local environment", "PENDING_PRODUCTION.md": "local registry", "data/mongo/state": "database", "lavalink/application.yml": "local config", ".git/HEAD": "local git"}
            for name, value in protected.items():
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            (root/"main.py").write_text("old")
            package = io.BytesIO()
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("Vocard-2.7.3/main.py", "new")
                archive.writestr("Vocard-2.7.3/lavalink/application.yml", "default config")
            with patch.object(update, "ROOT_DIR", str(root)), patch("builtins.input", return_value="yes"), redirect_stdout(io.StringIO()):
                update.install(SimpleNamespace(content=package.getvalue()), "v2.7.3")
            self.assertEqual((root/"main.py").read_text(), "new")
            for name, value in protected.items():
                self.assertEqual((root/name).read_text(), value)
            self.assertFalse((root/"Vocard-2.7.3").exists())

    def test_zip_cannot_escape_the_installation_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary).resolve()
            package = io.BytesIO()
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("../outside.py", "invalid")
            with patch.object(update, "ROOT_DIR", str(root)), patch("builtins.input", return_value="yes"), redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    update.install(SimpleNamespace(content=package.getvalue()), "v2.7.3")


if __name__ == "__main__":
    unittest.main()
