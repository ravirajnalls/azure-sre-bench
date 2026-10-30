"""Fixture lint checkers for the task corpus (issues #150-#154).

`bench lint-fixtures --check <name>` runs one registered checker over every
task's `fixtures/` directory. The `az` checker (issue #151) validates canned
`az` CLI JSON outputs against minimal JSON Schemas under `bench/schemas/az/`,
matched by fixture filename prefix (longest prefix wins, so
`az-network-vnet-subnet-show-pool2.json` maps to the subnet-show schema).

The registry is the extension point for the sibling checkers: k8s
(issue #150), consistency (#152), logs (#153), and leaks (#154) each register
a callable with the same signature. Findings carry a severity so a checker
decides its own error-vs-warning posture; the CLI fails (exit 1) only on
error-severity findings, which keeps the az check warning-only in CI by
construction.
"""

from __future__ import annotations

import email.utils
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SCHEMA_DIR = Path(__file__).parent / "schemas"

CheckFn = Callable[[Path], list["Finding"]]

# Registered checkers, name -> callable(tasks_root) -> findings.
CHECKS: dict[str, CheckFn] = {}

# Severity values; the CLI exits non-zero only on ERROR.
ERROR = "error"
WARNING = "warning"
SKIPPED = "skipped"


@dataclass(frozen=True)
class Finding:
    """One lint result for one fixture file."""

    check: str
    path: str
    severity: str  # ERROR | WARNING | SKIPPED
    message: str


