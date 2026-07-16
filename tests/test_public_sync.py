from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_public_sync_tool():
    path = ROOT / "tools" / "check_public_sync.py"
    spec = importlib.util.spec_from_file_location("check_public_sync", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load tools/check_public_sync.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class PublicSyncPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = load_public_sync_tool()

    def test_policy_self_test_passes(self) -> None:
        self.assertEqual(self.tool.run_self_test(), [])

    def test_private_paid_credentials_and_weights_are_blocked(self) -> None:
        blocked = (
            "local-components/StreamDiffusionTD.tox",
            "paid/vendor/plugin.dll",
            "licensed/sdk/runtime.dll",
            "private/show-notes.txt",
            "credentials.json",
            "service-account-production.json",
            ".env.production",
            ".env.sample",
            "weights/model.gguf",
            "keys/operator.pem",
            "config/local-flexshow.json",
            "captures/audience/frame-0001.ppm",
            "commissioning/site-a/manifest.json",
            "recordings/rehearsal/depth.pgm",
        )
        for path in blocked:
            with self.subTest(path=path):
                self.assertTrue(self.tool.path_findings(path))

    def test_public_source_assets_and_placeholder_env_are_allowed(self) -> None:
        allowed = (
            "README.md",
            "src/flexgpu/config.py",
            "assets/original-public-texture.png",
            ".env.example",
            "projects/FlexShow.toe",
        )
        for path in allowed:
            with self.subTest(path=path):
                self.assertEqual(self.tool.path_findings(path), [])

    def test_provider_tokens_private_keys_and_assignments_are_detected(self) -> None:
        payloads = (
            b"gh" + b"p_" + (b"A" * 36),
            b"AK" + b"IA" + (b"A1" * 8),
            b"sk-" + (b"z" * 32),
            b"-----BEGIN " + b"PRIVATE KEY-----\n" + (b"A" * 40),
            b"-----BEGIN " + b"ENCRYPTED PRIVATE KEY-----\n" + (b"B" * 40),
            b"-----BEGIN " + b"DSA PRIVATE KEY-----\n" + (b"C" * 40),
            b'{"api_' + b'key":"' + (b"K8z_" * 8) + b'"}',
            b"client_" + b"secret: " + (b"Z7x_" * 8),
            b"postgresql://operator:" + (b"P4s_" * 6) + b"@db.internal/show",
        )
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                findings = self.tool.content_findings("fixture.txt", payload)
                self.assertTrue(findings)
                self.assertNotIn(payload.decode("ascii"), repr(findings))

    def test_diffusion_provider_and_namespaced_credentials_are_detected(self) -> None:
        payloads = {
            "huggingface-provider": b"hf" + b"_" + (b"H" * 32),
            "npm-provider": b"npm" + b"_" + (b"N" * 32),
            "pypi-provider": b"pypi" + b"-" + (b"P" * 32),
            "gitlab-provider": b"glpat" + b"-" + (b"G" * 32),
            "huggingface-namespaced": (
                b'{"huggingface_' + b'token":"' + (b"H7x_" * 8) + b'"}'
            ),
            "npm-namespaced": b'{"npm_' + b'token":"' + (b"N7x_" * 8) + b'"}',
            "pypi-namespaced": b'{"pypi_' + b'token":"' + (b"P7x_" * 8) + b'"}',
            "gitlab-namespaced": (
                b'{"gitlab_' + b'token":"' + (b"G7x_" * 8) + b'"}'
            ),
            "license-key": b'{"license_' + b'key":"' + (b"L8z_" * 8) + b'"}',
            "quoted-hash-password": b'password="abc#' + (b"V7q_" * 6) + b'"',
        }
        for name, payload in payloads.items():
            with self.subTest(name=name):
                findings = self.tool.content_findings("fixture.txt", payload)
                self.assertTrue(findings)
                self.assertNotIn(payload.decode("ascii"), repr(findings))

    def test_minified_json_secret_after_public_property_is_detected(self) -> None:
        payload = b'{"safe":true,"api_' + b'key":"' + (b"K8z_" * 8) + b'"}'
        findings = self.tool.content_findings("fixture.json", payload)
        self.assertTrue(any(item.rule == "assigned-secret" for item in findings))

    def test_placeholders_are_not_treated_as_credentials(self) -> None:
        payload = (
            b"api_key=${API_KEY}\n"
            b"client_secret=<provided-at-runtime>\n"
            b'token = os.environ["TOKEN"]\n'
            b"token=REPLACE_WITH_LOCAL_VALUE\n"
        )
        self.assertEqual(self.tool.content_findings(".env.example", payload), [])

    def test_renamed_structured_local_artifacts_are_blocked_by_content(self) -> None:
        fixtures = {
            "hardware": (
                {
                    "version": "flexgpu-hardware-profile/v1",
                    "captured_ns": 1,
                    "gpus": [{"uuid": "GPU-local-machine"}],
                    "recommendation": {},
                },
                "local-hardware-profile",
            ),
            "commissioning": (
                {
                    "version": "flexgpu-commissioning/v1",
                    "created_ns": 1,
                    "source": {},
                    "calibration": {},
                    "frames": [],
                },
                "local-commissioning-manifest",
            ),
            "frame": (
                {
                    "version": "flexgpu-frame-state/v1",
                    "session_id": "local-session",
                    "frame_id": 1,
                    "timestamp_ns": 1,
                    "calibration_id": "camera",
                    "calibration_digest": "a" * 64,
                },
                "local-frame-state",
            ),
            "runtime": (
                {
                    "version": 3,
                    "session_id": "local-session",
                    "state": "running",
                    "started_at": "local",
                    "updated_at": "local",
                    "config": "C:/private/show.json",
                    "planned_roles": ["world"],
                    "processes": [{"pid": 1234}],
                },
                "local-runtime-manifest",
            ),
            "telemetry": (
                {
                    "schema_version": 1,
                    "timestamp": 1.0,
                    "frame_time_ms": 16.7,
                    "vram_used_mib": 1000,
                    "vram_total_mib": 16000,
                    "queue_age_ms": 2,
                    "quality_level": 2,
                },
                "local-telemetry-artifact",
            ),
            "support": (
                {"artifact_type": "support-bundle", "diagnostics": []},
                "local-support-artifact",
            ),
            "capture": (
                {"artifact_type": "audience-capture", "capture_id": "local"},
                "local-capture-artifact",
            ),
            "td-validation": (
                {"version": "flexgpu-td-validation/v1", "checks": []},
                "local-validation-report",
            ),
        }
        private_value = "audience-machine-precise-identifier"
        for name, (payload, expected_rule) in fixtures.items():
            payload["machine_name"] = private_value
            with self.subTest(name=name):
                findings = self.tool.content_findings(
                    "docs/innocent-%s.json" % name,
                    json.dumps(payload).encode("utf-8"),
                )
                self.assertTrue(any(item.rule == expected_rule for item in findings))
                self.assertNotIn(private_value, repr(findings))

    def test_current_and_future_runtime_manifests_are_detected_by_shape(self) -> None:
        manifest = {
            "version": 6,
            "session_id": "local-session",
            "state": "running",
            "started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:01Z",
            "config": "C:/private/show.json",
            "planned_roles": ["world"],
            "processes": [{"role": "world", "pid": 1234}],
        }
        for version in (6, 7, 999):
            with self.subTest(version=version):
                manifest["version"] = version
                findings = self.tool.content_findings(
                    "docs/renamed-state.json",
                    json.dumps(manifest).encode("utf-8"),
                )
                self.assertTrue(
                    any(item.rule == "local-runtime-manifest" for item in findings)
                )

    def test_ordinary_versioned_config_is_not_a_runtime_manifest(self) -> None:
        config = {
            "version": 7,
            "state": "enabled",
            "config": "config/flexshow.example.json",
            "planned_roles": ["world"],
            "processes": {"world": {"command": ["python", "show.py"]}},
            "topology": "single",
            "experience": "installation",
        }
        findings = self.tool.content_findings(
            "config/example.json", json.dumps(config).encode("utf-8")
        )
        self.assertFalse(
            any(item.rule == "local-runtime-manifest" for item in findings)
        )

    def test_only_exact_public_synthetic_calibration_fixture_is_allowed(self) -> None:
        fixture_path = ROOT / "config" / "calibration.example.json"
        payload = fixture_path.read_bytes()
        self.assertFalse(
            any(
                item.rule == "local-calibration-profile"
                for item in self.tool.content_findings(
                    "config/calibration.example.json", payload
                )
            )
        )
        renamed = self.tool.content_findings("docs/camera.json", payload)
        self.assertTrue(
            any(item.rule == "local-calibration-profile" for item in renamed)
        )

        changed = json.loads(payload)
        changed["camera_to_world"][3] = 0.5
        changed_payload = json.dumps(changed).encode("utf-8")
        findings = self.tool.content_findings(
            "config/calibration.example.json", changed_payload
        )
        self.assertTrue(
            any(item.rule == "local-calibration-profile" for item in findings)
        )

    def test_jsonl_telemetry_is_detected_after_rename_without_value_exposure(self) -> None:
        private_value = "local-stage-machine-name"
        records = [
            {
                "schema_version": 1,
                "timestamp": index,
                "frame_time_ms": 16.0,
                "vram_used_mib": 8000,
                "vram_total_mib": 16000,
                "queue_age_ms": 1.0,
                "quality_level": 2,
                "metadata": {"machine": private_value},
            }
            for index in range(2)
        ]
        payload = b"\n".join(json.dumps(record).encode("utf-8") for record in records)
        findings = self.tool.content_findings("assets/notes.txt", payload)
        self.assertTrue(
            any(item.rule == "local-telemetry-artifact" for item in findings)
        )
        self.assertNotIn(private_value, repr(findings))

    def test_placeholder_recognition_is_exact_and_fail_closed(self) -> None:
        placeholders = (
            b"password=CHANGEME\n"
            b"password=REDACTED\n"
            b"password=not-a-secret\n"
            b"password=${PASSWORD}\n"
            b"password=YOUR_PASSWORD\n"
            b"password=<provided-at-runtime>\n"
        )
        self.assertEqual(
            self.tool.content_findings(".env.example", placeholders), []
        )

        real_values = (
            b"exampleRealPassword123",
            b"test_actual_secret_123",
            b"AAAAAAAA",
            b"abababababab",
        )
        for value in real_values:
            with self.subTest(value=value):
                findings = self.tool.content_findings(
                    "fixture.env", b"password=" + value
                )
                self.assertTrue(any(item.rule == "assigned-secret" for item in findings))

    def test_int_wrapper_does_not_hide_a_credential_literal(self) -> None:
        assignment = b"pass" + b"word="
        for expression in (
            b"int(" + b'"12345678"' + b")",
            b"int(" + b"'87654321'" + b")",
            b"int(" + b"12345678" + b")",
        ):
            with self.subTest(expression=expression):
                findings = self.tool.content_findings(
                    "fixture.py", assignment + expression
                )
                self.assertTrue(
                    any(item.rule == "assigned-secret" for item in findings)
                )

    def test_narrow_int_environment_and_runtime_expressions_remain_allowed(self) -> None:
        payload = (
            b'token = int(os.getenv("TOKEN"))\n'
            b'token = os.getenv("TOKEN", "${TOKEN}")\n'
            b'token = os.environ.get("TOKEN", "CHANGEME")\n'
            b"token = int(slot.get('fallback_counter', -1)) + 1\n"
        )
        self.assertEqual(self.tool.content_findings("runtime.py", payload), [])

    def test_environment_getter_nonplaceholder_defaults_are_credentials(self) -> None:
        assignment = b"pass" + b"word="
        lookup = b"get" + b"env"
        expressions = (
            b"os." + lookup + b'("PASSWORD", "actual-secret-123")',
            b"int(os." + lookup + b'("PASSWORD", "12345678"))',
            lookup + b"('PASSWORD', 'fallback-secret')",
            b"system." + lookup + b'("PASSWORD", "fallback-secret")',
            b"os.environ.get(" + b'"PASSWORD", "fallback-secret")',
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                findings = self.tool.content_findings(
                    "fixture.py", assignment + expression
                )
                self.assertTrue(
                    any(item.rule == "assigned-secret" for item in findings)
                )

    def test_environment_getter_without_or_with_placeholder_default_is_allowed(self) -> None:
        assignment = b"to" + b"ken="
        lookup = b"get" + b"env"
        expressions = (
            b"os." + lookup + b'("TOKEN")',
            b"int(os." + lookup + b'("TOKEN"))',
            b"os." + lookup + b'("TOKEN", "${TOKEN}")',
            b"os.environ.get(" + b'"TOKEN", "CHANGEME")',
            b"system." + lookup + b'("TOKEN", None)',
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                self.assertEqual(
                    self.tool.content_findings(
                        "fixture.py", assignment + expression
                    ),
                    [],
                )

    def test_index_and_candidate_scans_use_their_exact_git_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(git(root, "init", "-q").returncode, 0)
            self.assertEqual(git(root, "config", "user.email", "test@example.com").returncode, 0)
            self.assertEqual(git(root, "config", "user.name", "Public Sync Test").returncode, 0)
            path = root / "settings.txt"
            path.write_text("public=true\n", encoding="utf-8")
            self.assertEqual(git(root, "add", "settings.txt").returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "safe").returncode, 0)

            secret = b"gh" + b"p_" + (b"Q" * 36)
            path.write_bytes(secret)
            index_findings, _ = self.tool.scan_repository(root, "index")
            candidate_findings, _ = self.tool.scan_repository(root, "candidates")
            self.assertEqual(index_findings, [])
            self.assertTrue(any(item.rule == "github-token" for item in candidate_findings))

    def test_history_scan_finds_a_removed_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(git(root, "init", "-q").returncode, 0)
            self.assertEqual(git(root, "config", "user.email", "test@example.com").returncode, 0)
            self.assertEqual(git(root, "config", "user.name", "Public Sync Test").returncode, 0)
            path = root / "old.txt"
            path.write_bytes(b"sk-" + (b"x" * 32))
            self.assertEqual(git(root, "add", "old.txt").returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "unsafe historical fixture").returncode, 0)
            path.write_text("removed\n", encoding="utf-8")
            self.assertEqual(git(root, "add", "old.txt").returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "remove fixture").returncode, 0)

            findings, _ = self.tool.scan_repository(root, "history")
            self.assertTrue(any(item.rule == "openai-token" for item in findings))
            self.assertTrue(all("sk-" not in repr(item) for item in findings))

    def test_history_scan_rechecks_public_calibration_blob_after_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(git(root, "init", "-q").returncode, 0)
            self.assertEqual(git(root, "config", "user.email", "test@example.com").returncode, 0)
            self.assertEqual(git(root, "config", "user.name", "Public Sync Test").returncode, 0)
            payload = (ROOT / "config" / "calibration.example.json").read_bytes()
            public = root / "config" / "calibration.example.json"
            renamed = root / "docs" / "camera.json"
            public.parent.mkdir()
            renamed.parent.mkdir()
            public.write_bytes(payload)
            renamed.write_bytes(payload)
            self.assertEqual(git(root, "add", ".").returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "calibration copies").returncode, 0)

            findings, _ = self.tool.scan_repository(root, "history")
            self.assertTrue(
                any(
                    item.rule == "local-calibration-profile"
                    and item.path == "docs/camera.json"
                    for item in findings
                )
            )
            self.assertFalse(
                any(
                    item.rule == "local-calibration-profile"
                    and item.path == "config/calibration.example.json"
                    for item in findings
                )
            )

    def test_history_scan_finds_a_secret_commit_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(git(root, "init", "-q").returncode, 0)
            self.assertEqual(git(root, "config", "user.email", "test@example.com").returncode, 0)
            self.assertEqual(git(root, "config", "user.name", "Public Sync Test").returncode, 0)
            (root / "README.md").write_text("safe\n", encoding="utf-8")
            self.assertEqual(git(root, "add", "README.md").returncode, 0)
            secret_message = "gh" + "p_" + ("M" * 36)
            self.assertEqual(
                git(root, "commit", "-qm", secret_message).returncode, 0
            )

            findings, _ = self.tool.scan_repository(root, "history")
            self.assertTrue(any(item.rule == "github-token" for item in findings))
            self.assertTrue(all(secret_message not in repr(item) for item in findings))

    def test_head_limited_history_ignores_secret_only_local_stash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(git(root, "init", "-q").returncode, 0)
            self.assertEqual(git(root, "config", "user.email", "test@example.com").returncode, 0)
            self.assertEqual(git(root, "config", "user.name", "Public Sync Test").returncode, 0)
            path = root / "settings.txt"
            path.write_text("public=true\n", encoding="utf-8")
            self.assertEqual(git(root, "add", "settings.txt").returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "safe").returncode, 0)

            path.write_bytes(b"gh" + b"p_" + (b"S" * 36))
            self.assertEqual(
                git(root, "stash", "push", "-qm", "private local stash").returncode,
                0,
            )

            head_findings, _ = self.tool.scan_repository(
                root, "history", revisions=["HEAD"]
            )
            all_findings, _ = self.tool.scan_repository(root, "history")
            self.assertEqual(head_findings, [])
            self.assertTrue(any(item.rule == "github-token" for item in all_findings))

    def test_secret_filename_is_hashed_and_never_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(git(root, "init", "-q").returncode, 0)
            filename = "gh" + "p_" + ("R" * 36) + ".txt"
            (root / filename).write_text("public content\n", encoding="utf-8")
            self.assertEqual(git(root, "add", "--", filename).returncode, 0)
            findings, _ = self.tool.scan_repository(root, "index")
            self.assertTrue(any(item.rule == "github-token" for item in findings))
            self.assertTrue(any(item.path.startswith("<redacted-path") for item in findings))
            self.assertTrue(all(filename not in repr(item) for item in findings))

    def test_cli_never_prints_secret_input_or_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(git(root, "init", "-q").returncode, 0)
            input_secret = "gh" + "p_" + ("I" * 36)
            label_secret = "gh" + "p_" + ("L" * 36)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "check_public_sync.py"),
                    "--root",
                    str(root),
                    "--scope",
                    "candidates",
                    "--stdin-label",
                    label_secret,
                ],
                input=input_secret.encode("ascii"),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            output = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 1)
            self.assertNotIn(input_secret.encode("ascii"), output)
            self.assertNotIn(label_secret.encode("ascii"), output)
            self.assertIn(b"<redacted-path", output)

    def test_cli_argument_errors_do_not_reflect_values(self) -> None:
        secret_argument = "--gh" + "p_" + ("A" * 36)
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "check_public_sync.py"),
                secret_argument,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(secret_argument.encode("ascii"), output)
        self.assertIn(b"invalid command-line arguments", output)

    def test_annotated_tag_message_is_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(git(root, "init", "-q").returncode, 0)
            self.assertEqual(git(root, "config", "user.email", "test@example.com").returncode, 0)
            self.assertEqual(git(root, "config", "user.name", "Public Sync Test").returncode, 0)
            (root / "README.md").write_text("safe\n", encoding="utf-8")
            self.assertEqual(git(root, "add", "README.md").returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "safe").returncode, 0)
            tag_value = "gh" + "p_" + ("T" * 36)
            self.assertEqual(git(root, "tag", "-a", "v-test", "-m", tag_value).returncode, 0)
            findings, _ = self.tool.scan_repository(root, "history")
            self.assertTrue(any(item.rule == "github-token" for item in findings))
            self.assertTrue(all(tag_value not in repr(item) for item in findings))

    def test_oversize_files_fail_closed_before_content_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(git(root, "init", "-q").returncode, 0)
            (root / "large.bin").write_bytes(b"abc")
            original = self.tool.MAX_SCANNED_BLOB_BYTES
            self.tool.MAX_SCANNED_BLOB_BYTES = 2
            try:
                findings, _ = self.tool.scan_repository(root, "candidates")
            finally:
                self.tool.MAX_SCANNED_BLOB_BYTES = original
            self.assertTrue(any(item.rule == "oversize-unscanned-blob" for item in findings))

    def test_noncanonical_touchdesigner_projects_and_archives_are_blocked(self) -> None:
        blocked = {
            "projects/ShowCopy.toe": "noncanonical-touchdesigner-project",
            "projects/flexshow.toe": "noncanonical-touchdesigner-project",
            "vendor/plugin.zip": "restricted-extension",
            "vendor/plugin.7z": "restricted-extension",
            "vendor/plugin.rar": "restricted-extension",
            "vendor/plugin.tar.gz": "restricted-extension",
        }
        for path, expected_rule in blocked.items():
            with self.subTest(path=path):
                findings = self.tool.path_findings(path)
                self.assertTrue(any(item.rule == expected_rule for item in findings))
        self.assertEqual(self.tool.path_findings("projects/FlexShow.toe"), [])

    def test_gitignore_matches_restricted_but_not_public_paths(self) -> None:
        blocked = (
            "local-components/StreamDiffusionTD.tox",
            ".env.production",
            "config/local-flexshow.json",
            "paid/sdk/plugin.dll",
            "licensed/model/file.bin",
            "weights/model.pt",
            "projects/ShowCopy.toe",
            "vendor/plugin.zip",
            "vendor/plugin.7z",
            "vendor/plugin.tar.gz",
            "captures/audience/frame-0001.ppm",
            "commissioning/site-a/manifest.json",
            "recordings/rehearsal/depth.pgm",
        )
        allowed = (
            "README.md",
            "src/flexgpu/config.py",
            "assets/original-public-texture.png",
            ".env.example",
            "projects/FlexShow.toe",
        )
        for path in blocked:
            with self.subTest(blocked=path):
                self.assertEqual(git(ROOT, "check-ignore", "-q", "--", path).returncode, 0)
        for path in allowed:
            with self.subTest(allowed=path):
                self.assertEqual(git(ROOT, "check-ignore", "-q", "--", path).returncode, 1)

    def test_current_candidates_and_index_are_clean(self) -> None:
        findings, scanned = self.tool.scan_repository(ROOT, "both")
        self.assertGreater(scanned, 0)
        self.assertEqual(findings, [])

    def test_sync_script_contains_static_publication_guards(self) -> None:
        source = (ROOT / "scripts" / "Sync-PublicRepo.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "$Remote -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            source,
        )
        self.assertIn(
            'rev-list "$remoteReference..HEAD" -- projects/FlexShow.toe',
            source,
        )
        self.assertIn(
            "-InputText $branch -InputLabel 'branch-name' | Out-Host",
            source,
        )
        self.assertIn(
            "-InputText $Message -InputLabel 'commit-message'",
            source,
        )
        self.assertIn("& $checker -Scope History -Revision HEAD", source)
        self.assertIn("'push', '--no-follow-tags', $Remote", source)
        self.assertIn('"HEAD:refs/heads/$branch"', source)


if __name__ == "__main__":
    unittest.main()
