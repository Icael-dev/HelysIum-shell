import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtQml import QJSEngine

SHELL_ROOT = Path(__file__).resolve().parent.parent
_APP = QCoreApplication.instance() or QCoreApplication([])


def _qml_functions_to_js(qml_source: str, func_names: list[str]) -> str:
    """Extract named QML functions from a QML file and strip QML type annotations for QJSEngine."""
    extracted: list[str] = []
    for name in func_names:
        pattern = re.compile(rf"\bfunction\s+{re.escape(name)}\s*\(([^)]*)\)\s*(?::\s*[\w<>]+)?\s*\{{")
        match = pattern.search(qml_source)
        if not match:
            raise AssertionError(f"Function {name} not found in QML source")
        start = match.start()
        brace_start = qml_source.index("{", match.start())
        depth = 0
        end = brace_start
        in_str: str | None = None
        escape = False
        for idx in range(brace_start, len(qml_source)):
            ch = qml_source[idx]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if in_str:
                if ch == in_str:
                    in_str = None
                continue
            if ch in ("'", '"', "`"):
                in_str = ch
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break
        raw_params = match.group(1)
        clean_params = ", ".join(
            p.split(":")[0].strip() for p in raw_params.split(",") if p.strip()
        )
        body = qml_source[brace_start:end]
        extracted.append(f"function {name}({clean_params}) {body}")
    return "\n\n".join(extracted)