def register_check(name: str) -> Callable[[CheckFn], CheckFn]:
    """Register a checker under `bench lint-fixtures --check <name>`."""

    def decorator(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn

    return decorator


def iter_fixture_dirs(tasks_root: Path) -> list[tuple[Path, Path]]:
    """Yield (task_dir, fixtures_dir) for every task with a fixtures/ folder."""
    pairs = []
    for task_dir in sorted(tasks_root.glob("*/*/")):
        fixtures = task_dir / "fixtures"
        if fixtures.is_dir():
            pairs.append((task_dir, fixtures))
    return pairs


# Fixture filename stem -> schema file under SCHEMA_DIR. Matched by
# longest-prefix: a stem matches when it equals the prefix or extends it with
# a `-suffix` (so task-specific variants like `-acr` or `-pool2` reuse the
# parent command's schema). Sorted longest-first at load time.
AZ_PREFIX_SCHEMAS: tuple[tuple[str, str], ...] = (
    ("az-aks-nodepool-list", "az/aks-nodepool-list.json"),
    ("az-aks-show", "az/aks-show.json"),
    ("az-identity-federated-credential-list", "az/federated-credential-list.json"),
    ("az-keyvault-show", "az/keyvault-show.json"),
    ("az-network-private-dns-link-list", "az/private-dns-link-list.json"),
    ("az-network-vnet-subnet-show", "az/vnet-subnet-show.json"),
    ("az-role-assignment-list", "az/role-assignment-list.json"),
)


def match_az_schema(stem: str) -> Path | None:
    """Return the schema path for a fixture stem, or None if no prefix maps."""
    for prefix, rel in sorted(AZ_PREFIX_SCHEMAS, key=lambda p: len(p[0]), reverse=True):
        if stem == prefix or stem.startswith(f"{prefix}-"):
            return SCHEMA_DIR / rel
    return None


def _validate_with_schema(data: object, schema_path: Path) -> list[str]:
    """Return sorted jsonschema error messages for `data` against the schema."""
    import jsonschema  # deferred: keeps `bench --help` cheap

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    errors = sorted(
        validator.iter_errors(data), key=lambda err: list(err.absolute_path)
    )
    return [
        f"{_format_path(err.absolute_path)}: {err.message}" if err.absolute_path else err.message
        for err in errors
    ]


def _format_path(parts: tuple) -> str:
    """Format a jsonschema instance path as `items.0.subject`-style text."""
    rendered = ".".join(str(part) for part in parts)
    return f"at {rendered}"


@register_check("az")
def check_az_fixtures(tasks_root: Path) -> list[Finding]:
    """Validate canned az JSON fixtures against their prefix-mapped schemas.

    Unknown `az-*` fixtures are skipped, not failed (issue #151 acceptance);
    everything this check reports is warning-severity, keeping the CI job
    warning-only. Non-`az-` JSON files are not az outputs and are ignored.
    """
    findings: list[Finding] = []
    for task_dir, fixtures in iter_fixture_dirs(tasks_root):
        for fixture in sorted(fixtures.glob("*.json")):
            rel = f"{task_dir.name}/fixtures/{fixture.name}"
            if not fixture.stem.startswith("az-"):
                continue  # not an az output (kubectl, logs, KQL, ...)
            schema_path = match_az_schema(fixture.stem)
            if schema_path is None:
                findings.append(
                    Finding(
                        check="az",
                        path=rel,
                        severity=SKIPPED,
                        message=(
                            "no schema matches this az fixture prefix "
                            "(unknown az fixtures are skipped, not failed)"
                        ),
                    )
                )
                continue
            try:
                data = json.loads(fixture.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                findings.append(
                    Finding(
                        check="az",
                        path=rel,
                        severity=WARNING,
                        message=f"fixture is not valid JSON: {exc}",
                    )
                )
                continue
            for problem in _validate_with_schema(data, schema_path):
                findings.append(
                    Finding(
                        check="az",
                        path=rel,
                        severity=WARNING,
                        message=f"shape violation against {schema_path.name}: {problem}",
                    )
                )
    return findings


def run_checks(tasks_root: Path, check_names: list[str]) -> list[Finding]:
    """Run the named checkers (registered in CHECKS) over the corpus."""
    findings: list[Finding] = []
    for name in check_names:
        if name not in CHECKS:
            raise ValueError(f"unknown check '{name}'; known: {', '.join(sorted(CHECKS))}")
        findings.extend(CHECKS[name](tasks_root))
    return findings


def print_report(findings: list[Finding]) -> None:
    """Print findings in a stable order, then a per-check summary."""
    for finding in sorted(findings, key=lambda f: (f.check, f.path, f.message)):
        tag = finding.severity.upper()
        print(f"{tag} {finding.path}: {finding.message}")
    checks_present = {f.check for f in findings}
    for name in sorted(checks_present):
        mine = [f for f in findings if f.check == name]
        errors = sum(1 for f in mine if f.severity == ERROR)
        warnings = sum(1 for f in mine if f.severity == WARNING)
        skipped = sum(1 for f in mine if f.severity == SKIPPED)
        checked = len(mine) - skipped
        print(
            f"{name}: {checked} fixture finding(s), "
            f"{warnings} warning(s), {skipped} skipped, {errors} error(s)"
        )


def cmd_lint_fixtures(args) -> int:
    """CLI entry: run the selected checkers; exit 1 only on error-severity findings."""
    tasks_root = Path(args.tasks_root)
    if not tasks_root.is_dir():
        print(f"error: tasks root '{tasks_root}' does not exist", file=sys.stderr)
        return 2
    check_names = (
        sorted(CHECKS) if args.check is None else [args.check]
    )
    findings = run_checks(tasks_root, check_names)
    print_report(findings)
    has_errors = any(f.severity == ERROR for f in findings)
    return 1 if has_errors else 0


# ---------------------------------------------------------------------------
# logs check (issue #153): timestamps and log realism
# ---------------------------------------------------------------------------


def _log_finding(task_id: str, rel_file: str, line_no: int, message: str) -> Finding:
    """Build a WARNING-severity logs finding anchored to a fixture line.

    The path already names the task; line 0 marks a whole-file finding.
    """
    prefix = f"line {line_no}: " if line_no else ""
    return Finding(check="logs", path=rel_file, severity=WARNING, message=f"{prefix}{message}")


@register_check("logs")
def check_logs(tasks_root: Path) -> list[Finding]:
    """Timestamp and log-realism check over every task's fixtures (issue #153).

    All findings are WARNING severity: the CI job surfaces them without
    failing the run; issue #155 owns promoting fixture checks to blocking.
    """
    findings: list[Finding] = []
    for task_id, task_dir in _task_dirs(tasks_root):
        findings.extend(_check_task_logs(task_id, task_dir))
    return findings


INCIDENT_WINDOW = timedelta(days=7)

# ISO 8601 with T or space separator, optional fractional seconds (any
# precision — KQL emits 7 digits), and Z / UTC / offset zone.
ISO_TS_RE = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(Z|UTC|[+-]\d{2}:?\d{2})?"
)

# kubelet/klog header, e.g. "I1013 07:38:52.114230       1 start.go:76]".
KLOG_RE = re.compile(r"^([IWEF])(\d{4}) (\d{2}):(\d{2}):(\d{2})\.(\d+) ")

# kubectl durations, chained largest-to-smallest units, e.g. 13m, 2m34s,
# 6d2h, 8000ms, 3h. The "(x39 over 12m)" repeat suffix is optional context.
AGE_TOKEN_RE = re.compile(
    r"^(\d+y)?(\d+w)?(\d+d)?(\d+h)?(\d+m)?(\d+s)?(\d+ms)?$"
)

# `kubectl describe pod` Events section row:
#   Normal   Pulling   12m (x4 over 13m)   kubelet   Pulling image "..."
# The age column is matched loosely (\S+) and validated by parse_age_token,
# so a malformed age surfaces as a finding instead of hiding the row.
DESCRIBE_EVENT_RE = re.compile(
    r"^\s+(Normal|Warning)\s+(\S+)\s+(\S+)\s+(\S+)"
)

# `kubectl get events` table row:
#   payments  29m  Warning  Evicted  pod/payments-api-...  The node was low ...
GET_EVENT_RE = re.compile(
    r"^(\S+)\s+(\S+)\s+(Normal|Warning)\s+(\S+)\s+(\S+)"
)

# Severity tokens that satisfy "log lines carry a level". LOG covers the
# PostgreSQL `... [pid] LOG:` convention.
LEVEL_RE = re.compile(
    r"\b(INFO|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|DEBUG|TRACE|NOTICE|LOG)\b"
)

# Envoy/istio-proxy native format: a lowercase severity word directly after
# the ISO timestamp, e.g. "2026-10-28T09:14:02.318Z warning envoy connection".
# Position (right after the timestamp) is the discriminator, so message text
# that merely contains the word "error" cannot produce false level evidence.
ENVOY_LEVEL_RE = re.compile(
    ISO_TS_RE.pattern + r"\s+(info|warning|error|critical|debug|trace)\b"
)

# Bracketed levels, e.g. ama-logs "2026-10-30T08:05:12Z [info] AgentHeartbeat:".
BRACKET_LEVEL_RE = re.compile(
    r"\[(info|warning|error|debug|critical|verbose|notice)\]", re.IGNORECASE
)

# Azure Functions host transcripts write lifecycle lines (Started/Executing/
# Succeeded/Failed) without severity levels; the host prefix is the structure.
FUNCTIONS_HOST_RE = re.compile(r"Function\.\S+|Host\.\S+\.Queue")

# Azure DevOps pipeline transcripts carry structure via ##[...] markers
# instead of severity levels.
ADO_MARKER_RE = re.compile(r"##\[(section|group|endgroup|command|warning|error)\]")

# A line starting one of these begins a new capture stream, resetting the
# monotonic-ordering baseline (e.g. a second `kubectl logs` in one fixture).
STREAM_BREAK_RE = re.compile(r"^[#$]")

# `kubectl describe pod` Start Time line (RFC 1123).
START_TIME_RE = re.compile(r"^\s*Start Time:\s+(.+)$")

# Real Kubernetes event reasons: core/kubelet/scheduler/controller-manager/
# cluster-autoscaler, HPA (SuccessfulScaleUp, SuccessfulRescale,
# FailedComputeMetricsReplicas), kubelet eviction (EvictionThresholdMet), and
# cert-manager controller (Issuing, Presented, SelfCheckFailed, ...).
KNOWN_EVENT_REASONS = frozenset(
    {
        "BackOff",
        "BackoffLimitExceeded",
        "Completed",
        "ContainerGCFailed",
        "Created",
        "DeadlineExceeded",
        "Drain",
        "ErrImageNeverPull",
        "ErrImagePull",
        "Evicted",
        "EvictionThresholdMet",
        "ExceededGracePeriod",
        "ExternalProvisioning",
        "Failed",
        "FailedAttachVolume",
        "FailedComputeMetricsReplicas",
        "FailedCreate",
        "FailedCreatePodSandBox",
        "FailedDaemonPod",
        "FailedDelete",
        "FailedKillPod",
        "FailedMount",
        "FailedScheduling",
        "Generated",
        "ImageGCFailed",
        "ImageInspectError",
        "ImagePullBackOff",
        "InsufficientMemory",
        "InvalidImageName",
        "Issuing",
        "Killing",
        "LeaderElection",
        "NodeAllocatableEnforced",
        "NodeHasDiskPressure",
        "NodeHasInsufficientMemory",
        "NodeHasInsufficientPID",
        "NodeHasNoDiskPressure",
        "NodeHasSufficientMemory",
        "NodeHasSufficientPID",
        "NodeNotReady",
        "NodeNotSchedulable",
        "NodeReady",
        "NodeSchedulable",
        "NotTriggerScaleUp",
        "OOMKilled",
        "OutOfMemory",
        "Preempted",
        "Presented",
        "ProbeWarning",
        "Provisioning",
        "ProvisioningFailed",
        "ProvisioningSucceeded",
        "Pulled",
        "Pulling",
        "ReadFailed",
        "Requested",
        "SandboxChanged",
        "ScaleDown",
        "ScaledUpCluster",
        "ScalingReplicaSet",
        "Scheduled",
        "SelfCheckFailed",
        "Started",
        "SuccessfulAttachVolume",
        "SuccessfulCreate",
        "SuccessfulDelete",
        "SuccessfulRescale",
        "SuccessfulScaleUp",
        "TaintManagerEviction",
        "TriggeredScaleUp",
        "Unhealthy",
        "VolumeBound",
    }
)


def parse_age_token(text: str) -> timedelta | None:
    """Parse a kubectl duration (13m, 2m34s, 6d2h, 8000ms); None if invalid."""
    match = AGE_TOKEN_RE.fullmatch(text.strip())
    if match is None or not any(match.groups()):
        return None
    years, weeks, days, hours, minutes, seconds, millis = (
        group or "" for group in match.groups()
    )
    return timedelta(
        days=(int(years[:-1]) if years else 0) * 365
        + (int(weeks[:-1]) if weeks else 0) * 7
        + (int(days[:-1]) if days else 0)
        + (int(hours[:-1]) if hours else 0) / 24
        + (int(minutes[:-1]) if minutes else 0) / 1440
        + (int(seconds[:-1]) if seconds else 0) / 86400
        + (int(millis[:-2]) if millis else 0) / 86400000
    )


def parse_iso_timestamp(text: str) -> datetime | None:
    """Parse an ISO 8601/KQL datetime anywhere in `text`; None if absent/invalid."""
    match = ISO_TS_RE.search(text)
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    micros = int((fraction or "0")[:6].ljust(6, "0"))
    try:
        stamp = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second),
            micros, tzinfo=timezone.utc,
        )
    except ValueError:  # syntactically plausible but not a real date-time
        return None
    return stamp if zone in (None, "Z", "UTC") else _apply_zone_offset(stamp, zone)


