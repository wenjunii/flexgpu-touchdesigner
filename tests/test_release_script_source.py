from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "Test-FlexShowRelease.ps1"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
README_PATH = ROOT / "README.md"
TOUCHDESIGNER_README_PATH = ROOT / "touchdesigner" / "README.md"
COMMON_SCRIPT_PATH = ROOT / "scripts" / "_FlexShow.Common.ps1"
START_SCRIPT_PATH = ROOT / "scripts" / "Start-FlexShow.ps1"
RECOVER_SCRIPT_PATH = ROOT / "scripts" / "Recover-FlexShow.ps1"
TEST_REQUIREMENTS_PATH = ROOT / "requirements-test.txt"
INITIALIZER_SCRIPT_PATH = ROOT / "scripts" / "Initialize-FlexShow.ps1"


class ReleaseScriptSourceTests(unittest.TestCase):
    def test_release_script_composes_the_complete_source_gate(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        for marker in (
            "Get-FlexShowPython",
            "m.version('jsonschema') == '4.17.3'",
            "Check NumPy source-test dependency",
            '"import numpy"',
            "-m', 'compileall'",
            "tools/validate_configs.py",
            "'-m', 'unittest'",
            "'discover', '-s', 'tests', '-q'",
            "tools/benchmark_flexshow.py",
            "Parser]::ParseFile",
            "Test-TDKnowledgeBridge.ps1",
            "Smoke-test TD Knowledge bridge public wiring",
            "Initialize-FlexShow.ps1",
            "Smoke-test TouchDesigner version inventory and selectors",
            "Smoke-test Depth Anything wrapper accepted rehearsal defaults",
            "Smoke-test Start/Recover readiness arguments",
            "-Scope Both -SelfTest",
            "-Scope History -Revision HEAD",
        ):
            self.assertIn(marker, source)

    def test_readiness_wait_is_forwarded_only_when_supplied(self) -> None:
        common = COMMON_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("[string]([int]$WaitReadyMs)", common)
        self.assertNotIn("$WaitReadyMs.Value", common)

        for path in (START_SCRIPT_PATH, RECOVER_SCRIPT_PATH):
            source = path.read_text(encoding="utf-8")
            self.assertIn("$PSBoundParameters.ContainsKey('WaitReadyMs')", source)
            self.assertIn(
                "$invokeArguments['WaitReadyMs'] = [int]$WaitReadyMs",
                source,
            )
            self.assertNotIn("-WaitReadyMs $WaitReadyMs", source)

    def test_initializer_smoke_compares_the_written_contract(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        for marker in (
            "$written.topology -ne $result.topology",
            "$written.tier -ne $result.tier",
            "$written.gpu.ai.uuid -ne $result.ai_gpu.uuid",
            "$written.gpu.render.uuid -ne $result.render_gpu.uuid",
            "$written.processes.ai.executable -ne $fakeTouchDesigner",
            "$written.source.geometry_provider -ne 'depth_anything'",
            "$written.source.frame_state_operator -ne 'DEPTH_ANYTHING_GEOMETRY_BRIDGE/FRAME_STATE'",
            "$written.render.display_mode -ne 'panoramic_wrap'",
            "$written.render.installation_width -ne 1920",
            "$written.render.triple_surface_width -ne 1920",
            "$result.display_profile -ne 'venue_1080p'",
            "$written.transport.type -ne 'touch_tcp'",
            "touchdesigner_version -ne $versionCase.Version",
            "touchdesigner_selection -ne 'validated_baseline'",
            "$versionWritten.processes.psobject.Properties",
            "$previousPath = $env:PATH",
            "automatically selected an unvalidated TouchDesigner candidate",
            "2025.32820",
            "2025.33060",
        ):
            self.assertIn(marker, source)

    def test_initializer_has_deterministic_touchdesigner_build_selection(self) -> None:
        source = INITIALIZER_SCRIPT_PATH.read_text(encoding="utf-8-sig")
        for marker in (
            "$TouchDesignerVersion",
            "$ListTouchDesigner",
            "$Project",
            "$DisplayProfile",
            "$DisplayMode",
            "$GeometryProvider",
            "'tier_default', 'venue_1080p'",
            "'single', 'panoramic_wrap', 'artistic_multi_angle'",
            "'moge2', 'depth_anything'",
            "geometry_provider = $GeometryProvider",
            "$render.installation_width = 1920",
            "$render.triple_surface_width = 1920",
            "$validatedTouchDesignerVersion = '2025.32820'",
            "Get-TouchDesignerInstallations",
            "Get-TouchDesignerVersion",
            "selection = 'validated_baseline'",
            "selection = 'explicit_version'",
            "touchdesigner_version = $touchDesignerSelection.version",
            "touchdesigner_selection = $touchDesignerSelection.selection",
            "project = $projectPath",
            "cannot be combined. Use one exact selector",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("Sort-Object -Unique)[-1]", source)
        self.assertNotIn("selection = 'sole_installation'", source)

    def test_release_script_uses_bounded_temporary_outputs(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("[guid]::NewGuid()", source)
        self.assertIn("config\\local-release-$verificationId.json", source)
        self.assertIn("finally {", source)
        self.assertIn("$temporaryParent.TrimEnd", source)
        self.assertIn("Remove-Item -LiteralPath $temporaryDirectory -Recurse", source)
        self.assertIn("Join-Path $TemporaryDirectory 'runtime'", source)
        self.assertIn("$written.runtime_dir = $releaseRuntime", source)

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
        requirements = TEST_REQUIREMENTS_PATH.read_text(encoding="utf-8")
        self.assertIn(
            r".\scripts\Test-FlexShowRelease.ps1 -SkipPublicSync",
            workflow,
        )
        self.assertIn("-r requirements-test.txt", workflow)
        self.assertIn(r"-r .\requirements-test.txt", readme)
        self.assertIn("jsonschema==4.17.3", requirements)
        self.assertIn("numpy==2.2.6", requirements)
        self.assertIn("Pillow==10.4.0", requirements)
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