class TestSec05VpnAndNmcli(unittest.TestCase):
    def setUp(self) -> None:
        self.vpn_path = SHELL_ROOT / "services" / "VPN.qml"
        self.nmcli_path = SHELL_ROOT / "services" / "Nmcli.qml"
        self.vpn_src = self.vpn_path.read_text(encoding="utf-8")
        self.nmcli_src = self.nmcli_path.read_text(encoding="utf-8")

        self.engine = QJSEngine()
        js_code = _qml_functions_to_js(
            self.vpn_src,
            [
                "isValidInterface",
                "isValidConnectionId",
                "buildWireguardConnectCmd",
                "buildWireguardFallbackConnectCmd",
                "buildWireguardDisconnectCmd",
                "buildWireguardFallbackDisconnectCmd",
                "isValidCommand",
                "parseInterfaceStatus",
                "applyPingOutput",
                "refreshStats",
            ],
        )
        setup_script = f"""
var connected = true;
var active = {{ interface: "wg0", serverCmd: null }};
var root = {{
    connected: connected,
    pingMs: -1,
    serverLocation: "",
    active: active,
}};
var statsProc = {{ command: [], running: false }};
var pingProc = {{ command: [], running: false }};
var pingFallbackProc = {{ command: ["ping", "-c1", "-W2", "1.1.1.1"], running: false }};
var serverProc = {{ exec: function(cmd) {{}} }};

{js_code}

root.isValidInterface = isValidInterface;
root.isValidConnectionId = isValidConnectionId;
root.buildWireguardConnectCmd = buildWireguardConnectCmd;
root.buildWireguardFallbackConnectCmd = buildWireguardFallbackConnectCmd;
root.buildWireguardDisconnectCmd = buildWireguardDisconnectCmd;
root.buildWireguardFallbackDisconnectCmd = buildWireguardFallbackDisconnectCmd;
root.isValidCommand = isValidCommand;
root.parseInterfaceStatus = parseInterfaceStatus;
root.applyPingOutput = applyPingOutput;
root.refreshStats = refreshStats;
root.wireguardConnectCmd = function(iface) {{
    return root.buildWireguardConnectCmd(iface, "");
}};
root.wireguardDisconnectCmd = function(iface) {{
    return root.buildWireguardDisconnectCmd(iface, "");
}};
"""
        res = self.engine.evaluate(setup_script)
        self.assertFalse(res.isError(), msg=res.toString())

    def test_no_pkexec_wg_quick_or_sh_c_in_vpn_and_nmcli(self) -> None:
        self.assertNotIn("pkexec", self.vpn_src)
        self.assertNotIn("wg-quick", self.vpn_src)
        self.assertNotIn('"sh", "-c"', self.vpn_src)
        self.assertIn('["nmcli", "connection", "up", "id", iface]', self.vpn_src)
        self.assertIn('["nmcli", "connection", "down", "id", iface]', self.vpn_src)
        self.assertIn('root.buildWireguardConnectCmd(iface', self.vpn_src)
        self.assertIn('root.buildWireguardDisconnectCmd(iface', self.vpn_src)

        # In Nmcli.qml, speedProc and dataUsageProc must use direct ["cat", ...] without "sh", "-c"
        self.assertNotIn("cat /sys/class/net/", self.nmcli_src)
        self.assertIn(
            'speedProc.command = ["cat", `/sys/class/net/${interfaceName}/speed`]',
            self.nmcli_src,
        )
        self.assertIn(
            'dataUsageProc.command = ["cat", `/sys/class/net/${interfaceName}/statistics/rx_bytes`, `/sys/class/net/${interfaceName}/statistics/tx_bytes`]',
            self.nmcli_src,
        )

    def test_vpn_interface_validation_and_command_generation(self) -> None:
        valid_ifaces = ["wg0", "wg-mullvad_01", "tun0.10", "enp3s0", "CloudflareWARP", "wt0", "tailscale0"]
        for iface in valid_ifaces:
            ok = self.engine.evaluate(f"root.isValidInterface({json.dumps(iface)})").toBool()
            self.assertTrue(ok, f"Expected valid interface: {iface!r}")

            up_cmd = self.engine.evaluate(f"JSON.stringify(root.wireguardConnectCmd({json.dumps(iface)}))").toString()
            down_cmd = self.engine.evaluate(f"JSON.stringify(root.wireguardDisconnectCmd({json.dumps(iface)}))").toString()
            self.assertEqual(json.loads(up_cmd), ["nmcli", "connection", "up", "id", iface])
            self.assertEqual(json.loads(down_cmd), ["nmcli", "connection", "down", "id", iface])

        malicious_ifaces = [
            "",
            "wg 0",
            "wg0 eth0",
            "wg0' || touch /tmp/pwn",
            'wg0" || touch /tmp/pwn',
            "wg0; touch /tmp/pwn",
            "$(touch /tmp/pwn)",
            "`touch /tmp/pwn`",
            "wg0 | sh",
            "wg0 && id",
            "-I",
            "--help",
            "-wg0",
            ".",
            "..",
            "../etc/passwd",
            "wg0/../../etc",
            "a" * 16,  # Exceeds IFNAMSIZ - 1 (15 chars)
            "wg0\x00evil",
            "wg0\nid",
        ]
        for bad in malicious_ifaces:
            ok = self.engine.evaluate(f"root.isValidInterface({json.dumps(bad)})").toBool()
            self.assertFalse(ok, f"Should have rejected malformed/malicious interface: {bad!r}")

            up_cmd = json.loads(
                self.engine.evaluate(f"JSON.stringify(root.wireguardConnectCmd({json.dumps(bad)}))").toString()
            )
            down_cmd = json.loads(
                self.engine.evaluate(f"JSON.stringify(root.wireguardDisconnectCmd({json.dumps(bad)}))").toString()
            )
            self.assertEqual(up_cmd, [])
            self.assertEqual(down_cmd, [])

            # Also verify refreshStats refuses to populate or run statsProc/pingProc for invalid interface
            self.engine.evaluate(
                f"""
                statsProc.command = [];
                statsProc.running = false;
                pingProc.command = [];
                pingProc.running = false;
                root.active.interface = {json.dumps(bad)};
                root.refreshStats();
                """
            )
            self.assertFalse(self.engine.evaluate("statsProc.running").toBool())
            self.assertFalse(self.engine.evaluate("pingProc.running").toBool())

    def test_vpn_refresh_stats_and_nmcli_behavioral_no_injection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = Path(tmpdir) / "vpn_pwned"
            payload = f"wg0; touch {sentinel}"

            # 1. Attempting refreshStats with injection payload does not run anything
            self.engine.evaluate(
                f"""
                statsProc.command = [];
                statsProc.running = false;
                pingProc.command = [];
                pingProc.running = false;
                root.active.interface = {json.dumps(payload)};
                root.refreshStats();
                """
            )
            self.assertFalse(self.engine.evaluate("statsProc.running").toBool())
            self.assertFalse(sentinel.exists())

            # 2. Valid interface populates direct argv arrays without sh -c
            self.engine.evaluate(
                """
                root.active.interface = "wg0";
                root.refreshStats();
                """
            )
            stats_cmd = json.loads(self.engine.evaluate("JSON.stringify(statsProc.command)").toString())
            ping_cmd = json.loads(self.engine.evaluate("JSON.stringify(pingProc.command)").toString())
            self.assertEqual(
                stats_cmd,
                ["cat", "/sys/class/net/wg0/statistics/rx_bytes", "/sys/class/net/wg0/statistics/tx_bytes"],
            )
            self.assertEqual(
                ping_cmd,
                ["ping", "-c1", "-W2", "-I", "wg0", "1.1.1.1"],
            )

    def test_nmcli_and_vpn_connect_behavioral_execution(self) -> None:
        nmcli_js = _qml_functions_to_js(
            self.nmcli_src,
            ["isValidInterface", "getEthernetSpeed", "getEthernetDataUsage"],
        )
        nm_engine = QJSEngine()
        nm_setup = f"""
var root = {{ ethernetSpeed: "1000" }};
var speedProc = {{ command: [], running: false }};
var dataUsageProc = {{ iface: "", cb: null, command: [], running: false }};
{nmcli_js}
root.isValidInterface = isValidInterface;
root.getEthernetSpeed = getEthernetSpeed;
root.getEthernetDataUsage = getEthernetDataUsage;
"""
        res = nm_engine.evaluate(nm_setup)
        self.assertFalse(res.isError(), msg=res.toString())

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sentinel = tmp / "nmcli_pwned"
            for bad in (f"eth0; touch {sentinel}", "' || touch " + str(sentinel), '" && id', "../etc/passwd", "-h"):
                nm_engine.evaluate(
                    f"""
                    speedProc.command = [];
                    speedProc.running = false;
                    dataUsageProc.command = [];
                    dataUsageProc.running = false;
                    root.getEthernetSpeed({json.dumps(bad)});
                    root.getEthernetDataUsage({json.dumps(bad)}, function(v) {{}});
                    """
                )
                self.assertFalse(nm_engine.evaluate("speedProc.running").toBool())
                self.assertFalse(nm_engine.evaluate("dataUsageProc.running").toBool())
                self.assertFalse(sentinel.exists())

            nm_engine.evaluate('root.getEthernetSpeed("enp3s0"); root.getEthernetDataUsage("enp3s0", null);')
            self.assertEqual(
                json.loads(nm_engine.evaluate("JSON.stringify(speedProc.command)").toString()),
                ["cat", "/sys/class/net/enp3s0/speed"],
            )
            self.assertEqual(
                json.loads(nm_engine.evaluate("JSON.stringify(dataUsageProc.command)").toString()),
                ["cat", "/sys/class/net/enp3s0/statistics/rx_bytes", "/sys/class/net/enp3s0/statistics/tx_bytes"],
            )

            # Behavioral test with stub nmcli for wireguardConnectCmd / wireguardDisconnectCmd
            nmcli_log = tmp / "nmcli_args.json"
            nmcli_stub = tmp / "nmcli"
            nmcli_stub.write_text(
                f"""#!{sys.executable}
import json, sys
with open({json.dumps(str(nmcli_log))}, "w", encoding="utf-8") as f:
    json.dump(sys.argv[1:], f)
""",
                encoding="utf-8",
            )
            nmcli_stub.chmod(0o755)

            up_cmd = json.loads(self.engine.evaluate('JSON.stringify(root.wireguardConnectCmd("wg-mullvad_01"))').toString())
            subprocess.run([str(nmcli_stub), *up_cmd[1:]], check=True)
            self.assertEqual(
                json.loads(nmcli_log.read_text(encoding="utf-8")),
                ["connection", "up", "id", "wg-mullvad_01"],
            )

    def test_wireguard_nm_profiles_connection_name_mismatch_and_wg_quick_unmanaged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sentinel = tmp / "wg_pwned"
            nmcli_stub = tmp / "nmcli"
            # Stub nmcli that knows connections in KNOWN_CONNS env var (JSON list) and devices in KNOWN_DEVS
            nmcli_stub.write_text(
                f"""#!{sys.executable}
import json, os, sys
known_conns = json.loads(os.environ.get("KNOWN_CONNS", "[]"))
known_devs = json.loads(os.environ.get("KNOWN_DEVS", "[]"))
args = sys.argv[1:]
if len(args) == 4 and args[0] == "connection" and args[1] in ("up", "down") and args[2] == "id":
    target = args[3]
    if target in known_conns:
        print(f"Connection '{{target}}' successfully activated/deactivated.")
        sys.exit(0)
    sys.stderr.write(f"Error: '{{target}}' is not an active connection or unknown connection '{{target}}'.\\n")
    sys.exit(10)
if len(args) == 3 and args[0] == "device" and args[1] == "disconnect":
    dev = args[2]
    if dev in known_devs:
        print(f"Device '{{dev}}' successfully disconnected.")
        sys.exit(0)
    sys.stderr.write(f"Error: Device '{{dev}}' not found.\\n")
    sys.exit(10)
sys.exit(2)
""",
                encoding="utf-8",
            )
            nmcli_stub.chmod(0o755)

            def run_wg_flow(action: str, iface: str, display_name: str, known_conns: list[str], known_devs: list[str]) -> tuple[int, list[list[str]], str]:
                env = {
                    "KNOWN_CONNS": json.dumps(known_conns),
                    "KNOWN_DEVS": json.dumps(known_devs),
                    "LC_ALL": "C.UTF-8",
                }
                if action == "connect":
                    primary = json.loads(
                        self.engine.evaluate(
                            f"JSON.stringify(root.buildWireguardConnectCmd({json.dumps(iface)}, {json.dumps(display_name)}))"
                        ).toString()
                    )
                    fallback = json.loads(
                        self.engine.evaluate(
                            f"JSON.stringify(root.buildWireguardFallbackConnectCmd({json.dumps(iface)}, {json.dumps(display_name)}))"
                        ).toString()
                    )
                else:
                    primary = json.loads(
                        self.engine.evaluate(
                            f"JSON.stringify(root.buildWireguardDisconnectCmd({json.dumps(iface)}, {json.dumps(display_name)}))"
                        ).toString()
                    )
                    fallback = json.loads(
                        self.engine.evaluate(
                            f"JSON.stringify(root.buildWireguardFallbackDisconnectCmd({json.dumps(iface)}, {json.dumps(display_name)}))"
                        ).toString()
                    )
                if not primary:
                    return (1, [], "Invalid configuration")
                tried = [primary]
                res = subprocess.run([str(nmcli_stub), *primary[1:]], env=env, capture_output=True, text=True, check=False)
                if res.returncode != 0 and fallback:
                    tried.append(fallback)
                    res = subprocess.run([str(nmcli_stub), *fallback[1:]], env=env, capture_output=True, text=True, check=False)
                return (res.returncode, tried, res.stderr.strip())

            # 1. Valid NetworkManager profile where connection.id == iface ("wg0")
            rc1, tried1, _ = run_wg_flow("connect", "wg0", "wg0", ["wg0"], ["wg0"])
            self.assertEqual(rc1, 0)
            self.assertEqual(tried1, [["nmcli", "connection", "up", "id", "wg0"]])

            # 2. Connection name ("Mullvad FR") differs from interface name ("wg0")
            rc2, tried2, _ = run_wg_flow("connect", "wg0", "Mullvad FR", ["Mullvad FR"], ["wg0"])
            self.assertEqual(rc2, 0)
            self.assertEqual(
                tried2,
                [
                    ["nmcli", "connection", "up", "id", "wg0"],
                    ["nmcli", "connection", "up", "id", "Mullvad FR"],
                ],
            )
            rc2_down, tried2_down, _ = run_wg_flow("disconnect", "wg0", "Mullvad FR", ["Mullvad FR"], ["wg0"])
            self.assertEqual(rc2_down, 0)
            self.assertEqual(
                tried2_down,
                [
                    ["nmcli", "connection", "down", "id", "wg0"],
                    ["nmcli", "connection", "down", "id", "Mullvad FR"],
                ],
            )

            # 2b. Disconnect fallback via device disconnect when connection.id is unknown to HelysIum
            rc2_dev, tried2_dev, _ = run_wg_flow("disconnect", "wg0", "wg0", ["SomeOtherConnName"], ["wg0"])
            self.assertEqual(rc2_dev, 0)
            self.assertEqual(
                tried2_dev,
                [
                    ["nmcli", "connection", "down", "id", "wg0"],
                    ["nmcli", "device", "disconnect", "wg0"],
                ],
            )

            # 3. Profile absent or standalone wg-quick config not imported into NetworkManager
            rc3, tried3, err3 = run_wg_flow("connect", "wg0", "wg0", [], [])
            self.assertNotEqual(rc3, 0)
            self.assertIn("unknown connection", err3)
            self.assertIn("nmcli connection import type wireguard file /etc/wireguard/${iface}.conf", self.vpn_src)

            # 4. Injection attempts in displayName/connId never execute shell commands and reject flags/control chars
            for bad_conn in ("-h", "--help", "VPN\nid", "VPN\x00id", "", "A" * 129):
                self.assertFalse(
                    self.engine.evaluate(f"root.isValidConnectionId({json.dumps(bad_conn)})").toBool(),
                    f"Should have rejected connection ID: {bad_conn!r}",
                )
            inj_display = f"Mullvad; $(touch {sentinel}) `touch {sentinel}`"
            rc4, tried4, _ = run_wg_flow("connect", "wg0", inj_display, [inj_display], ["wg0"])
            self.assertEqual(rc4, 0)
            self.assertEqual(tried4[-1], ["nmcli", "connection", "up", "id", inj_display])
            self.assertFalse(sentinel.exists(), "Shell metacharacters in displayName must never be executed")