def _apply_zone_offset(stamp: datetime, zone: str) -> datetime:
    """Rewind `stamp` (parsed as UTC) by its written UTC offset."""
    sign = -1 if zone[0] == "+" else 1
    digits = zone[1:].replace(":", "")
    offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    return stamp + sign * offset


def _parse_klog_time(line: str) -> datetime | None:
    """Parse a klog header into a synthetic-year UTC datetime (no year given)."""
    match = KLOG_RE.match(line)
    if match is None:
        return None
    month, day = int(match.group(2)[:2]), int(match.group(2)[2:])
    try:
        return datetime(
            2000, month, day, int(match.group(3)), int(match.group(4)),
            int(match.group(5)), int(match.group(6)[:6].ljust(6, "0")),
            tzinfo=timezone.utc,
        )
    except ValueError:  # invalid month/day in a fabricated klog header
        return None


def _is_bin_timestamp(stamp: datetime) -> bool:
    """Midnight-exact timestamps are aggregation-bin labels, not event times."""
    return (
        stamp.hour == 0 and stamp.minute == 0 and stamp.second == 0
        and stamp.microsecond == 0
    )


def is_log_file(name: str) -> bool:
    """True for app-log captures: `*.log` or `*log*` (kubectl-logs-*.txt...)."""
    return name.endswith(".log") or "log" in name.lower()


