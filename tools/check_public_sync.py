#!/usr/bin/env python3
"""Fail closed when a public Git sync contains private or secret material.

The checker intentionally reports only a repository-relative path, rule name,
and line number.  It never prints the matched value.  It has no third-party
dependencies and can inspect the working candidates, the exact Git index, and
every reachable historical blob.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

BLOCKED_DIRECTORIES = frozenset(
    {
        ".aws",
        ".azure",
        ".gnupg",
        ".secrets",
        "calibration",
        "checkpoints",
        "credentials",
        "licensed",
        "local-components",
        "logs",
        "models",
        "paid",
        "private",
        "runtime",
        "secrets",
        "vendor-private",
        "weights",
    }
)

BLOCKED_SUFFIXES = frozenset(
    {
        ".7z",
        ".bz2",
        ".ckpt",
        ".engine",
        ".gguf",
        ".gz",
        ".jks",
        ".key",
        ".kdbx",
        ".keystore",
        ".onnx",
        ".p12",
        ".pem",
        ".pfx",
        ".plan",
        ".pt",
        ".pth",
        ".rar",
        ".safetensors",
        ".snk",
        ".tar",
        ".tgz",
        ".tox",
        ".xz",
        ".zip",
    }
)

BLOCKED_FILENAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)

SAFE_ENV_FILENAMES = frozenset({".env.example"})
MAX_SCANNED_BLOB_BYTES = 100 * 1024 * 1024

BLOCKED_PATH_GLOBS = (
    "config/local-*",
    "config/*-local.*",
    "config/show-config.json",
    "projects/*-local.toe",
    "service-account*.json",
    "service_account*.json",
    "credentials.*.json",
    "secrets.*.json",
    "**/service-account*.json",
    "**/service_account*.json",
    "**/credentials.*.json",
    "**/secrets.*.json",
)


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    message: str
    scope: str
    line: int | None = None


@dataclass(frozen=True)
class ScanEntry:
    path: str
    data: bytes | None
    scope: str


class PublicSyncError(RuntimeError):
    """Raised when repository inspection cannot be completed safely."""


class SafeArgumentParser(argparse.ArgumentParser):
    """Reject invalid arguments without reflecting a possibly secret value."""

    def error(self, message: str) -> None:
        del message
        self.print_usage(sys.stderr)
        self.exit(2, "check_public_sync.py: error: invalid command-line arguments\n")


def normalize_path(path: str) -> str:
    value = path.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def path_findings(path: str, scope: str = "candidate") -> list[Finding]:
    """Return fail-closed publication findings for one repository path."""

    normalized = normalize_path(path)
    pure = PurePosixPath(normalized)
    lowered = normalized.casefold()
    parts = tuple(part.casefold() for part in pure.parts)
    name = pure.name.casefold()
    findings: list[Finding] = []

    if pure.is_absolute() or ".." in pure.parts or not normalized:
        findings.append(
            Finding(
                normalized or "<empty>",
                "unsafe-path",
                "path is not a safe repository-relative path",
                scope,
            )
        )
        return findings

    restricted = sorted(set(parts).intersection(BLOCKED_DIRECTORIES))
    if restricted:
        findings.append(
            Finding(
                normalized,
                "restricted-directory",
                "path is inside a private, paid, local, or generated directory",
                scope,
            )
        )

    if name.startswith(".env") and name not in SAFE_ENV_FILENAMES:
        findings.append(
            Finding(
                normalized,
                "environment-file",
                "environment files may contain machine-local credentials",
                scope,
            )
        )

    if name in BLOCKED_FILENAMES:
        findings.append(
            Finding(
                normalized,
                "credential-file",
                "credential or private-key filename is not publishable",
                scope,
            )
        )

    credential_config_suffixes = {".ini", ".json", ".toml", ".yaml", ".yml"}
    credential_prefixes = ("credentials.", "secrets.", "service-account", "service_account")
    if name.startswith(credential_prefixes) and pure.suffix.casefold() in credential_config_suffixes:
        findings.append(
            Finding(
                normalized,
                "credential-file",
                "credential configuration filename is not publishable",
                scope,
            )
        )

    suffix = pure.suffix.casefold()
    if suffix == ".toe" and normalized != "projects/FlexShow.toe":
        findings.append(
            Finding(
                normalized,
                "noncanonical-touchdesigner-project",
                "only canonical projects/FlexShow.toe is publishable by default",
                scope,
            )
        )
    if suffix in BLOCKED_SUFFIXES:
        findings.append(
            Finding(
                normalized,
                "restricted-extension",
                "private component, model weight, key store, or opaque archive is blocked",
                scope,
            )
        )

    for pattern in BLOCKED_PATH_GLOBS:
        if fnmatch.fnmatchcase(lowered, pattern.casefold()):
            findings.append(
                Finding(
                    normalized,
                    "machine-local-path",
                    "machine-local configuration or component path is blocked",
                    scope,
                )
            )
            break

    return findings


_CONTENT_PATTERNS: tuple[tuple[str, str, re.Pattern[bytes]], ...] = (
    (
        "private-key",
        "private-key material detected",
        re.compile(
            rb"-----BEGIN[ \t]+"
            rb"(?:(?:RSA|EC|OPENSSH|DSA|ENCRYPTED|PGP)[ \t]+)?"
            rb"PRIVATE[ \t]+KEY(?:[ \t]+BLOCK)?-----"
        ),
    ),
    (
        "github-token",
        "GitHub credential signature detected",
        re.compile(rb"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})(?![A-Za-z0-9])"),
    ),
    (
        "aws-access-key",
        "AWS access-key signature detected",
        re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    (
        "openai-token",
        "OpenAI credential signature detected",
        re.compile(rb"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    (
        "google-api-key",
        "Google API-key signature detected",
        re.compile(rb"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{30,}(?![A-Za-z0-9_-])"),
    ),
    (
        "slack-token",
        "Slack credential signature detected",
        re.compile(rb"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9-])"),
    ),
    (
        "stripe-live-key",
        "Stripe live credential signature detected",
        re.compile(rb"(?<![A-Za-z0-9_])(?:sk|rk)_live_[A-Za-z0-9]{16,}(?![A-Za-z0-9])"),
    ),
    (
        "huggingface-token",
        "Hugging Face credential signature detected",
        re.compile(rb"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{20,}(?![A-Za-z0-9])"),
    ),
    (
        "gitlab-token",
        "GitLab credential signature detected",
        re.compile(rb"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    (
        "npm-token",
        "npm credential signature detected",
        re.compile(rb"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{20,}(?![A-Za-z0-9])"),
    ),
    (
        "pypi-token",
        "PyPI credential signature detected",
        re.compile(rb"(?<![A-Za-z0-9_-])pypi-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    (
        "jwt-token",
        "JWT-like bearer credential detected",
        re.compile(rb"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])"),
    ),
)

_GENERIC_ASSIGNMENT = re.compile(
    rb"(?im)(?:^|[,{;])[ \t]*(?:export[ \t]+)?[\"']?"
    rb"(?:api[_-]?(?:key|secret)|access[_-]?key|access[_-]?token|"
    rb"auth[_-]?token|aws[_-]?secret[_-]?access[_-]?key|bearer[_-]?token|"
    rb"client[_-]?secret|connection[_-]?string|consumer[_-]?secret|"
    rb"database[_-]?password|db[_-]?password|encryption[_-]?key|password|"
    rb"passwd|private[_-]?(?:key|token)|refresh[_-]?token|license[_-]?(?:key|token)|secret|"
    rb"secret[_-]?(?:access[_-]?key|key)|session[_-]?secret|"
    rb"signing[_-]?key|smtp[_-]?password|token|webhook[_-]?secret"
    rb"|(?:(?:aws|azure|discord|git(?:hub|lab)|google|hf|huggingface|npm|"
    rb"openai|pypi|slack|stripe|twilio)[_-])"
    rb"(?:access[_-]?token|api[_-]?key|key|password|secret|token)"
    rb")[\"']?[ \t]*[:=][ \t]*"
    rb"(?:\"([^\"\r\n]{8,})\"|'([^'\r\n]{8,})'|([^\"'#,{\r\n}]{8,}))"
)

_CREDENTIALED_URL = re.compile(
    rb"\b[A-Za-z][A-Za-z0-9+.-]{1,31}://"
    rb"[^\s/:@]{1,128}:([^\s/@]{1,})@[^\s/]+"
)


def _line_number(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _is_placeholder(value: bytes) -> bool:
    text = value.decode("utf-8", "ignore").strip().rstrip(",;}").strip()
    text = text.strip("\"'").strip().casefold()
    if not text:
        return True
    if text in {"changeme", "not-a-secret", "redacted"}:
        return True
    explicit_prefixes = (
        "<",
        "${",
        "$env:",
        "[environment]::getenvironmentvariable",
        "get_secret(",
        "getenv(",
        "keyring.",
        "os.environ",
        "os.getenv",
        "process.env",
        "replace_",
        "replace-with-",
        "secretmanager.",
        "system.getenv",
        "vault.",
        "your_",
        "your-",
    )
    if text.startswith(explicit_prefixes):
        return True
    return re.fullmatch(r"%[a-z][a-z0-9_]*%", text) is not None


def content_findings(
    path: str, data: bytes, scope: str = "candidate"
) -> list[Finding]:
    """Scan bytes without returning or logging any matched credential value."""

    findings: list[Finding] = []
    for rule, message, pattern in _CONTENT_PATTERNS:
        match = pattern.search(data)
        if match is not None:
            findings.append(
                Finding(
                    normalize_path(path),
                    rule,
                    message,
                    scope,
                    _line_number(data, match.start()),
                )
            )

    for match in _GENERIC_ASSIGNMENT.finditer(data):
        value = next(group for group in match.groups() if group is not None)
        if not _is_placeholder(value):
            findings.append(
                Finding(
                    normalize_path(path),
                    "assigned-secret",
                    "non-placeholder value assigned to a credential-like key",
                    scope,
                    _line_number(data, match.start()),
                )
            )
            break

    for match in _CREDENTIALED_URL.finditer(data):
        if not _is_placeholder(match.group(1)):
            findings.append(
                Finding(
                    normalize_path(path),
                    "credentialed-url",
                    "URL containing embedded credentials detected",
                    scope,
                    _line_number(data, match.start()),
                )
            )
            break
    return findings


def safe_display_path(path: str, sensitive: bool = False) -> str:
    """Return a control-safe path label, or a hash when the path is secret."""

    raw = normalize_path(path).encode("utf-8", "surrogateescape")
    if sensitive:
        digest = hashlib.sha256(raw).hexdigest()[:12]
        return "<redacted-path sha256:" + digest + ">"
    return raw.decode("utf-8", "surrogateescape").encode(
        "unicode_escape", "backslashreplace"
    ).decode("ascii")


def _git(root: Path, arguments: Sequence[str]) -> bytes:
    command = ["git", "-C", os.fspath(root), *arguments]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise PublicSyncError("could not execute Git") from exc
    if completed.returncode != 0:
        # Git errors can echo a secret-bearing filename or remote. Keep the
        # public error generic and never relay native stderr.
        raise PublicSyncError(
            "Git inspection failed with exit code " + str(completed.returncode)
        )
    return completed.stdout


def _nul_paths(payload: bytes) -> list[str]:
    return [
        item.decode("utf-8", "surrogateescape")
        for item in payload.split(b"\0")
        if item
    ]


def candidate_entries(root: Path) -> Iterable[ScanEntry]:
    payload = _git(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    for path in _nul_paths(payload):
        local_path = root.joinpath(*PurePosixPath(normalize_path(path)).parts)
        if local_path.is_symlink():
            data = os.readlink(local_path).encode("utf-8", "surrogateescape")
        elif local_path.is_file():
            data = (
                None
                if local_path.stat().st_size > MAX_SCANNED_BLOB_BYTES
                else local_path.read_bytes()
            )
        else:
            continue
        yield ScanEntry(path, data, "candidate")


def index_entries(root: Path) -> Iterable[ScanEntry]:
    payload = _git(root, ["ls-files", "-z", "--cached"])
    for path in _nul_paths(payload):
        object_spec = ":" + path
        size = int(_git(root, ["cat-file", "-s", object_spec]).strip())
        data = (
            None
            if size > MAX_SCANNED_BLOB_BYTES
            else _git(root, ["cat-file", "blob", object_spec])
        )
        yield ScanEntry(path, data, "index")


def _resolve_revisions(root: Path, revisions: Sequence[str]) -> list[str]:
    """Resolve conservative ref names to immutable commit IDs."""

    resolved: list[str] = []
    for revision in revisions:
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", revision) is None
            or ".." in revision
            or "//" in revision
            or revision.endswith(("/", ".", ".lock"))
        ):
            raise PublicSyncError("history revision is not a conservative Git ref name")
        object_id = _git(
            root, ["rev-parse", "--verify", revision + "^{commit}"]
        ).strip()
        if re.fullmatch(rb"[0-9A-Fa-f]{40,64}", object_id) is None:
            raise PublicSyncError("Git returned an invalid commit identifier")
        decoded = object_id.decode("ascii", "strict").lower()
        if decoded not in resolved:
            resolved.append(decoded)
    if not resolved:
        raise PublicSyncError("at least one history revision is required")
    return resolved


def history_entries(
    root: Path, revisions: Sequence[str] | None = None
) -> Iterable[ScanEntry]:
    if revisions is None:
        references = _git(
            root,
            [
                "for-each-ref",
                "--format=%(refname)%09%(objecttype)%09%(objectname)",
            ],
        )
        for index, record in enumerate(references.splitlines()):
            fields = record.split(b"\t", 2)
            if len(fields) != 3:
                continue
            reference, object_type, object_id = fields
            yield ScanEntry(
                ".git-history/ref-" + str(index) + ".txt",
                reference,
                "refs",
            )
            if object_type == b"tag":
                tag_id = object_id.decode("ascii", "strict")
                size = int(_git(root, ["cat-file", "-s", tag_id]).strip())
                data = (
                    None
                    if size > MAX_SCANNED_BLOB_BYTES
                    else _git(root, ["cat-file", "tag", tag_id])
                )
                yield ScanEntry(
                    ".git-history/tag-" + str(index) + ".txt",
                    data,
                    "tag:" + tag_id[:12],
                )
        commit_arguments = ["rev-list", "--all"]
    else:
        commit_arguments = ["rev-list", *_resolve_revisions(root, revisions)]

    commits = _git(root, commit_arguments).decode("ascii", "strict").splitlines()
    seen: set[tuple[str, str]] = set()
    scanned_content: set[str] = set()
    for commit in commits:
        # Commit messages are public Git objects too. Scan the object without
        # ever displaying the message or any matched value.
        yield ScanEntry(
            ".git-history/commit-" + commit[:12] + ".txt",
            _git(root, ["cat-file", "commit", commit]),
            "history:" + commit[:12],
        )
        tree = _git(root, ["ls-tree", "-r", "-z", "--full-tree", commit])
        for record in tree.split(b"\0"):
            if not record or b"\t" not in record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            fields = metadata.split()
            if len(fields) != 3 or fields[1] != b"blob":
                continue
            object_id = fields[2].decode("ascii", "strict")
            path = raw_path.decode("utf-8", "surrogateescape")
            identity = (object_id, path)
            if identity in seen:
                continue
            seen.add(identity)
            if object_id in scanned_content:
                data: bytes | None = b""
            else:
                size = int(_git(root, ["cat-file", "-s", object_id]).strip())
                data = (
                    None
                    if size > MAX_SCANNED_BLOB_BYTES
                    else _git(root, ["cat-file", "blob", object_id])
                )
                scanned_content.add(object_id)
            yield ScanEntry(path, data, "history:" + commit[:12])


def scan_repository(
    root: Path,
    scope: str = "all",
    revisions: Sequence[str] | None = None,
) -> tuple[list[Finding], int]:
    """Scan selected Git surfaces and return de-duplicated safe findings."""

    root = root.resolve()
    valid_scopes = {"candidates", "index", "history", "both", "all"}
    if scope not in valid_scopes:
        raise PublicSyncError("unknown public-sync scan scope")
    if revisions is not None and scope not in {"history", "all"}:
        raise PublicSyncError("history revisions require history or all scope")
    _git(root, ["rev-parse", "--is-inside-work-tree"])
    selected: list[Iterable[ScanEntry]] = []
    if scope in {"candidates", "both", "all"}:
        selected.append(candidate_entries(root))
    if scope in {"index", "both", "all"}:
        selected.append(index_entries(root))
    if scope in {"history", "all"}:
        selected.append(history_entries(root, revisions))

    findings: list[Finding] = []
    scanned = 0
    seen_findings: set[tuple[str, str, int | None]] = set()
    for entries in selected:
        for entry in entries:
            scanned += 1
            raw_path_bytes = normalize_path(entry.path).encode(
                "utf-8", "surrogateescape"
            )
            path_secret_matches = content_findings(
                "<repository-path>", raw_path_bytes, entry.scope + ":path"
            )
            display_path = safe_display_path(
                entry.path, sensitive=bool(path_secret_matches)
            )
            entry_findings: list[Finding] = []
            for match in path_secret_matches:
                entry_findings.append(
                    Finding(
                        display_path,
                        match.rule,
                        "credential signature detected in repository path",
                        entry.scope + ":path",
                    )
                )
            for match in path_findings(entry.path, entry.scope):
                entry_findings.append(
                    Finding(
                        display_path,
                        match.rule,
                        match.message,
                        match.scope,
                        match.line,
                    )
                )
            if entry.data is None:
                entry_findings.append(
                    Finding(
                        display_path,
                        "oversize-unscanned-blob",
                        "file exceeds the 100 MiB fail-closed scan limit",
                        entry.scope,
                    )
                )
            else:
                entry_findings.extend(
                    content_findings(display_path, entry.data, entry.scope)
                )

            for finding in entry_findings:
                identity = (finding.path, finding.rule, finding.line)
                if identity not in seen_findings:
                    findings.append(finding)
                    seen_findings.add(identity)
    findings.sort(key=lambda item: (item.path.casefold(), item.line or 0, item.rule))
    return findings, scanned


def run_self_test() -> list[str]:
    """Run small positive/negative checks without embedding usable secrets."""

    failures: list[str] = []
    blocked_paths = (
        "local-components/StreamDiffusionTD.tox",
        "private/component.tox",
        "paid/sdk/plugin.dll",
        "credentials/service-account.json",
        "config/local-flexshow.json",
        ".env.production",
        "keys/id_rsa",
        "models/geometry.safetensors",
        "vendor/plugin.zip",
        "projects/private-copy.toe",
    )
    safe_paths = (
        "README.md",
        "src/flexgpu/config.py",
        "assets/original-public-texture.png",
        ".env.example",
        "projects/FlexShow.toe",
    )
    for path in blocked_paths:
        if not path_findings(path, "self-test"):
            failures.append("blocked path was accepted: " + path)
    for path in safe_paths:
        if path_findings(path, "self-test"):
            failures.append("safe path was rejected: " + path)

    generated_secrets = (
        b"gh" + b"p_" + (b"A" * 36),
        b"AK" + b"IA" + (b"A1" * 8),
        b"sk-" + (b"z" * 32),
        b"-----BEGIN " + b"PRIVATE KEY-----\n" + (b"A" * 40),
        b"-----BEGIN " + b"ENCRYPTED PRIVATE KEY-----\n" + (b"B" * 40),
        b"-----BEGIN " + b"DSA PRIVATE KEY-----\n" + (b"C" * 40),
        b'{"api_' + b'key":"' + (b"K8z_" * 8) + b'"}',
        b'{"safe":true,"hf_' + b'token":"' + (b"H9x_" * 8) + b'"}',
        b"api_" + b"key = " + (b"Ab9_" * 7),
        b'password="abc#' + (b"V7q_" * 6) + b'"',
        b"hf_" + b"token=" + b"hf_" + (b"A" * 32),
        b"license_" + b"key=" + (b"L8z_" * 8),
        b"postgresql://operator:" + (b"P4s_" * 6) + b"@db.internal/show",
    )
    for index, payload in enumerate(generated_secrets):
        if not content_findings("self-test.txt", payload, "self-test"):
            failures.append("secret fixture was accepted: case " + str(index))

    safe_payload = (
        b"api_key=${API_KEY}\n"
        b"client_secret=<provided-at-runtime>\n"
        b'token = os.environ["TOKEN"]\n'
        b"token=REPLACE_WITH_LOCAL_VALUE\n"
    )
    if content_findings(".env.example", safe_payload, "self-test"):
        failures.append("placeholder fixture was rejected")

    real_values = (
        b"password=exampleRealPassword123",
        b"password=test_actual_secret_123",
        b"password=AAAAAAAA",
        b"password=abababababab",
    )
    for index, payload in enumerate(real_values):
        if not content_findings("self-test.txt", payload, "self-test"):
            failures.append("real credential-like value was accepted: case " + str(index))
    return failures


def _parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        description="Check the FlexShow public Git sync boundary without printing secrets."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="repository root (default: project containing this tool)",
    )
    parser.add_argument(
        "--scope",
        choices=("candidates", "index", "history", "both", "all"),
        default="all",
        help="Git surfaces to scan (default: all)",
    )
    parser.add_argument(
        "--revision",
        action="append",
        default=[],
        help=(
            "limit history scanning to the commit closure of this conservative "
            "ref name; repeat for more than one ref"
        ),
    )
    parser.add_argument("--self-test", action="store_true", help="run built-in policy tests first")
    parser.add_argument(
        "--stdin-label",
        default="",
        help="also scan stdin as this safe label (the value is never printed)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    self_test_failures = run_self_test() if args.self_test else []
    try:
        findings, scanned = scan_repository(
            args.root, args.scope, args.revision or None
        )
        if args.stdin_label:
            raw_label = args.stdin_label.encode("utf-8", "surrogateescape")
            label_matches = content_findings("<stdin-label>", raw_label, "stdin-label")
            safe_label = safe_display_path(
                args.stdin_label, sensitive=bool(label_matches)
            )
            for match in label_matches:
                findings.append(
                    Finding(
                        safe_label,
                        match.rule,
                        "credential signature detected in input label",
                        "stdin-label",
                    )
                )
            input_findings = content_findings(
                safe_label, sys.stdin.buffer.read(), "stdin"
            )
            findings.extend(input_findings)
            findings.sort(key=lambda item: (item.path.casefold(), item.line or 0, item.rule))
            scanned += 1
    except (OSError, UnicodeError, PublicSyncError) as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        else:
            print("[public-sync] ERROR: " + str(exc), file=sys.stderr)
        return 2

    status = "blocked" if findings or self_test_failures else "pass"
    payload = {
        "status": status,
        "scope": args.scope,
        "scanned_files": scanned,
        "self_test": "fail" if self_test_failures else ("pass" if args.self_test else "not-run"),
        "findings": [asdict(item) for item in findings],
        "self_test_failures": self_test_failures,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif status == "pass":
        suffix = " with self-test" if args.self_test else ""
        print(f"[public-sync] PASS: scanned {scanned} file version(s){suffix}; no restricted material detected")
    else:
        print("[public-sync] BLOCKED: public sync policy violations detected", file=sys.stderr)
        for failure in self_test_failures:
            print("  - [self-test] " + failure, file=sys.stderr)
        for finding in findings:
            location = finding.path + ((":" + str(finding.line)) if finding.line else "")
            print(
                f"  - {location} [{finding.rule}] {finding.message} ({finding.scope})",
                file=sys.stderr,
            )
        print("Matched credential values are intentionally not displayed.", file=sys.stderr)
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