class TestSec07aCalcItem(unittest.TestCase):
    def setUp(self) -> None:
        self.calc_path = SHELL_ROOT / "modules" / "launcher" / "items" / "CalcItem.qml"
        self.calc_src = self.calc_path.read_text(encoding="utf-8")
        self.engine = QJSEngine()
        js_code = _qml_functions_to_js(self.calc_src, ["isValidExpression", "buildCalcCommand"])
        setup_script = f"""
var root = {{}};
{js_code}
root.isValidExpression = isValidExpression;
root.buildCalcCommand = buildCalcCommand;
"""
        res = self.engine.evaluate(setup_script)
        self.assertFalse(res.isError(), msg=res.toString())

    def test_no_fish_c_in_calc_item(self) -> None:
        self.assertNotIn('"fish"', self.calc_src)
        self.assertNotIn('"-C"', self.calc_src)
        self.assertIn('["wl-copy", "--", Qalculator.rawResult]', self.calc_src)

    def test_calc_item_command_building_and_behavioral_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sentinel = tmp / "calc_pwned"
            term_log = tmp / "terminal_args.json"
            term_stub = tmp / "foot_stub"
            term_stub.write_text(
                f"""#!{sys.executable}
import json, sys
with open({json.dumps(str(term_log))}, "w", encoding="utf-8") as f:
    json.dump(sys.argv[1:], f)
""",
                encoding="utf-8",
            )
            term_stub.chmod(0o755)

            expressions = [
                "2 + 2",
                "  (10 + 20) * 3  ",
                "-5 + 3",
                "-f /etc/passwd",
                "--help",
                f"1+1'; touch {sentinel}; echo '",
                f'1+1"; touch {sentinel}; echo "',
                rf"1+1 \ $(touch {sentinel}) `touch {sentinel}` ; | & > <",
            ]
            for expr in expressions:
                cmd_json = self.engine.evaluate(
                    f"JSON.stringify(root.buildCalcCommand([{json.dumps(str(term_stub))}], {json.dumps(expr)}))"
                ).toString()
                cmd = json.loads(cmd_json)
                self.assertEqual(cmd, [str(term_stub), "qalc", "-i", "--", expr])

                # Execute the generated command directly as Quickshell.execDetached does
                subprocess.run(cmd, check=True)
                recorded = json.loads(term_log.read_text(encoding="utf-8"))
                self.assertEqual(recorded, ["qalc", "-i", "--", expr])
                self.assertFalse(sentinel.exists(), f"Injection executed for expr={expr!r}")

                # Also execute the real qalc binary with the exact arguments after terminal
                qalc_proc = subprocess.run(
                    cmd[1:],
                    input="quit\n",
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(qalc_proc.returncode, 0)
                self.assertFalse(sentinel.exists())

            # Empty expression opens interactive qalc without "--" or trailing empty arg
            empty_cmd = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildCalcCommand([{json.dumps(str(term_stub))}], ''))"
                ).toString()
            )
            self.assertEqual(empty_cmd, [str(term_stub), "qalc", "-i"])

            # Malformed expressions (control characters: NUL, newline, CR, tab) are rejected
            for malformed in (f"1+1\n touch {sentinel}", "1+1\r\n", "1+1\x00evil", "1+1\t2"):
                bad_cmd = json.loads(
                    self.engine.evaluate(
                        f"JSON.stringify(root.buildCalcCommand([{json.dumps(str(term_stub))}], {json.dumps(malformed)}))"
                    ).toString()
                )
                self.assertEqual(bad_cmd, [], f"Should have rejected malformed expr: {malformed!r}")