def is_timeseries_csv(path: Path) -> bool:
    """True for CSVs whose header starts with a time column (KQL results)."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            first_column = line.split(",", 1)[0].strip().strip('"')
            return first_column.lower().startswith("time")
    return False


def _check_task_logs(task_id: str, task_dir: Path) -> list[Finding]:
    """Run the logs check over one task's fixtures (issue #153)."""
    findings: list[Finding] = []
    event_times: dict[str, list[datetime]] = {}
    for fixture in _fixtures(task_dir):
        rel_file = f"{task_id}/fixtures/{fixture.name}"
        # Level evidence applies to app-log text captures only; timeline
        # checks (monotonicity, incident window) additionally apply to
        # time-series CSVs. Other command-output fixtures legitimately carry
        # out-of-order or wide-span metadata (certificate validity dates,
        # status dumps), so they are parse-checked but not timeline-checked.
        log_like = fixture.suffix != ".csv" and is_log_file(fixture.name)
        timeline_like = log_like or (
            fixture.suffix == ".csv" and is_timeseries_csv(fixture)
        )
        file_findings, file_event_times = _check_fixture_file(
            task_id, rel_file, fixture,
            level_check=log_like, timeline_check=timeline_like,
        )
        findings.extend(file_findings)
        event_times[rel_file] = file_event_times
    findings.extend(_window_findings(task_id, event_times))
    return findings


