"""Exercise the local MCP interpreter probe without TD or an installed MCP SDK."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
SCRIPT = ROOT / "scripts" / "Test-TDKnowledgeBridge.ps1"


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows PowerShell probe")
class TDKnowledgeRuntimeProbeTests(unittest.TestCase):
    def run_probe(self, server_source, timeout_ms=5000):
        with tempfile.TemporaryDirectory(prefix="flexgpu-mcp-probe-") as directory:
            folder = Path(directory)
            server = folder / "server with spaces.py"
            server.write_text(server_source, encoding="utf-8")
            registry = folder / "envoy.json"
            registry.write_text(json.dumps({"active": "", "instances": {}}))
            config = folder / "config.toml"
            args = [
                "-u", str(server), "--port", "9870", "--envoy-config", str(registry),
                "--faiss-db", str(folder), "--project-context",
                str(ROOT / "integrations/embody/flexgpu-project-context.json"),
            ]
            config.write_text(
                "[mcp_servers.td-knowledge]\ncommand = " + json.dumps(sys.executable)
                + "\nargs = [\n" + "".join("  " + json.dumps(a) + ",\n" for a in args)
                + "]\n", encoding="utf-8",
            )
            return subprocess.run(
                [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
                 "-Config", str(config), "-RuntimeTimeoutMs", str(timeout_ms)],
                cwd=ROOT, capture_output=True, text=True, timeout=20,
            )

    def test_runtime_import_failure_is_not_reported_as_valid_wiring(self):
        result = self.run_probe("raise ImportError('synthetic missing MCP runtime')\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot load the server CLI", result.stderr)
        self.assertNotIn("synthetic missing MCP runtime", result.stderr)

    def test_runtime_probe_runs_help_only_and_keeps_offline_envoy_distinct(self):
        result = self.run_probe(
            "import sys\nassert sys.argv[1:] == ['--help']\nprint('usage: synthetic server')\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["python_runtime"], "ready")
        self.assertEqual(report["local_config"], "valid")
        self.assertEqual(report["envoy"], "offline")
        self.assertNotIn("usage: synthetic server", result.stdout)

    def test_runtime_probe_timeout_is_bounded(self):
        result = self.run_probe("import time\ntime.sleep(10)\n", timeout_ms=100)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("probe timed out", result.stderr)


if __name__ == "__main__":
    unittest.main()