class TestSec07bPicker(unittest.TestCase):
    def setUp(self) -> None:
        self.picker_path = SHELL_ROOT / "modules" / "areapicker" / "Picker.qml"
        self.picker_src = self.picker_path.read_text(encoding="utf-8")
        self.engine = QJSEngine()
        js_code = _qml_functions_to_js(
            self.picker_src,
            ["randomHex", "isSafeCacheDir", "buildTempPath", "buildPostSaveCommand"],
        )
        setup_script = f"""
var root = {{ saveSeq: 0 }};
{js_code}
root.randomHex = randomHex;
root.isSafeCacheDir = isSafeCacheDir;
root.buildTempPath = buildTempPath;
root.buildPostSaveCommand = buildPostSaveCommand;
"""
        res = self.engine.evaluate(setup_script)
        self.assertFalse(res.isError(), msg=res.toString())

    def test_no_predictable_tmp_or_sh_c_in_picker(self) -> None:
        self.assertNotIn("/tmp/helysium-picker", self.picker_src)
        self.assertNotIn('"sh", "-c"', self.picker_src)
        self.assertNotIn("wl-copy --type image/png < ", self.picker_src)

    def test_secure_temp_path_generation_and_validation(self) -> None:
        # Reject /tmp, /, relative paths, .. traversal, and control chars
        for unsafe_dir in ("", "/tmp", "/tmp/cache", "/", "///", "relative/cache", "/home/user/../tmp", "/home/user\ncache"):
            res = self.engine.evaluate(f"root.buildTempPath({json.dumps(unsafe_dir)}, 1234)").toString()
            self.assertEqual(res, "", f"Should have rejected unsafe cacheDir: {unsafe_dir!r}")

        # Valid user cache directory produces unique, non-predictable filenames
        p1 = self.engine.evaluate('root.buildTempPath("/home/user/.cache/helysium///", 4321)').toString()
        p2 = self.engine.evaluate('root.buildTempPath("/home/user/.cache/helysium", 4321)').toString()
        self.assertNotEqual(p1, p2)
        self.assertRegex(
            p1,
            r"^/home/user/\.cache/helysium/picker/picker-4321-\d+-1-[0-9a-f]{16}\.png$",
        )
        self.assertRegex(
            p2,
            r"^/home/user/\.cache/helysium/picker/picker-4321-\d+-2-[0-9a-f]{16}\.png$",
        )

    def _create_picker_stubs(
        self,
        bin_dir: Path,
        wl_out: Path,
        notify_out: Path,
        swappy_out: Path,
        wl_exit: int = 0,
        wl_sleep: float = 0.0,
    ) -> None:
        bin_dir.mkdir(parents=True, exist_ok=True)
        py_link = bin_dir / "python3"
        if not py_link.exists():
            py_link.symlink_to(sys.executable)
        wl_stub = bin_dir / "wl-copy"
        wl_stub.write_text(
            f"""#!{sys.executable}
import sys, time
if {wl_sleep} > 0:
    time.sleep({wl_sleep})
data = sys.stdin.buffer.read()
if {wl_exit} != 0:
    sys.exit({wl_exit})
with open({json.dumps(str(wl_out))}, "wb") as f:
    f.write(data)
sys.exit(0)
""",
            encoding="utf-8",
        )
        wl_stub.chmod(0o755)

        notify_stub = bin_dir / "notify-send"
        notify_stub.write_text(
            f"""#!{sys.executable}
import json, sys
with open({json.dumps(str(notify_out))}, "w", encoding="utf-8") as f:
    json.dump(sys.argv[1:], f)
sys.exit(0)
""",
            encoding="utf-8",
        )
        notify_stub.chmod(0o755)

        swappy_stub = bin_dir / "swappy"
        swappy_stub.write_text(
            f"""#!{sys.executable}
import json, os, sys
path = sys.argv[2] if len(sys.argv) > 2 else ""
with open({json.dumps(str(swappy_out))}, "w", encoding="utf-8") as f:
    json.dump({{"argv": sys.argv[1:], "exists_during_run": os.path.isfile(path)}}, f)
sys.exit(0)
""",
            encoding="utf-8",
        )
        swappy_stub.chmod(0o755)

    def test_picker_clipboard_unusual_paths_metacharacters_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sentinel = tmp / "picker_pwned"
            bin_dir = tmp / "bin"
            wl_out = tmp / "wl_copied.png"
            notify_out = tmp / "notify.json"
            swappy_out = tmp / "swappy.json"
            self._create_picker_stubs(bin_dir, wl_out, notify_out, swappy_out)

            # Create a directory and filename containing spaces, quotes, backslashes, and shell metacharacters
            unusual_dir = tmp / "cache 'quote' \"dbl\" $(touch picker_pwned) ; | &"
            unusual_dir.mkdir(parents=True, mode=0o700)
            unusual_dir.chmod(0o700)
            img_path = unusual_dir / "shot `touch picker_pwned` \\ -flag.png"
            png_bytes = b"\x89PNG\r\n\x1a\nFAKE_PNG_PAYLOAD_1234567890"
            img_path.write_bytes(png_bytes)

            title = f"-t 0 $(touch {sentinel})"
            body = f"Body '; touch {sentinel}; '"

            cmd = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildPostSaveCommand({json.dumps(str(img_path))}, true, {json.dumps(title)}, {json.dumps(body)}))"
                ).toString()
            )
            self.assertGreater(len(cmd), 0)

            env = {"PATH": str(bin_dir), "LC_ALL": "C.UTF-8"}
            res = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, msg=res.stderr)

            # 1. wl-copy received exact binary PNG payload on stdin
            self.assertTrue(wl_out.is_file())
            self.assertEqual(wl_out.read_bytes(), png_bytes)

            # 2. notify-send received exact arguments without option injection
            self.assertTrue(notify_out.is_file())
            self.assertEqual(
                json.loads(notify_out.read_text(encoding="utf-8")),
                ["-a", "helysium-cli", f"--icon={img_path}", "--", title, body],
            )

            # 3. Temporary image file was cleaned up
            self.assertFalse(img_path.exists())

            # 4. No injected command was executed
            self.assertFalse(sentinel.exists())

    def test_picker_wl_copy_failure_or_absence_still_cleans_up_without_notifying(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            wl_out = tmp / "wl_copied.png"
            notify_out = tmp / "notify.json"
            swappy_out = tmp / "swappy.json"

            # Case 1: wl-copy fails with exit code 1
            self._create_picker_stubs(bin_dir, wl_out, notify_out, swappy_out, wl_exit=1)
            img1 = tmp / "shot_fail.png"
            img1.write_bytes(b"\x89PNG_FAIL_TEST")

            cmd1 = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildPostSaveCommand({json.dumps(str(img1))}, true, 'Title', 'Body'))"
                ).toString()
            )
            res1 = subprocess.run(cmd1, env={"PATH": str(bin_dir)}, capture_output=True, text=True, check=False)
            self.assertNotEqual(res1.returncode, 0)
            self.assertFalse(img1.exists(), "Temp file must be deleted even when wl-copy fails")
            self.assertFalse(notify_out.exists(), "notify-send must not run when wl-copy fails")

            # Case 2: wl-copy is completely absent from PATH
            (bin_dir / "wl-copy").unlink()
            img2 = tmp / "shot_missing_wl.png"
            img2.write_bytes(b"\x89PNG_MISSING_TEST")
            cmd2 = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildPostSaveCommand({json.dumps(str(img2))}, true, 'Title', 'Body'))"
                ).toString()
            )
            res2 = subprocess.run(cmd2, env={"PATH": str(bin_dir)}, capture_output=True, text=True, check=False)
            self.assertNotEqual(res2.returncode, 0)
            self.assertFalse(img2.exists(), "Temp file must be deleted even when wl-copy is absent")
            self.assertFalse(notify_out.exists())

    def test_picker_refuses_symlink_and_preserves_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            wl_out = tmp / "wl_copied.png"
            notify_out = tmp / "notify.json"
            swappy_out = tmp / "swappy.json"
            self._create_picker_stubs(bin_dir, wl_out, notify_out, swappy_out)

            secret_target = tmp / "secret_file.txt"
            secret_target.write_text("TOP_SECRET_DATA", encoding="utf-8")
            symlink_path = tmp / "symlink_shot.png"
            symlink_path.symlink_to(secret_target)

            cmd = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildPostSaveCommand({json.dumps(str(symlink_path))}, true, 'Title', 'Body'))"
                ).toString()
            )
            res = subprocess.run(cmd, env={"PATH": str(bin_dir)}, capture_output=True, text=True, check=False)
            self.assertNotEqual(res.returncode, 0)
            self.assertFalse(wl_out.exists(), "Symlink target must never be copied to clipboard")
            self.assertTrue(secret_target.is_file(), "Symlink target must not be deleted")

    def test_picker_swappy_mode_and_sigterm_interruption_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            wl_out = tmp / "wl_copied.png"
            notify_out = tmp / "notify.json"
            swappy_out = tmp / "swappy.json"
            self._create_picker_stubs(bin_dir, wl_out, notify_out, swappy_out, wl_sleep=10.0)

            # 1. Swappy mode: file exists while swappy runs, and is deleted once swappy exits
            img_swappy = tmp / "shot_swappy.png"
            img_swappy.write_bytes(b"\x89PNG_SWAPPY")
            cmd_swappy = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildPostSaveCommand({json.dumps(str(img_swappy))}, false, 'Title', 'Body'))"
                ).toString()
            )
            res_swappy = subprocess.run(cmd_swappy, env={"PATH": str(bin_dir)}, capture_output=True, text=True, check=False)
            self.assertEqual(res_swappy.returncode, 0)
            swappy_data = json.loads(swappy_out.read_text(encoding="utf-8"))
            self.assertEqual(swappy_data["argv"], ["-f", str(img_swappy)])
            self.assertTrue(swappy_data["exists_during_run"])
            self.assertFalse(img_swappy.exists(), "Temp file must be cleaned up after swappy exits")

            # 2. SIGTERM interruption during clipboard copy: finally block must still delete temp file
            img_sig = tmp / "shot_sigterm.png"
            img_sig.write_bytes(b"\x89PNG_SIGTERM")
            cmd_sig = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildPostSaveCommand({json.dumps(str(img_sig))}, true, 'Title', 'Body'))"
                ).toString()
            )
            proc = subprocess.Popen(cmd_sig, env={"PATH": str(bin_dir)})
            time.sleep(0.25)
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=3)
            self.assertFalse(img_sig.exists(), "Temp file must be cleaned up when process is interrupted by SIGTERM")

    def test_picker_post_save_rejects_insecure_cache_dir_and_parent_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            wl_out = tmp / "wl_copied.png"
            notify_out = tmp / "notify.json"
            swappy_out = tmp / "swappy.json"
            self._create_picker_stubs(bin_dir, wl_out, notify_out, swappy_out)

            # 1. Group/world-writable parent directory (0777) must be rejected
            insecure_dir = tmp / "world_writable_cache"
            insecure_dir.mkdir(mode=0o777)
            insecure_dir.chmod(0o777)
            img_insecure = insecure_dir / "shot.png"
            img_insecure.write_bytes(b"\x89PNG_INSECURE_DIR")

            cmd1 = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildPostSaveCommand({json.dumps(str(img_insecure))}, true, 'T', 'B'))"
                ).toString()
            )
            res1 = subprocess.run(cmd1, env={"PATH": str(bin_dir)}, capture_output=True, text=True, check=False)
            self.assertNotEqual(res1.returncode, 0)
            self.assertFalse(wl_out.exists(), "Must refuse to read screenshot from world-writable directory")

            # 2. Symlink in parent directory path must be rejected
            real_dir = tmp / "real_cache"
            real_dir.mkdir(mode=0o700)
            real_dir.chmod(0o700)
            img_in_real = real_dir / "shot.png"
            img_in_real.write_bytes(b"\x89PNG_PARENT_SYMLINK")

            symlink_parent = tmp / "symlink_cache"
            symlink_parent.symlink_to(real_dir)
            via_symlink = symlink_parent / "shot.png"

            cmd2 = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildPostSaveCommand({json.dumps(str(via_symlink))}, true, 'T', 'B'))"
                ).toString()
            )
            res2 = subprocess.run(cmd2, env={"PATH": str(bin_dir)}, capture_output=True, text=True, check=False)
            self.assertNotEqual(res2.returncode, 0)
            self.assertFalse(wl_out.exists(), "Must refuse path traversing a symlinked parent directory")
            self.assertTrue(img_in_real.is_file(), "Target file inside real dir must not be deleted through symlinked parent")

    def test_cutils_save_image_securely_cpp_collision_symlink_permissions_and_cache_security(self) -> None:
        gpp = shutil.which("g++")
        if not gpp:
            self.skipTest("g++ not available")

        cutils_cpp = (SHELL_ROOT / "plugin" / "src" / "HelysIum" / "cutils.cpp").read_text(encoding="utf-8")
        match = re.search(r"bool saveImageSecurely\(const QImage& img, const QString& file\)\s*\{", cutils_cpp)
        self.assertIsNotNone(match, "saveImageSecurely not found in cutils.cpp")
        assert match is not None

        # Extract the exact C++ function body from plugin/src/HelysIum/cutils.cpp
        brace_start = cutils_cpp.index("{", match.start())
        depth = 0
        end = brace_start
        for idx in range(brace_start, len(cutils_cpp)):
            if cutils_cpp[idx] == "{":
                depth += 1
            elif cutils_cpp[idx] == "}":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break
        save_fn_code = cutils_cpp[match.start() : end]

        harness_cpp = f"""
#include <qdir.h>
#include <qfile.h>
#include <qimage.h>
#include <qstring.h>
#include <qstringlist.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static uid_t test_geteuid() {{
    const char* fake = ::getenv("FAKE_EUID");
    if (fake && *fake) {{
        return static_cast<uid_t>(::strtoul(fake, nullptr, 10));
    }}
    return ::geteuid();
}}
#define geteuid test_geteuid

{save_fn_code}

#undef geteuid

int main(int argc, char** argv) {{
    if (argc < 2) return 2;
    ::umask(0000);
    const bool emptyImg = (argc >= 3 && ::strcmp(argv[2], "--empty-image") == 0);
    QImage img;
    if (!emptyImg) {{
        img = QImage(16, 16, QImage::Format_ARGB32);
        img.fill(0xFF1E1E2E);
    }}
    return saveImageSecurely(img, QString::fromUtf8(argv[1])) ? 0 : 1;
}}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src_file = tmp / "test_save.cpp"
            bin_file = tmp / "test_save"
            src_file.write_text(harness_cpp, encoding="utf-8")

            compile_res = subprocess.run(
                [
                    gpp,
                    "-std=c++20",
                    "-fPIC",
                    "-I/usr/include/qt6",
                    "-I/usr/include/qt6/QtCore",
                    "-I/usr/include/qt6/QtGui",
                    str(src_file),
                    "-o",
                    str(bin_file),
                    "-lQt6Gui",
                    "-lQt6Core",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(compile_res.returncode, 0, msg=compile_res.stderr)

            # 1. Valid creation in new cache subdir: must enforce 0700 on dir and 0600 on file even with umask(0000)
            target_ok = tmp / "cache" / "picker" / "shot1.png"
            res_ok = subprocess.run([str(bin_file), str(target_ok)], check=False)
            self.assertEqual(res_ok.returncode, 0)
            self.assertTrue(target_ok.is_file())
            self.assertEqual(target_ok.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target_ok.parent.stat().st_mode & 0o777, 0o700)
            self.assertTrue(target_ok.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

            # 2. Collision with preexisting file: must fail (O_CREAT | O_EXCL) and NEVER overwrite existing content
            existing_file = tmp / "cache" / "picker" / "existing.png"
            existing_file.write_bytes(b"PREEXISTING_THIRD_PARTY_CONTENT")
            res_coll = subprocess.run([str(bin_file), str(existing_file)], check=False)
            self.assertNotEqual(res_coll.returncode, 0)
            self.assertEqual(existing_file.read_bytes(), b"PREEXISTING_THIRD_PARTY_CONTENT")

            # 3. Symlink at destination: must fail (O_NOFOLLOW | O_EXCL) and NEVER overwrite symlink target
            victim = tmp / "victim.txt"
            victim.write_bytes(b"VICTIM_INTACT")
            dest_symlink = tmp / "cache" / "picker" / "symlink.png"
            dest_symlink.symlink_to(victim)
            res_sym = subprocess.run([str(bin_file), str(dest_symlink)], check=False)
            self.assertNotEqual(res_sym.returncode, 0)
            self.assertEqual(victim.read_bytes(), b"VICTIM_INTACT")

            # 4. Symlink in intermediate parent directory component: openat(O_DIRECTORY | O_NOFOLLOW) must fail
            real_sub = tmp / "real_sub"
            real_sub.mkdir(mode=0o700)
            sym_sub = tmp / "sym_sub"
            sym_sub.symlink_to(real_sub)
            res_psym = subprocess.run([str(bin_file), str(sym_sub / "picker" / "shot.png")], check=False)
            self.assertNotEqual(res_psym.returncode, 0)
            self.assertFalse((real_sub / "picker" / "shot.png").exists())

            # 5. Insecure cache directory (world-writable 0777 or group-writable 0770): must fail
            for bad_mode in (0o777, 0o770, 0o757):
                bad_cache = tmp / f"bad_cache_{bad_mode:o}"
                bad_cache.mkdir(mode=bad_mode)
                bad_cache.chmod(bad_mode)
                res_bad_dir = subprocess.run([str(bin_file), str(bad_cache / "shot.png")], check=False)
                self.assertNotEqual(res_bad_dir.returncode, 0)
                self.assertFalse((bad_cache / "shot.png").exists())

            # 6. Directory not owned by effective user (st_uid != euid): must fail
            unowned_target = tmp / "cache" / "picker" / "unowned_shot.png"
            env_fake_uid = dict(os.environ, FAKE_EUID=str(os.geteuid() + 1000))
            res_unowned = subprocess.run([str(bin_file), str(unowned_target)], env=env_fake_uid, check=False)
            self.assertNotEqual(res_unowned.returncode, 0)
            self.assertFalse(unowned_target.exists())

            # 7. Cleanup after image write failure: unlinkat(dirFd, ...) must remove the created file
            failed_save_target = tmp / "cache" / "picker" / "failed_write.png"
            res_fail_write = subprocess.run([str(bin_file), str(failed_save_target), "--empty-image"], check=False)
            self.assertNotEqual(res_fail_write.returncode, 0)
            self.assertFalse(failed_save_target.exists(), "Failed image write must unlink the newly created file")

    def test_picker_toctou_intermediate_component_swap_and_unowned_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            bin_dir.mkdir(mode=0o700)
            (bin_dir / "python3").symlink_to(sys.executable)

            active_cache = tmp / "active_cache"
            active_cache.mkdir(mode=0o700)
            active_cache.chmod(0o700)
            (active_cache / "picker").mkdir(mode=0o700)
            (active_cache / "picker").chmod(0o700)
            moved_cache = tmp / "moved_cache"
            victim_cache = tmp / "victim_cache"
            victim_cache.mkdir(mode=0o700)
            victim_cache.chmod(0o700)
            (victim_cache / "picker").mkdir(mode=0o700)
            (victim_cache / "picker").chmod(0o700)

            real_shot = active_cache / "picker" / "shot.png"
            real_shot.write_bytes(b"\x89PNG_REAL_SHOT")
            victim_shot = victim_cache / "picker" / "shot.png"
            victim_shot.write_bytes(b"VICTIM_SHOT_MUST_NOT_BE_DELETED")

            wl_out = tmp / "wl_toctou.png"
            # While wl-copy runs (after fd was opened via dir_fd), swap active_cache with a symlink to victim_cache!
            wl_stub = bin_dir / "wl-copy"
            wl_stub.write_text(
                f"""#!{sys.executable}
