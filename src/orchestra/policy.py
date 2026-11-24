"""Policy layer: configuration, risk classification and human approval tokens.

Three things live here on purpose, because they all answer "is this change allowed
to proceed":

* ``Config``     - the single ``config/orchestra.toml`` of a managed repo.
* ``classify``   - mechanical risk grading from a diff (T0/T1/T2).
* approval token - an HMAC-signed, single-use, expiring record of a human "yes"
                   bound to an exact subject digest. Immutable actions verify the
                   token instead of "trusting" that a human was in the loop.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .clock import Clock
from .paths import matches_any

CONFIG_RELATIVE = Path("config") / "orchestra.toml"
KEY_ENV = "ORCHESTRA_APPROVAL_KEY_FILE"
TOKEN_PREFIX = "oap1"

DEFAULTS: dict = {
    "lease": {"default_ttl": 1800, "max_ttl": 28800},
    "scheduler": {"wip_limit": 16, "research_share": 0.1, "per_holder_limit": 2, "cooldown_minutes": 30},
    "budget": {"default_claim_cost": 1.0},
    "gates": {"impl": [], "merge": []},
    "risk": {"rule": []},
    "domains": {},
    "debt": {},
    "paths": {"protected": ["ledger/**", ".orchestra/**"]},
}

RISK_ORDER = {"T0": 0, "T1": 1, "T2": 2}


class PolicyError(RuntimeError):
    """Configuration or approval problem."""


class AdmissionError(PolicyError):
    """Refused before work starts: WIP limit or budget envelope (exit code 5)."""

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
class Config:
    def __init__(self, data: dict | None = None) -> None:
        self.data = _merge(DEFAULTS, data or {})

    def get(self, dotted: str, default=None):
        node: object = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def lease_ttl(self, mode: str = "write") -> int:
        base = int(self.get("lease.default_ttl", 1800))
        return {"read": base, "write": base, "module": base, "repo": min(base, 600)}.get(mode, base)

    def max_ttl(self) -> int:
        return int(self.get("lease.max_ttl", 28800))

    def wip_limit(self) -> int:
        return int(self.get("scheduler.wip_limit", 16))

    def claim_cost(self) -> float:
        """Cost charged per claim, in whatever unit the envelope is written in."""

        return float(self.get("budget.default_claim_cost", 1.0))

    def per_holder_limit(self) -> int:
        """How many tasks one holder may hold at once (0 = unlimited)."""

        return int(self.get("scheduler.per_holder_limit", 2))

    def cooldown_minutes(self) -> float:
        """How long a holder is barred from a task it gave back as out_of_time/other."""

        return float(self.get("scheduler.cooldown_minutes", 30))

    def gate_commands(self, stage: str) -> list[str]:
        value = self.get(f"gates.{stage}", [])
        return [str(item) for item in value] if isinstance(value, list) else []

    def risk_rules(self) -> list[dict]:
        value = self.get("risk.rule", [])
        return [rule for rule in value if isinstance(rule, dict)] if isinstance(value, list) else []

    def domains(self) -> dict:
        value = self.get("domains", {})
        return value if isinstance(value, dict) else {}

    def debt_modules(self) -> dict:
        value = self.get("debt", {})
        return value if isinstance(value, dict) else {}

    def protected_patterns(self) -> list[str]:
        value = self.get("paths.protected", [])
        return [str(item) for item in value] if isinstance(value, list) else []


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def template_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def load_config(repo: Path) -> Config:
    path = Path(repo) / CONFIG_RELATIVE
    if not path.exists():
        return Config({})
    try:
        return Config(tomllib.loads(path.read_text(encoding="utf-8")))
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{path} is not valid TOML: {exc}") from exc


def write_default_config(repo: Path, *, force: bool = False) -> Path:
    target = Path(repo) / CONFIG_RELATIVE
    if target.exists() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_dir() / "orchestra.toml", target)
    return target


# --------------------------------------------------------------------------
# risk classification
# --------------------------------------------------------------------------
SKIP_MARKERS = (
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "@unittest.skip",
    "it.skip(",
    "test.skip(",
    "#[ignore]",
    "--no-verify",
    "continue-on-error",
    "skiptests",
)
IRREVERSIBLE_MARKERS = (
    "terraform apply",
    "terraform destroy",
    "kubectl delete",
    "drop table",
    "drop database",
    "truncate table",
    "rm -rf",
    "aws s3 rm",
    "gcloud compute instances delete",
)


@dataclass
class RiskResult:
    cls: str = "T0"
    signals: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    digest: str = ""
    blast_radius: int = 0

    def as_dict(self) -> dict:
        return {
            "class": self.cls,
            "signals": self.signals,
            "reasons": self.reasons,
            "digest": self.digest,
            "blast_radius": self.blast_radius,
            "changed_files": len(self.changed),
        }


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def subject_digest(changed: Iterable[str], diff_text: str) -> str:
    """Bind approvals to the exact change: path list + diff body."""

    body = "\n".join(sorted(set(changed))) + "\n---diff---\n" + diff_text
    return digest(body)


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered.startswith("tests/")
        or "/tests/" in lowered
        or "/test_" in lowered
        or lowered.startswith("test_")
        or "_test." in lowered
        or ".test." in lowered
        or "spec/" in lowered
    )


def _is_dependency_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in {
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "package.json",
        "cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "cargo.lock",
    }


def _is_migration_path(path: str) -> bool:
    lowered = path.lower()
    return "/migrations/" in f"/{lowered}" or lowered.endswith(".sql") or "schema.sql" in lowered


def _is_ci_path(path: str) -> bool:
    lowered = path.lower()
    return lowered.startswith(".github/workflows/") or lowered.startswith("ci/") or lowered.startswith(".gitlab-ci")


def detect_signals(changes: Iterable[tuple[str, str]], diff_text: str) -> list[str]:
    added = [
        line[1:].strip().lower()
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    signals: set[str] = set()
    for status, path in changes:
        if status.upper().startswith("D") and _is_test_path(path):
            signals.add("deletes_tests")
        if _is_migration_path(path):
            signals.add("migration")
        if _is_dependency_path(path) and added:
            signals.add("adds_dependency")
        if _is_ci_path(path):
            signals.add("touches_ci")
    for line in added:
        if any(marker in line for marker in SKIP_MARKERS):
            signals.add("adds_skip")
        if any(marker in line for marker in IRREVERSIBLE_MARKERS):
            signals.add("irreversible_command")
    return sorted(signals)


def classify(
    changes: Iterable[tuple[str, str]],
    diff_text: str,
    rules: Iterable[dict],
    *,
    protected: Iterable[str] = (),
) -> RiskResult:
    changes = list(changes)
    paths = [path for _status, path in changes]
    result = RiskResult(changed=paths, digest=subject_digest(paths, diff_text), blast_radius=len(paths))
    result.signals = detect_signals(changes, diff_text)

    def raise_to(cls: str, reason: str) -> None:
        if RISK_ORDER[cls] > RISK_ORDER[result.cls]:
            result.cls = cls
        result.reasons.append(f"{cls}: {reason}")

    for pattern in protected:
        if matches_any_paths(paths, [pattern]):
            raise_to("T1", f"touches protected path {pattern}")

    for rule in rules:
        cls = str(rule.get("class", "T1")).upper()
        if cls not in RISK_ORDER:
            continue
        name = str(rule.get("name", "unnamed"))
        path_patterns = [str(p) for p in rule.get("paths", []) or []]
        rule_signals = [str(s) for s in rule.get("signals", []) or []]
        if path_patterns and matches_any_paths(paths, path_patterns):
            raise_to(cls, f"rule {name} matched paths")
        if rule_signals and set(rule_signals) & set(result.signals):
            raise_to(cls, f"rule {name} matched signals {sorted(set(rule_signals) & set(result.signals))}")

    result.reasons = sorted(set(result.reasons))
    return result


def matches_any_paths(paths: Iterable[str], patterns: Iterable[str]) -> bool:
    patterns = list(patterns)
    return any(matches_any(path, patterns) for path in paths)


# --------------------------------------------------------------------------
# approval tokens
# --------------------------------------------------------------------------
class ApprovalError(PolicyError):
    """Invalid, expired, replayed or mismatched approval token."""


APPROVAL_KINDS = ("repo_lease", "merge", "irreversible")


def key_path(repo: Path) -> Path:
    override = os.environ.get(KEY_ENV, "").strip()
    if override:
        return Path(override)
    return Path(repo) / ".orchestra" / "approval.key"


def generate_key(repo: Path, *, force: bool = False) -> Path:
    path = key_path(repo)
    if path.exists() and not force:
        raise ApprovalError(f"key already exists: {path} (use --force to replace)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort on Windows
        pass
    return path


def read_key(repo: Path) -> bytes:
    path = key_path(repo)
    if not path.exists():
        raise ApprovalError(f"no approval key at {path}; run `orchestra approval keygen`")
    return path.read_text(encoding="utf-8").strip().encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue(
    key: bytes,
    *,
    kind: str,
    by: str,
    clock: Clock,
    ttl: int,
    subject: str = "",
    prediction: str = "",
    rollback: str = "",
    approval_id: str | None = None,
) -> dict:
    if kind not in APPROVAL_KINDS:
        raise ApprovalError(f"unknown approval kind: {kind}")
    if not by.strip():
        raise ApprovalError("--by is required: an approval needs a human identity")
    if not prediction.strip():
        raise ApprovalError("--prediction is required: a signature without a falsifiable prediction is noise")
    if not rollback.strip():
        raise ApprovalError("--rollback is required: a signature without a rollback plan is noise")
    if ttl <= 0:
        raise ApprovalError("--ttl must be positive")
    now = clock.now()
    payload = {
        "v": 1,
        "approval_id": approval_id or "A-" + secrets.token_hex(4),
        "kind": kind,
        "by": by.strip(),
        "issued_at": int(now),
        "expires_at": int(now + ttl),
        "nonce": secrets.token_hex(16),
        "subject": subject.strip(),
        "prediction": prediction.strip(),
        "rollback": rollback.strip(),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key, body, hashlib.sha256).hexdigest()
    payload["token"] = f"{TOKEN_PREFIX}.{_b64(body)}.{signature}"
    return payload


def verify(
    key: bytes,
    token: str,
    *,
    clock: Clock,
    store=None,
    subject: str = "",
    kinds: Iterable[str] | None = None,
    consume: bool = True,
) -> dict:
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise ApprovalError("malformed approval token")
    try:
        body = _unb64(parts[1])
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad token
        raise ApprovalError(f"malformed approval payload: {exc}") from exc
    expected = hmac.new(key, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[2]):
        raise ApprovalError("approval signature mismatch")
    payload = json.loads(body.decode("utf-8"))
    if float(payload.get("expires_at", 0)) <= clock.now():
        raise ApprovalError(f"approval {payload.get('approval_id')} expired at {payload.get('expires_at')}")
    allowed = tuple(kinds) if kinds else APPROVAL_KINDS
    if payload.get("kind") not in allowed:
        raise ApprovalError(f"approval kind {payload.get('kind')!r} not valid here (need {allowed})")
    if subject and payload.get("subject") != subject:
        raise ApprovalError("approval is bound to a different subject digest")
    if store is not None and consume:
        if not store.use_nonce(payload["nonce"], payload["approval_id"], subject or ""):
            raise ApprovalError(f"approval {payload.get('approval_id')} was already used (nonce replay)")
    return payload
