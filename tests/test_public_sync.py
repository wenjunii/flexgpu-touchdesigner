from __future__ import annotations

import importlib.util
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