def _fixtures(task_dir: Path) -> list[Path]:
    """Text fixtures eligible for the logs check, in stable order."""
    fixtures_dir = task_dir / "fixtures"
    if not fixtures_dir.is_dir():
        return []
    return sorted(
        fixture
        for fixture in fixtures_dir.iterdir()
        if fixture.is_file()
        and (
            fixture.suffix == ".csv" and is_timeseries_csv(fixture)
            or fixture.suffix in (".txt", ".log")
        )
    )


def _check_fixture_file(
    task_id: str, rel_file: str, path: Path,
    level_check: bool, timeline_check: bool,
) -> tuple[list[Finding], list[datetime]]:
    """Check one fixture file.

    Returns its findings and the event-time timestamps found in it (for the
    cross-file incident-window check; empty unless `timeline_check`).
    """
    findings: list[Finding] = []
    event_times: list[datetime] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    is_ado_transcript = ADO_MARKER_RE.search(text) is not None

    in_events_section = False
    is_event_table = False
    previous: datetime | None = None  # last timestamp in the current stream

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if STREAM_BREAK_RE.match(stripped):
            previous = None  # new capture stream: ordering baseline resets
            in_events_section = False
            is_event_table = False

        # --- timestamp parsing -------------------------------------------
        iso_match = ISO_TS_RE.search(line)
        stamp = parse_iso_timestamp(line) if iso_match else None
        klog_stamp = _parse_klog_time(line)
        start_time = _parse_start_time(line)

        if iso_match and stamp is None:
            findings.append(
                _log_finding(task_id, rel_file, line_no, "unparseable ISO 8601 timestamp")
            )
        if start_match_error(line):
            findings.append(
                _log_finding(task_id, rel_file, line_no, "unparseable 'Start Time:' timestamp")
            )

        # --- monotonic ordering within the current stream ------------------
        current = stamp or klog_stamp or start_time
        if current is not None and timeline_check:
            if previous is not None and current < previous:
                findings.append(
                    _log_finding(
                        task_id, rel_file, line_no,
                        f"timestamps not monotonic: {previous:%Y-%m-%dT%H:%M:%S} "
                        f"followed by {current:%Y-%m-%dT%H:%M:%S}",
                    )
                )
            previous = current
            if (start_time is not None or klog_stamp is not None) or (
                stamp is not None and not _is_bin_timestamp(stamp)
            ):
                event_times.append(current)

        # --- level evidence on timestamped app-log lines --------------------
        if (
            stamp is not None
            and level_check
            and not is_ado_transcript
            and not _has_level_evidence(line)
        ):
            findings.append(
                _log_finding(
                    task_id, rel_file, line_no,
                    "timestamped log line carries no severity level",
                )
            )

        # --- kubectl event rows --------------------------------------------
        if re.match(r"NAMESPACE\s+LAST SEEN", stripped):
            is_event_table = True  # header line itself is not an event row
            continue
        in_events_section, is_event_table = _update_event_section_state(
            line, stripped, in_events_section, is_event_table
        )
        if in_events_section:
            event_match = DESCRIBE_EVENT_RE.match(line)
            if event_match is not None:
                _validate_event_row(
                    findings, task_id, rel_file, line_no,
                    event_match.group(2), event_match.group(3),
                )
        elif is_event_table:
            event_match = GET_EVENT_RE.match(line)
            if event_match is not None:
                _validate_event_row(
                    findings, task_id, rel_file, line_no,
                    event_match.group(4), event_match.group(2),
                )
            else:
                is_event_table = False  # table ended

    return findings, event_times


