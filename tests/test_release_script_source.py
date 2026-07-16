from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "Test-FlexShowRelease.ps1"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
README_PATH = ROOT / "README.md"
TOUCHDESIGNER_README_PATH = ROOT / "touchdesigner" / "README.md"


class ReleaseScriptSourceTests(unittest.TestCase):
    def test_release_script_composes_the_complete_source_gate(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        for marker in (
            "Get-FlexShowPython",
            "m.version('jsonschema') == '4.17.3'",
            "-m', 'compileall'",
            "tools/validate_configs.py",
            "'-m', 'unittest'",
            "tools/benchmark_flexshow.py",
            "Parser]::ParseFile",
            "Initialize-FlexShow.ps1",
            "-Scope Both -SelfTest",
            "-Scope History -Revision HEAD",
        ):
            self.assertIn(marker, source)

    def test_initializer_smoke_compares_the_written_contract(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        for marker in (
            "$written.topology -ne $result.topology",
            "$written.tier -ne $result.tier",
            "$written.gpu.ai.uuid -ne $result.ai_gpu.uuid",
            "$written.gpu.render.uuid -ne $result.render_gpu.uuid",
            "$written.transport.type -ne 'touch_tcp'",
        ):
            self.assertIn(marker, source)

    def test_release_script_uses_bounded_temporary_outputs(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("[guid]::NewGuid()", source)
        self.assertIn("config\\local-release-$verificationId.json", source)
        self.assertIn("finally {", source)
        self.assertIn("$temporaryParent.TrimEnd", source)
        self.assertIn("Remove-Item -LiteralPath $temporaryDirectory -Recurse", source)

    def test_release_script_does_not_mutate_git_or_install_dependencies(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8").casefold()
        for forbidden in (
            "git add",
            "git commit",
            "git push",
            "pip install",
            "allowcanonicalprojectupdate",
        ):
            self.assertNotIn(forbidden, source)

    def test_ci_and_readme_use_the_release_script(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        readme = README_PATH.read_text(encoding="utf-8")
        self.assertIn(
            r".\scripts\Test-FlexShowRelease.ps1 -SkipPublicSync",
            workflow,
        )
        self.assertIn(r".\scripts\Test-FlexShowRelease.ps1", readme)
        self.assertIn("does not launch TouchDesigner", readme)

    def test_live_validation_docs_require_an_in_process_context(self) -> None:
        for path in (README_PATH, TOUCHDESIGNER_README_PATH):
            source = path.read_text(encoding="utf-8")
            self.assertIn("live `op()` namespace", source)
            self.assertIn("expected_build='1.2.1'", source)
            self.assertIn("%Y%m%dT%H%M%S%fZ", source)


if __name__ == "__main__":
    unittest.main()
