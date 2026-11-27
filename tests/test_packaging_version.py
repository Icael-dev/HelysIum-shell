import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKGBUILD_PATH = REPO_ROOT / "packaging" / "arch" / "PKGBUILD"
VERSION_PATH = REPO_ROOT / "VERSION"


class TestShellPackagingAndVersion(unittest.TestCase):
    def test_version_file_and_pkgbuild_are_synchronized(self) -> None:
        version = VERSION_PATH.read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

        pkgbuild = PKGBUILD_PATH.read_text(encoding="utf-8")
        pkgver_match = re.search(r"^pkgver=([^\s]+)$", pkgbuild, re.M)
        pkgrel_match = re.search(r"^pkgrel=(\d+)$", pkgbuild, re.M)
        self.assertIsNotNone(pkgver_match)
        self.assertIsNotNone(pkgrel_match)
        self.assertEqual(pkgver_match.group(1), version)
        self.assertGreaterEqual(int(pkgrel_match.group(1)), 1)
        self.assertIn("VERSION", pkgbuild)

        cmake = (REPO_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn("git describe --tags", cmake)
        self.assertIn("${CMAKE_SOURCE_DIR}/VERSION", cmake)

    def test_pkgbuild_license_and_dependencies_are_accurate(self) -> None:
        pkgbuild = PKGBUILD_PATH.read_text(encoding="utf-8")
        self.assertIn("license=('GPL-3.0-only')", pkgbuild)

        if shutil.which("makepkg"):
            srcinfo = subprocess.check_output(
                ["makepkg", "--printsrcinfo"],
                cwd=PKGBUILD_PATH.parent,
                text=True,
            )
            deps = set(re.findall(r"^\tdepends = (.+)$", srcinfo, re.M))
            makedeps = set(re.findall(r"^\tmakedepends = (.+)$", srcinfo, re.M))
            self.assertIn("lm_sensors", deps)
            self.assertIn("qt6-imageformats", deps)
            self.assertIn("quickshell-git", deps)
            self.assertIn("qt6-m3shapes-git", deps)
            self.assertIn("libcava", deps)
            self.assertIn("qt6-shadertools", makedeps)
            self.assertNotIn("qt6-shadertools", deps)
            self.assertIn(f"\tpkgver = {VERSION_PATH.read_text(encoding='utf-8').strip()}", srcinfo)

    def test_no_python_bytecode_tracked_in_git(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git not available")
        tracked = subprocess.check_output(["git", "ls-files"], cwd=REPO_ROOT, text=True).splitlines()
        pyc_files = [p for p in tracked if p.endswith((".pyc", ".pyo")) or "__pycache__/" in p]
        self.assertEqual(pyc_files, [])


if __name__ == "__main__":
    unittest.main()