def _parse_start_time(line: str) -> datetime | None:
    """Parse an RFC 1123 `Start Time:` line; None if absent or malformed."""
    match = START_TIME_RE.match(line)
    if match is None:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(match.group(1))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def start_match_error(line: str) -> bool:
    """True when a `Start Time:` line exists but its date does not parse."""
    return START_TIME_RE.match(line) is not None and _parse_start_time(line) is None


def _has_level_evidence(line: str) -> bool:
    return bool(
        LEVEL_RE.search(line)
        or BRACKET_LEVEL_RE.search(line)
        or FUNCTIONS_HOST_RE.search(line)
        or ENVOY_LEVEL_RE.search(line)
    )


def _update_event_section_state(
    line: str, stripped: str, in_events: bool, is_event_table: bool
) -> tuple[bool, bool]:
    """Track whether the current line is inside a kubectl events listing.

    Callers handle the `NAMESPACE LAST SEEN` get-events header themselves.
    """
    if stripped.startswith("Events:"):
        return True, False
    if in_events:
        # A describe Events section ends at a blank line or an unindented line.
        return bool(stripped) and line[:2] == "  ", False
    return in_events, is_event_table


def _validate_event_row(
    findings: list[Finding],
    task_id: str,
    rel_file: str,
    line_no: int,
    reason: str,
    age_text: str,
) -> None:
    """Check one kubectl event row's age format and reason string."""
    if parse_age_token(age_text.split(" (")[0]) is None:
        findings.append(
            _log_finding(
                task_id, rel_file, line_no,
                f"unparseable kubectl event age '{age_text}'",
            )
        )
    if reason not in KNOWN_EVENT_REASONS:
        findings.append(
            _log_finding(
                task_id, rel_file, line_no,
                f"unknown Kubernetes event reason '{reason}'",
            )
        )


def _window_findings(
    task_id: str, event_times: dict[str, list[datetime]]
) -> list[Finding]:
    """Event-time evidence across a task's files must fit the incident window."""
    dated = {name: times for name, times in event_times.items() if times}
    all_times = [stamp for times in dated.values() for stamp in times]
    if len(all_times) < 2:
        return []
    span = max(all_times) - min(all_times)
    if span <= INCIDENT_WINDOW:
        return []
    widest = max(dated, key=lambda name: max(dated[name]))
    return [
        _log_finding(
            task_id, widest, 0,
            f"event timestamps span {span.days} days, wider than the "
            f"{INCIDENT_WINDOW.days}-day incident window",
        )
    ]


def _task_dirs(tasks_root: Path) -> list[tuple[str, Path]]:
    """(task_id, task_dir) pairs for every directory containing a task.yaml."""
    task_dirs = []
    for task_yaml in sorted(tasks_root.rglob("task.yaml")):
        if task_yaml.is_file():
            task_dir = task_yaml.parent
            task_dirs.append((task_dir.name, task_dir))
    return sorted(task_dirs)


# Sibling checkers live in their own modules and are bound into CHECKS here,
# from this module's tail: bench.tasks imports bench.lint_k8s directly, so the
# checker cannot import this module at load time — its functions import the
# shared Finding/severity helpers lazily instead.
from bench.lint_k8s import check_k8s_fixtures as _k8s_check  # noqa: E402,F401

CHECKS["k8s"] = _k8s_check