import os, sys
data = sys.stdin.buffer.read()
with open({json.dumps(str(wl_out))}, "wb") as f:
    f.write(data)
# Perform mid-flight TOCTOU swap of intermediate directory active_cache -> symlink to victim_cache
os.rename({json.dumps(str(active_cache))}, {json.dumps(str(moved_cache))})
os.symlink({json.dumps(str(victim_cache))}, {json.dumps(str(active_cache))})
sys.exit(0)
""",
                encoding="utf-8",
            )
            wl_stub.chmod(0o755)

            cmd = json.loads(
                self.engine.evaluate(
                    f"JSON.stringify(root.buildPostSaveCommand({json.dumps(str(real_shot))}, true, 'T', 'B'))"
                ).toString()
            )
            res = subprocess.run(cmd, env={"PATH": str(bin_dir)}, capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, msg=res.stderr)

            # 1. wl-copy received the real screenshot bytes
            self.assertEqual(wl_out.read_bytes(), b"\x89PNG_REAL_SHOT")
            # 2. The victim file behind the newly swapped intermediate symlink was NOT deleted
            self.assertTrue(victim_shot.is_file(), "TOCTOU swap of intermediate dir must never unlink victim file!")
            self.assertEqual(victim_shot.read_bytes(), b"VICTIM_SHOT_MUST_NOT_BE_DELETED")
            # 3. The original file inside moved_cache WAS deleted via the pinned dir_fd!
            self.assertFalse((moved_cache / "picker" / "shot.png").exists(), "Original file must be unlinked via pinned dir_fd")

            # 4. Also verify post-save refuses a file in a root-owned non-sticky directory (st_uid != euid)
            cmd_unowned = json.loads(
                self.engine.evaluate(
                    'JSON.stringify(root.buildPostSaveCommand("/etc/passwd", true, "T", "B"))'
                ).toString()
            )
            res_unowned = subprocess.run(cmd_unowned, env={"PATH": str(bin_dir)}, capture_output=True, text=True, check=False)
            self.assertNotEqual(res_unowned.returncode, 0)


class TestSec08Pam(unittest.TestCase):
    def setUp(self) -> None:
        self.pam_qml_path = SHELL_ROOT / "modules" / "lock" / "Pam.qml"
        self.pam_src = self.pam_qml_path.read_text(encoding="utf-8")
        self.engine = QJSEngine()
        js_code = _qml_functions_to_js(
            self.pam_src,
            [
                "isValidPamUsername",
                "isTrustedPamConfig",
                "buildFprintAvailCommand",
                "buildHowdyAvailCommand",
            ],
        )
        setup_script = f"""
