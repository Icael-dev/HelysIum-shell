import os
import platform
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
HOST_ARCH = platform.machine()

DEFAULT_SRCINFO = (
    "pkgbase = helysium-shell\n"
    "\tpkgver = 0.1.0\n"
    "\tpkgrel = 1\n"
    "\tdepends = aubio\n"
    "\tdepends = libcava\n"
    "\tdepends = libqalculate\n"
    "\tdepends = lm_sensors\n"
    "\tdepends = pipewire\n"
    "\tdepends = qt6-base\n"
    "\tdepends = qt6-declarative\n"
    "\tdepends = qt6-imageformats\n"
    "\tdepends = qt6-m3shapes-git\n"
    "\tdepends = qt6-multimedia\n"
    "\tdepends = quickshell-git\n"
    "\tmakedepends = cmake>=3.19\n"
    "\tmakedepends = git\n"
    "\tmakedepends = ninja\n"
    "\tmakedepends = pkgconf\n"
    "\tmakedepends = qt6-shadertools\n"
    "\toptdepends = helysium-cli: CLI integration\n"
    "pkgname = helysium-shell\n"
)


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TestShellInstallSh(unittest.TestCase):
    def _stage_isolated_repo(self, tmp: Path) -> tuple[Path, Path]:
        repo = tmp / "repo"
        pkg_dir = repo / "packaging" / "arch"
        pkg_dir.mkdir(parents=True)
        script = repo / "install.sh"
        shutil.copy2(INSTALL_SH, script)
        shutil.copy2(REPO_ROOT / "packaging" / "arch" / "PKGBUILD", pkg_dir / "PKGBUILD")
        return script, pkg_dir

    def _make_fake_env(
        self,
        tmp: Path,
        *,
        include_bins: tuple[str, ...] = ("pacman", "sudo", "git", "makepkg"),
        base_devel_installed: bool = True,
        printsrcinfo_out: str = DEFAULT_SRCINFO,
        printsrcinfo_rc: int = 0,
        missing_deps: tuple[str, ...] = ("libcava", "qt6-m3shapes-git", "quickshell-git", "cmake>=3.19"),
        makepkg_helper_rc: int = 0,
        makepkg_pkg_rc: int = 0,
        yay_rc: int = 0,
        paru_rc: int = 0,
        yay_stderr: str = "",
        yay_interactive_cleanbuild: bool = False,
        yay_sleep_on_s: bool = False,
        os_release: str = "ID=arch\nPRETTY_NAME=\"Arch Linux\"\n",
    ) -> tuple[dict[str, str], Path]:
        bin_dir = tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        log_file = tmp / "calls.log"
        log_file.write_text("", encoding="utf-8")
        os_rel = tmp / "os-release"
        os_rel.write_text(os_release, encoding="utf-8")
        srcinfo_payload = tmp / "srcinfo.payload"
        srcinfo_payload.write_text(printsrcinfo_out, encoding="utf-8")
        missing_payload = tmp / "missing.payload"
        missing_payload.write_text("\n".join(missing_deps) + ("\n" if missing_deps else ""), encoding="utf-8")

        for tool in ("basename", "bash", "cat", "chmod", "cut", "dirname", "grep", "head", "id", "mkdir", "mktemp", "rm", "sleep", "tr", "uname"):
            real = shutil.which(tool)
            if real:
                link = bin_dir / tool
                if not link.exists():
                    link.symlink_to(real)

        if "pacman" in include_bins:
            bd_rc = "0" if base_devel_installed else "1"
            _write_exe(
                bin_dir / "pacman",
                f"#!/usr/bin/env bash\n"
                f"echo \"pacman $*\" >> \"{log_file}\"\n"
                f"if [[ \"$1\" == \"-Qq\" && \"$2\" == \"base-devel\" ]]; then exit {bd_rc}; fi\n"
                f"if [[ \"$1\" == \"-T\" ]]; then\n"
                f"  if [[ -s \"{missing_payload}\" ]]; then\n"
                f"    cat \"{missing_payload}\"\n"
                f"    exit 127\n"
                f"  fi\n"
                f"  exit 0\n"
                f"fi\n"
                f"exit 0\n",
            )

        if "sudo" in include_bins:
            _write_exe(
                bin_dir / "sudo",
                f"#!/usr/bin/env bash\necho \"sudo $*\" >> \"{log_file}\"\n\"$@\"\n",
            )

        if "git" in include_bins:
            _write_exe(
                bin_dir / "git",
                f"#!/usr/bin/env bash\n"
                f"echo \"git $*\" >> \"{log_file}\"\n"
                f"if [[ \"$1\" == \"clone\" ]]; then\n"
                f"  dest=\"${{@: -1}}\"\n"
                f"  mkdir -p \"$dest\"\n"
                f"  echo 'pkgname=mock' > \"$dest/PKGBUILD\"\n"
                f"fi\n"
                f"exit 0\n",
            )

        if "makepkg" in include_bins:
            _write_exe(
                bin_dir / "makepkg",
                f"#!/usr/bin/env bash\n"
                f"echo \"makepkg $*\" >> \"{log_file}\"\n"
                f"if [[ \"$1\" == \"--printsrcinfo\" ]]; then\n"
                f"  if (( {printsrcinfo_rc} != 0 )); then exit {printsrcinfo_rc}; fi\n"
                f"  cat \"{srcinfo_payload}\"\n"
                f"  exit 0\n"
                f"fi\n"
                f"case \"$PWD\" in\n"
                f"  */yay|*/paru)\n"
                f"    if (( {makepkg_helper_rc} == 0 )); then\n"
                f"      h=\"$(basename \"$PWD\")\"\n"
                f"      printf '#!/usr/bin/env bash\\necho \"%s $*\" >> \"%s\"\\nexit 0\\n' \"$h\" \"{log_file}\" > \"{bin_dir}/$h\"\n"
                f"      chmod +x \"{bin_dir}/$h\"\n"
                f"    fi\n"
                f"    exit {makepkg_helper_rc}\n"
                f"    ;;\n"
                f"  *)\n"
                f"    exit {makepkg_pkg_rc}\n"
                f"    ;;\n"
                f"esac\n",
            )

        if "yay" in include_bins:
            extra = ""
            if yay_interactive_cleanbuild:
                extra = (
                    "printf 'Packages to cleanBuild?\\n==> '\n"
                    "IFS= read -r ans || { echo '-> EOF' >&2; exit 1; }\n"
                    "echo \"yay_cleanbuild_ans=${ans}\" >> \"" + str(log_file) + "\"\n"
                )
            elif yay_sleep_on_s:
                extra = "exec sleep 10\n"
            stderr_line = f'echo "{yay_stderr}" >&2\n' if yay_stderr else ""
            _write_exe(
                bin_dir / "yay",
                f"#!/usr/bin/env bash\n"
                f"echo \"yay $*\" >> \"{log_file}\"\n"
                f"{extra}"
                f"{stderr_line}"
                f"exit {yay_rc}\n",
            )

        if "paru" in include_bins:
            _write_exe(
                bin_dir / "paru",
                f"#!/usr/bin/env bash\necho \"paru $*\" >> \"{log_file}\"\nexit {paru_rc}\n",
            )

        env = {
            "PATH": str(bin_dir),
            "HOME": str(tmp),
            "TMPDIR": str(tmp),
            "HELYSIUM_OS_RELEASE": str(os_rel),
        }
        return env, log_file

    def _run_install(self, env: dict[str, str], *, stdin_text: str = "", script_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script_path)],
            input=stdin_text,
            text=True,
            capture_output=True,
            env=env,
            timeout=15,
        )

    def test_install_sh_executable_and_no_unsafe_flags(self) -> None:
        self.assertTrue(os.access(INSTALL_SH, os.X_OK), "install.sh must be executable")
        text = INSTALL_SH.read_text(encoding="utf-8")
        for forbidden in ("--nodeps", "--skipinteg", "--noconfirm", "-Bi ."):
            self.assertNotIn(forbidden, text)
        syntax = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_refuses_root_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, _ = self._stage_isolated_repo(tmp_path)
            env, _ = self._make_fake_env(tmp_path)
            env["HELYSIUM_TEST_EUID"] = "0"
            r = self._run_install(env, script_path=script)
            self.assertEqual(r.returncode, 1)
            self.assertIn("Do not run install.sh as root", r.stderr)

    def test_yay_resolves_aur_deps_then_runs_makepkg_Cfsi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, pkg_dir = self._stage_isolated_repo(tmp_path)
            env, log_file = self._make_fake_env(tmp_path, include_bins=("pacman", "sudo", "git", "makepkg", "yay"))
            r = self._run_install(env, script_path=script)
            self.assertEqual(r.returncode, 0, r.stderr)
            calls = log_file.read_text(encoding="utf-8")
            self.assertIn("makepkg --printsrcinfo", calls)
            self.assertIn("yay -S --needed --asdeps libcava qt6-m3shapes-git quickshell-git cmake>=3.19", calls)
            self.assertNotIn("yay -Bi", calls)
            self.assertIn("makepkg -Cfsi", calls)
            self.assertFalse((pkg_dir / ".SRCINFO").exists())
            self.assertEqual(list(tmp_path.glob("helysium-shell-srcinfo.*")), [])

    def test_paru_resolves_aur_deps_then_runs_makepkg_Cfsi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, _ = self._stage_isolated_repo(tmp_path)
            env, log_file = self._make_fake_env(tmp_path, include_bins=("pacman", "sudo", "git", "makepkg", "paru"))
            r = self._run_install(env, script_path=script)
            self.assertEqual(r.returncode, 0, r.stderr)
            calls = log_file.read_text(encoding="utf-8")
            self.assertIn("paru -S --needed --asdeps libcava qt6-m3shapes-git quickshell-git cmake>=3.19", calls)
            self.assertIn("makepkg -Cfsi", calls)

    def test_all_deps_already_satisfied_skips_helper_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, _ = self._stage_isolated_repo(tmp_path)
            env, log_file = self._make_fake_env(
                tmp_path,
                include_bins=("pacman", "sudo", "git", "makepkg", "yay"),
                missing_deps=(),
            )
            r = self._run_install(env, script_path=script)
            self.assertEqual(r.returncode, 0, r.stderr)
            calls = log_file.read_text(encoding="utf-8")
            self.assertNotIn("yay -S", calls)
            self.assertIn("makepkg -Cfsi", calls)

    def test_aur_dep_failure_stops_before_makepkg_with_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, _ = self._stage_isolated_repo(tmp_path)
            env, log_file = self._make_fake_env(
                tmp_path,
                include_bins=("pacman", "sudo", "git", "makepkg", "yay"),
                yay_rc=1,
                yay_stderr="could not satisfy dependencies: quickshell-git",
            )
            r = self._run_install(env, script_path=script)
            self.assertEqual(r.returncode, 1)
            self.assertIn("Failed to resolve or install dependencies", r.stderr)
            self.assertIn("quickshell-git", r.stderr)
            self.assertNotIn("makepkg -Cfsi", log_file.read_text(encoding="utf-8"))
            self.assertNotIn("built and installed successfully", r.stdout)

    def test_interactive_cleanbuild_all_choice_never_touches_local_srcinfo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, pkg_dir = self._stage_isolated_repo(tmp_path)
            preexisting = pkg_dir / ".SRCINFO"
            preexisting.write_text("# preexisting\n", encoding="utf-8")
            env, log_file = self._make_fake_env(
                tmp_path,
                include_bins=("pacman", "sudo", "git", "makepkg", "yay"),
                yay_interactive_cleanbuild=True,
            )
            r = self._run_install(env, stdin_text="A\n", script_path=script)
            self.assertEqual(r.returncode, 0, r.stderr)
            calls = log_file.read_text(encoding="utf-8")
            self.assertIn("yay_cleanbuild_ans=A", calls)
            self.assertTrue(preexisting.exists())
            self.assertEqual(preexisting.read_text(encoding="utf-8"), "# preexisting\n")

    def test_both_helpers_installed_user_chooses_paru(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, _ = self._stage_isolated_repo(tmp_path)
            env, log_file = self._make_fake_env(
                tmp_path, include_bins=("pacman", "sudo", "git", "makepkg", "yay", "paru")
            )
            r = self._run_install(env, stdin_text="2\n", script_path=script)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Selected AUR helper: paru", r.stdout)
            self.assertIn("paru -S --needed --asdeps", log_file.read_text(encoding="utf-8"))

    def test_no_helper_bootstrap_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, _ = self._stage_isolated_repo(tmp_path)
            env, log_file = self._make_fake_env(tmp_path)
            r = self._run_install(env, stdin_text="q\n", script_path=script)
            self.assertEqual(r.returncode, 0)
            self.assertIn("Installation cancelled", r.stdout)
            self.assertNotIn("git clone", log_file.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, _ = self._stage_isolated_repo(tmp_path)
            env, log_file = self._make_fake_env(tmp_path)
            r = self._run_install(env, stdin_text="1\ny\n", script_path=script)
            self.assertEqual(r.returncode, 0, r.stderr)
            calls = log_file.read_text(encoding="utf-8")
            self.assertIn("git clone --depth=1 https://aur.archlinux.org/yay.git", calls)
            self.assertIn("yay -S --needed --asdeps", calls)
            self.assertIn("makepkg -Cfsi", calls)

    def test_interruption_cleans_temp_srcinfo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script, _ = self._stage_isolated_repo(tmp_path)
            env, _ = self._make_fake_env(
                tmp_path,
                include_bins=("pacman", "sudo", "git", "makepkg", "yay"),
                yay_sleep_on_s=True,
            )
            proc = subprocess.Popen(
                ["bash", str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not list(tmp_path.glob("helysium-shell-srcinfo.*")):
                    time.sleep(0.05)
                os.killpg(proc.pid, signal.SIGINT)
                proc.communicate(timeout=5)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
            self.assertEqual(list(tmp_path.glob("helysium-shell-srcinfo.*")), [])


if __name__ == "__main__":
    unittest.main()