var CUtils = {{
    securePaths: {{}},
    isSecureSystemFile: function(p) {{
        return !!this.securePaths[p];
    }}
}};
var root = {{}};
{js_code}
root.isValidPamUsername = isValidPamUsername;
root.isTrustedPamConfig = isTrustedPamConfig;
root.buildFprintAvailCommand = buildFprintAvailCommand;
root.buildHowdyAvailCommand = buildHowdyAvailCommand;
"""
        res = self.engine.evaluate(setup_script)
        self.assertFalse(res.isError(), msg=res.toString())

    def test_no_shell_path_pam_or_sh_c_or_pam_permit_in_lock_pam(self) -> None:
        # 1. Pam.qml must never use Quickshell.shellPath("assets/pam.d") or sh -c
        self.assertNotIn('Quickshell.shellPath("assets/pam.d")', self.pam_src)
        self.assertNotIn('"sh", "-c"', self.pam_src)
        self.assertNotIn("fprintd-list $USER", self.pam_src)
        self.assertNotIn("command -v howdy", self.pam_src)

        # 2. Password authentication must use /etc/pam.d and system-auth
        self.assertIn('config: "system-auth"', self.pam_src)
        self.assertIn('configDirectory: "/etc/pam.d"', self.pam_src)

        # 3. Biometric contexts must use /etc/xdg/quickshell/helysium/assets/pam.d verified by isTrustedPamConfig
        self.assertIn('configDirectory: "/etc/xdg/quickshell/helysium/assets/pam.d"', self.pam_src)
        self.assertIn("root.isTrustedPamConfig(pam.configDirectory, pam.config)", self.pam_src)

        # 4. Shipped PAM configs in assets/pam.d/ must never contain pam_permit.so or sufficient bypasses
        for name, expected_mod in (("passwd", "pam_unix.so"), ("fprint", "pam_fprintd.so"), ("howdy", "pam_howdy.so")):
            content = (SHELL_ROOT / "assets" / "pam.d" / name).read_text(encoding="utf-8")
            self.assertNotIn("pam_permit.so", content, f"assets/pam.d/{name} must not contain pam_permit.so")
            self.assertNotIn("sufficient", content, f"assets/pam.d/{name} must not contain sufficient bypass")
            self.assertIn(expected_mod, content)

    def test_user_directory_pam_config_is_never_trusted(self) -> None:
        # Even if CUtils.isSecureSystemFile were mocked true for a path, isTrustedPamConfig
        # strictly whitelists /etc/pam.d and /etc/xdg/quickshell/helysium/assets/pam.d
        self.engine.evaluate(
            """
            CUtils.securePaths = {
                "/etc/pam.d/system-auth": true,
                "/etc/xdg/quickshell/helysium/assets/pam.d/fprint": true,
                "/etc/xdg/quickshell/helysium/assets/pam.d/howdy": true,
                "/home/user/.config/quickshell/helysium/assets/pam.d/fprint": true,
                "/tmp/pam.d/fprint": true
            };
            """
        )
        self.assertTrue(self.engine.evaluate('root.isTrustedPamConfig("/etc/pam.d", "system-auth")').toBool())
        self.assertTrue(
            self.engine.evaluate('root.isTrustedPamConfig("/etc/xdg/quickshell/helysium/assets/pam.d", "fprint")').toBool()
        )
        self.assertTrue(
            self.engine.evaluate('root.isTrustedPamConfig("/etc/xdg/quickshell/helysium/assets/pam.d", "howdy")').toBool()
        )

        untrusted_cases = [
            ("/home/user/.config/quickshell/helysium/assets/pam.d", "fprint"),
            (str(SHELL_ROOT / "assets" / "pam.d"), "fprint"),
            ("/tmp/pam.d", "fprint"),
            ("/etc/pam.d/../tmp", "fprint"),
            ("/etc/xdg/quickshell/helysium/assets/pam.d", "../fprint"),
            ("/etc/xdg/quickshell/helysium/assets/pam.d", "fprint/../../evil"),
            ("/etc/xdg/quickshell/helysium/assets/pam.d", "."),
            ("/etc/xdg/quickshell/helysium/assets/pam.d", ".."),
            ("/etc/xdg/quickshell/helysium/assets/pam.d", ""),
            ("", "fprint"),
        ]
        for cfg_dir, cfg_name in untrusted_cases:
            ok = self.engine.evaluate(
                f"root.isTrustedPamConfig({json.dumps(cfg_dir)}, {json.dumps(cfg_name)})"
            ).toBool()
            self.assertFalse(ok, f"Must reject untrusted PAM config ({cfg_dir!r}, {cfg_name!r})")

        # When CUtils.isSecureSystemFile returns false (missing, symlink, or user-writable), it must be rejected
        self.engine.evaluate('CUtils.securePaths["/etc/xdg/quickshell/helysium/assets/pam.d/fprint"] = false;')
        self.assertFalse(
            self.engine.evaluate('root.isTrustedPamConfig("/etc/xdg/quickshell/helysium/assets/pam.d", "fprint")').toBool()
        )

    def test_fprint_and_howdy_commands_use_direct_argv_without_shell(self) -> None:
        self.assertEqual(
            json.loads(self.engine.evaluate('JSON.stringify(root.buildHowdyAvailCommand())').toString()),
            ["which", "--", "howdy"],
        )
        for valid_user in ("icael", "user_01", "john.doe", "nix-user", "machine$"):
            cmd = json.loads(
                self.engine.evaluate(f"JSON.stringify(root.buildFprintAvailCommand({json.dumps(valid_user)}))").toString()
            )
            self.assertEqual(cmd, ["fprintd-list", "--", valid_user])

        malicious_users = [
            "",
            "-h",
            "--help",
            "user; id",
            "user && touch /tmp/pwn",
            "$(touch /tmp/pwn)",
            "`id`",
            "user\nid",
            "user name",
            "../root",
            "a" * 33,
        ]
        for bad_user in malicious_users:
            cmd = json.loads(
                self.engine.evaluate(f"JSON.stringify(root.buildFprintAvailCommand({json.dumps(bad_user)}))").toString()
            )
            self.assertEqual(cmd, [], f"Should have rejected invalid username: {bad_user!r}")

    def test_cutils_verify_secure_system_file_rejects_symlinks_user_files_and_writable_files(self) -> None:
        gpp = shutil.which("g++")
        if not gpp:
            self.skipTest("g++ not available")

        cutils_cpp = (SHELL_ROOT / "plugin" / "src" / "HelysIum" / "cutils.cpp").read_text(encoding="utf-8")
        match = re.search(r"bool verifySecureSystemFile\(const QString& path\)\s*\{", cutils_cpp)
        self.assertIsNotNone(match, "verifySecureSystemFile not found in cutils.cpp")
        assert match is not None

        brace_start = cutils_cpp.index("{", match.start())
        depth = 0
        end = brace_start
        for idx in range(brace_start, len(cutils_cpp)):
            if cutils_cpp[idx] == "{":
                depth += 1
            elif cutils_cpp[idx] == "}":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break
        verify_fn_code = cutils_cpp[match.start() : end]

        harness_cpp = f"""
#include <qstring.h>
#include <qstringlist.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

static int test_fstat(int fd, struct stat* st) {{
    const int r = ::fstat(fd, st);
    if (r == 0) {{
        const char* treat_uid_as_root = ::getenv("TREAT_UID_AS_ROOT");
        if (treat_uid_as_root && *treat_uid_as_root) {{
            const uid_t target = static_cast<uid_t>(::strtoul(treat_uid_as_root, nullptr, 10));
            if (st->st_uid == target) {{
                st->st_uid = 0;
            }}
        }}
        const char* force_root_dirs = ::getenv("FORCE_ALL_DIRS_ROOT_0755");
        if (force_root_dirs && *force_root_dirs == '1' && S_ISDIR(st->st_mode)) {{
            const char* skip_dir_ino = ::getenv("KEEP_REAL_MODE_INO");
            const ino_t skip_ino = skip_dir_ino ? static_cast<ino_t>(::strtoull(skip_dir_ino, nullptr, 10)) : 0;
            if (st->st_ino != skip_ino) {{
                st->st_uid = 0;
                st->st_mode = (st->st_mode & ~static_cast<mode_t>(0777)) | 0755;
            }}
        }}
    }}
    return r;
}}
#define fstat test_fstat

{verify_fn_code}

#undef fstat

int main(int argc, char** argv) {{
    if (argc < 2) return 2;
    ::umask(0000);
    return verifySecureSystemFile(QString::fromUtf8(argv[1])) ? 0 : 1;
}}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src_file = tmp / "test_verify_pam.cpp"
            bin_file = tmp / "test_verify_pam"
            src_file.write_text(harness_cpp, encoding="utf-8")

            compile_res = subprocess.run(
                [
                    gpp,
                    "-std=c++20",
                    "-fPIC",
                    "-I/usr/include/qt6",
                    "-I/usr/include/qt6/QtCore",
                    str(src_file),
                    "-o",
                    str(bin_file),
                    "-lQt6Core",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(compile_res.returncode, 0, msg=compile_res.stderr)

            pam_dir = tmp / "etc" / "pam.d"
            pam_dir.mkdir(parents=True, mode=0o755)
            pam_dir.chmod(0o755)
            pam_file = pam_dir / "fprint"
            pam_file.write_text("#%PAM-1.0\nauth required pam_fprintd.so max-tries=1\n", encoding="utf-8")
            pam_file.chmod(0o644)

            # 1. File owned by non-root user (without UID map) -> MUST BE REJECTED
            res_unowned = subprocess.run([str(bin_file), str(pam_file)], check=False)
            self.assertNotEqual(res_unowned.returncode, 0, "User-owned PAM file must be rejected")

            # Simulated root environment for testing root-owned 0644 file vs symlinks & writable permissions
            root_env = dict(
                os.environ,
                TREAT_UID_AS_ROOT=str(os.geteuid()),
                FORCE_ALL_DIRS_ROOT_0755="1",
            )

            # 2. Valid root-owned 0644 regular file in root-owned 0755 directory -> ACCEPTED
            res_ok = subprocess.run([str(bin_file), str(pam_file)], env=root_env, check=False)
            self.assertEqual(res_ok.returncode, 0, "Root-owned 0644 regular PAM file must be accepted")

            # 3. PAM file replaced by a symlink -> MUST BE REJECTED (O_NOFOLLOW)
            sym_pam = pam_dir / "fprint_symlink"
            sym_pam.symlink_to(pam_file)
            res_sym = subprocess.run([str(bin_file), str(sym_pam)], env=root_env, check=False)
            self.assertNotEqual(res_sym.returncode, 0, "Symlinked PAM file must be rejected")

            # 4. Intermediate directory replaced by a symlink -> MUST BE REJECTED (O_DIRECTORY | O_NOFOLLOW)
            sym_dir = tmp / "etc" / "pam_symlink.d"
            sym_dir.symlink_to(pam_dir)
            res_sym_dir = subprocess.run([str(bin_file), str(sym_dir / "fprint")], env=root_env, check=False)
            self.assertNotEqual(res_sym_dir.returncode, 0, "PAM file inside symlinked directory must be rejected")

            # 5. Group-writable (0664) or world-writable (0666) PAM file -> MUST BE REJECTED
            for bad_mode in (0o664, 0o666, 0o646):
                pam_file.chmod(bad_mode)
                res_writable = subprocess.run([str(bin_file), str(pam_file)], env=root_env, check=False)
                self.assertNotEqual(
                    res_writable.returncode,
                    0,
                    f"PAM file with writable mode {bad_mode:o} must be rejected",
                )
            pam_file.chmod(0o644)

            # 6. Group-writable (0775) or world-writable (0777) parent directory -> MUST BE REJECTED
            for bad_dir_mode in (0o775, 0o777, 0o757):
                pam_dir.chmod(bad_dir_mode)
                env_bad_dir = dict(root_env, KEEP_REAL_MODE_INO=str(pam_dir.stat().st_ino))
                res_bad_dir = subprocess.run([str(bin_file), str(pam_file)], env=env_bad_dir, check=False)
                self.assertNotEqual(
                    res_bad_dir.returncode,
                    0,
                    f"PAM file in directory with writable mode {bad_dir_mode:o} must be rejected",
                )
            pam_dir.chmod(0o755)

    def test_biometric_absence_or_failure_falls_back_cleanly_to_password(self) -> None:
        # Extract handleKey, restartFprint, clearTransientState and simulate the full lock interaction
        js_lock_funcs = _qml_functions_to_js(
            self.pam_src,
            [
                "handleKey",
                "restartFprint",
                "clearTransientState",
                "isValidPamUsername",
                "isTrustedPamConfig",
                "buildFprintAvailCommand",
                "buildHowdyAvailCommand",
            ],
        )
        sim_engine = QJSEngine()
        sim_script = f"""
var Qt = {{ Key_Enter: 16777221, Key_Return: 16777220, Key_Backspace: 16777219, Key_U: 85, ControlModifier: 0x04000000 }};
var Pam = {{ None: 0, Error: 1, MaxTries: 2, Failed: 3 }};
var PamResult = {{ Success: 0, Failed: 1, Error: 2, MaxTries: 3 }};

var CUtils = {{
    securePaths: {{}},
    isSecureSystemFile: function(p) {{ return !!this.securePaths[p]; }}
}};

var passwdStarted = 0;
var unlocked = false;
var passwd = {{
    active: false,
    start: function() {{ this.active = true; passwdStarted++; }},
    abort: function() {{ this.active = false; }}
}};

function makeBio(cfgName, maxT) {{
    return {{
        configDirectory: "/etc/xdg/quickshell/helysium/assets/pam.d",
        config: cfgName,
        enabled: true,
        maxTries: maxT,
        available: false,
        active: false,
        tries: 0,
        errorTries: 0,
        state: Pam.None,
        get canAttempt() {{ return this.available && this.enabled && this.tries < this.maxTries; }},
        checkAvailable: function() {{
            if (!root.isTrustedPamConfig(this.configDirectory, this.config)) {{
                this.available = false;
                return;
            }}
            this.available = true;
        }},
        start: function() {{
            if (!root.isTrustedPamConfig(this.configDirectory, this.config)) {{
                this.available = false;
                return;
            }}
            this.active = true;
        }},
        abort: function() {{ this.active = false; }},
        reset: function() {{ this.tries = 0; this.errorTries = 0; this.state = Pam.None; }},
        simulateFail: function() {{
            this.active = false;
            root.clearTransientState();
            this.tries++;
            if (this.tries < this.maxTries) {{
                this.state = Pam.Failed;
            }} else {{
                this.state = Pam.MaxTries;
            }}
        }}
    }};
}}

var fprint = makeBio("fprint", 3);
var howdy = makeBio("howdy", 2);
var state = Pam.None;
var buffer = "";
var root = {{
    state: Pam.None,
    buffer: "",
    fprint: fprint,
    howdy: howdy,
    passwd: passwd,
    lock: {{ unlock: function() {{ unlocked = true; }} }}
}};

{js_lock_funcs}

root.handleKey = handleKey;
root.restartFprint = restartFprint;
root.clearTransientState = clearTransientState;
root.isValidPamUsername = isValidPamUsername;
root.isTrustedPamConfig = isTrustedPamConfig;
"""
        res = sim_engine.evaluate(sim_script)
        self.assertFalse(res.isError(), msg=res.toString())

        # Case 1: Both fprintd and howdy are absent/untrusted -> password typing and Enter still starts passwd
        sim_engine.evaluate(
            """
            fprint.checkAvailable();
            howdy.checkAvailable();
            handleKey({ key: 0, modifiers: 0, text: "s" });
            handleKey({ key: 0, modifiers: 0, text: "e" });
            handleKey({ key: 0, modifiers: 0, text: "c" });
            handleKey({ key: Qt.Key_Return, modifiers: 0, text: "" });
            """
        )
        self.assertFalse(sim_engine.evaluate("fprint.available").toBool())
        self.assertFalse(sim_engine.evaluate("howdy.available").toBool())
        self.assertEqual(sim_engine.evaluate("buffer").toString(), "sec")
        self.assertEqual(sim_engine.evaluate("passwdStarted").toInt(), 1)

        # Case 2: Biometrics are available, howdy is active, then biometric attempts fail up to MaxTries ->
        # typing password aborts active howdy and pressing Enter authenticates via passwd
        sim_engine.evaluate(
            """
            passwd.active = false;
            buffer = "";
            CUtils.securePaths["/etc/xdg/quickshell/helysium/assets/pam.d/fprint"] = true;
            CUtils.securePaths["/etc/xdg/quickshell/helysium/assets/pam.d/howdy"] = true;
            fprint.checkAvailable();
            howdy.checkAvailable();
            fprint.simulateFail();
            fprint.simulateFail();
            fprint.simulateFail(); // fprint reaches MaxTries
            howdy.start(); // howdy is scanning
            """
        )
        self.assertEqual(sim_engine.evaluate("fprint.state").toInt(), 2)  # Pam.MaxTries
        self.assertTrue(sim_engine.evaluate("howdy.active").toBool())

        # Typing password character immediately aborts howdy and appends to buffer; Enter triggers passwd.start()
        sim_engine.evaluate(
            """
            handleKey({ key: 0, modifiers: 0, text: "p" });
            handleKey({ key: 0, modifiers: 0, text: "w" });
            handleKey({ key: Qt.Key_Enter, modifiers: 0, text: "" });
            """
        )
        self.assertFalse(sim_engine.evaluate("howdy.active").toBool())
        self.assertEqual(sim_engine.evaluate("buffer").toString(), "pw")
        self.assertEqual(sim_engine.evaluate("passwdStarted").toInt(), 2)


class TestSec08IpcLock(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_qml = (SHELL_ROOT / "modules" / "lock" / "Lock.qml").read_text(encoding="utf-8")
        self.lock_surface_qml = (SHELL_ROOT / "modules" / "lock" / "LockSurface.qml").read_text(encoding="utf-8")
        self.pam_qml = (SHELL_ROOT / "modules" / "lock" / "Pam.qml").read_text(encoding="utf-8")

    def test_ipc_handler_and_shortcut_do_not_expose_unauthenticated_unlock(self) -> None:
        # 1. IpcHandler target "lock" must NOT expose function unlock()
        start = self.lock_qml.index("IpcHandler {")
        brace_start = self.lock_qml.index("{", start)
        depth = 0
        end = brace_start
        for idx in range(brace_start, len(self.lock_qml)):
            if self.lock_qml[idx] == "{":
                depth += 1
            elif self.lock_qml[idx] == "}":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break
        ipc_block = self.lock_qml[start:end]
        self.assertIn('target: "lock"', ipc_block)
        self.assertIn("function lock(): void", ipc_block)
        self.assertIn("function isLocked(): bool", ipc_block)
        self.assertNotIn("function unlock", ipc_block)
        self.assertNotIn("lock.unlock()", ipc_block)

        # 2. CustomShortcut "lock" must remain, CustomShortcut "unlock" must be absent
        self.assertIn('name: "lock"', self.lock_qml)
        self.assertNotIn('name: "unlock"', self.lock_qml)

        # 3. Internal signal unlock on WlSessionLock remains intact for PAM and LockSurface
        self.assertIn("signal unlock", self.lock_qml)
        self.assertIn("return root.lock.unlock();", self.pam_qml)
        self.assertIn("function onUnlock(): void", self.lock_surface_qml)

    def test_lock_and_pam_unlock_behavioral_flow_without_ipc_bypass(self) -> None:
        engine = QJSEngine()
        ipc_js = _qml_functions_to_js(self.lock_qml, ["lock", "isLocked"])
        ipc_js_renamed = ipc_js.replace("function lock()", "function ipcLock()")
        sim = f"""
var lock = {{
    locked: false,
    unlockedCount: 0,
    unlock: function() {{
        this.unlockedCount++;
        this.locked = false;
    }}
}};
{ipc_js_renamed}
var ipc = {{
    lock: ipcLock,
    isLocked: isLocked
}};
"""
        res = engine.evaluate(sim)
        self.assertFalse(res.isError(), msg=res.toString())

        # Locking via IPC works and reports isLocked() === true
        self.assertFalse(engine.evaluate("ipc.isLocked()").toBool())
        engine.evaluate("ipc.lock()")
        self.assertTrue(engine.evaluate("ipc.isLocked()").toBool())

        # No IPC unlock method exists -> cannot unlock without PAM
        self.assertEqual(engine.evaluate("typeof ipc.unlock").toString(), "undefined")
        self.assertTrue(engine.evaluate("ipc.isLocked()").toBool())

        # Authentic PAM completion (PamResult.Success) unlocks cleanly via internal lock.unlock()
        engine.evaluate("lock.unlock()")
        self.assertFalse(engine.evaluate("ipc.isLocked()").toBool())
        self.assertEqual(engine.evaluate("lock.unlockedCount").toInt(), 1)

    def test_ipc_socket_directory_isolation_permissions(self) -> None:
        import socket

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "run_user_1000"
            runtime_dir.mkdir(mode=0o700)
            runtime_dir.chmod(0o700)
            qs_dir = runtime_dir / "quickshell" / "by-id" / "testinst"
            qs_dir.mkdir(parents=True, mode=0o700)
            sock_path = qs_dir / "ipc.sock"

            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                srv.bind(str(sock_path))
                srv.listen(1)
                self.assertEqual(runtime_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual(runtime_dir.stat().st_uid, os.geteuid())
                self.assertTrue(sock_path.exists())
            finally:
                srv.close()


class TestSec08IdleMonitors(unittest.TestCase):
    def setUp(self) -> None:
        self.idle_path = SHELL_ROOT / "daemons" / "IdleMonitors.qml"
        self.idle_src = self.idle_path.read_text(encoding="utf-8")

    def test_idle_monitors_blocks_unlock_action_and_preserves_legitimate_actions(self) -> None:
        # 1. Static checks: no `locked = false` anywhere in IdleMonitors.qml, SessionManager.onUnlockRequested preserved
        self.assertNotIn("locked = false", self.idle_src)
        self.assertIn("function onUnlockRequested(): void", self.idle_src)
        self.assertIn("root.lock.lock.unlock();", self.idle_src)

        # 2. Behavioral QJSEngine check of handleIdleAction extracted directly from IdleMonitors.qml
        js_fn = _qml_functions_to_js(self.idle_src, ["handleIdleAction"])
        engine = QJSEngine()
        sim = f"""
var dispatched = [];
var sessionExecCalls = [];
var detachedCalls = [];
var lock = {{ lock: {{ locked: false }} }};
var Hypr = {{
    usingLua: true,
    dispatch: function(cmd) {{ dispatched.push(cmd); }}
}};
var SessionManager = {{
    exec: function(cmd) {{
        sessionExecCalls.push(cmd);
        return cmd.length === 1 && cmd[0] === "suspendThenHibernate";
    }}
}};
var Quickshell = {{
    execDetached: function(cmd) {{ detachedCalls.push(cmd); }}
}};
{js_fn}
"""
        res = engine.evaluate(sim)
        self.assertFalse(res.isError(), msg=res.toString())

        # "lock" locks the session
        engine.evaluate('handleIdleAction("lock")')
        self.assertTrue(engine.evaluate("lock.lock.locked").toBool())

        # "unlock" MUST NOT unlock the session and MUST NOT dispatch or exec anything
        engine.evaluate('handleIdleAction("unlock")')
        self.assertTrue(engine.evaluate("lock.lock.locked").toBool())
        self.assertEqual(json.loads(engine.evaluate("JSON.stringify(dispatched)").toString()), [])
        self.assertEqual(json.loads(engine.evaluate("JSON.stringify(sessionExecCalls)").toString()), [])
        self.assertEqual(json.loads(engine.evaluate("JSON.stringify(detachedCalls)").toString()), [])

        # DPMS off / on still dispatch properly
        engine.evaluate('handleIdleAction("dpms off"); handleIdleAction("dpms on");')
        self.assertEqual(
            json.loads(engine.evaluate("JSON.stringify(dispatched)").toString()),
            [
                'hl.dsp.dpms({ action = "disable" })',
                'hl.dsp.dpms({ action = "enable" })',
            ],
        )

        # suspendThenHibernate still routes to SessionManager.exec
        engine.evaluate('handleIdleAction(["suspendThenHibernate"])')
        self.assertEqual(
            json.loads(engine.evaluate("JSON.stringify(sessionExecCalls)").toString()),
            [["suspendThenHibernate"]],
        )
        self.assertEqual(json.loads(engine.evaluate("JSON.stringify(detachedCalls)").toString()), [])


if __name__ == "__main__":
    unittest.main()
