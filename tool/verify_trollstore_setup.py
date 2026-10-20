#!/usr/bin/env python3
"""Static consistency verifier for the ProxyPin TrollStore probe foundation.

Checks that the iOS side is internally consistent for a TrollStore build:
bundle identifiers, provider bundle id contract, display name, entitlements,
the CI workflow (structure and key steps), and the git working-tree scope.

Usage:
    python tool/verify_trollstore_setup.py

Exit code:
    0  every check passed
    1  at least one check failed

Only the Python 3 standard library is required.  When PyYAML is installed the
workflow file is additionally parsed for structural validation; otherwise the
workflow checks degrade to key-string presence checks (this is reported).
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml  # type: ignore

    HAVE_YAML = True
except ImportError:
    yaml = None  # type: ignore
    HAVE_YAML = False

REPO_ROOT = Path(__file__).resolve().parents[1]

IOS_PBXPROJ = REPO_ROOT / "ios" / "Runner.xcodeproj" / "project.pbxproj"
MACOS_PBXPROJ = REPO_ROOT / "macos" / "Runner.xcodeproj" / "project.pbxproj"
VPN_MANAGER = REPO_ROOT / "ios" / "Runner" / "VpnManager.swift"
INFO_PLIST = REPO_ROOT / "ios" / "Runner" / "Info.plist"
RUNNER_ENTITLEMENTS = REPO_ROOT / "ios" / "Runner" / "Runner.entitlements"
APPEX_ENTITLEMENTS = REPO_ROOT / "ios" / "ProxyPin" / "ProxyPin.entitlements"
MAIN_YML = REPO_ROOT / ".github" / "workflows" / "main.yml"
CODEMAGIC_YML = REPO_ROOT / "codemagic.yaml"

# --- contract constants (per team spec) -------------------------------------
EXPECTED_RUNNER_BUNDLE_ID = "com.proxy.pin.troll"
EXPECTED_APPEX_BUNDLE_ID = "com.proxy.pin.troll.ProxyPin"
OLD_APPEX_BUNDLE_ID = "com.proxy.pin.ProxyPin"
RUNNER_TESTS_BUNDLE_ID = "com.proxy.pin.networkProxyFlutter.RunnerTests"
EXPECTED_DISPLAY_NAME = "ProxyPin Troll"
EXPECTED_TROLL_ID_COUNT = 6  # 3 Runner configs + 3 appex configs
EXPECTED_CONFIG_COUNT = 3  # Debug / Release / Profile per target
NE_ENTITLEMENTS_KEY = "com.apple.developer.networking.networkextension"
NE_VALUE = "packet-tunnel-provider"

# Acceptable hosted macOS runner labels. macos-latest (standard arm64) is
# capacity-starved; macos-15-xlarge (free for public repos) is the fallback.
ACCEPTABLE_RUNNERS = {"macos-latest", "macos-14", "macos-15", "macos-15-xlarge"}

MAIN_YML_KEY_STRINGS = (
    "flutter build ios --release --no-codesign",
    "Patch device_info_plus for current iOS SDK",
    "Select newest Xcode (iOS 26 SDK)",
    "PlugIns/ProxyPin.appex",
    "ldid -Sios/Runner/Runner.entitlements",
    "ldid -Sios/ProxyPin/ProxyPin.entitlements",
    "zip -qry",
    "ProxyPin-Troll.tipa",
    "upload-artifact",
    "trollstore-latest",
    "runs-on",
    "workflow_dispatch",
    "github.event_name == 'workflow_dispatch'",
)

MAIN_YML_STEP_ORDER = (
    "Build iOS (unsigned)",
    "Sign binaries with ldid",
    "Package .tipa",
    "Upload artifact",
    "Attach to rolling release",
)

# --- Codemagic pipeline (primary builder; see codemagic.yaml header) ---------
# GitHub-hosted standard macOS runners are capacity-starved for this account,
# so the .tipa is built on Codemagic (free plan, 500 macOS M2 min/month) with
# the identical unsigned-build -> ldid-sign -> .tipa pipeline.
CODEMAGIC_WORKFLOW_ID = "trollstore-tipa"
CODEMAGIC_KEY_STRINGS = (
    "flutter build ios --release --no-codesign",
    "Patch device_info_plus for current iOS SDK",
    "PlugIns/ProxyPin.appex",
    "-Sios/Runner/Runner.entitlements",
    "-Sios/ProxyPin/ProxyPin.entitlements",
    "zip -qry",
    "ProxyPin-Troll.tipa",
    "mac_mini_m2",
    "trollstore-latest",
    "GITHUB_TOKEN",
    "packet-tunnel-provider",
)
CODEMAGIC_SCRIPT_ORDER = (
    "Build iOS (unsigned)",
    "Sign binaries with ldid",
    "Package .tipa",
)

# Buggy framework-bin derivation: "$fw/${fw##*/}" keeps the ".framework"
# suffix, so the file never exists and frameworks are silently not signed.
FRAMEWORK_BUGGY_MARKER = '$fw/${fw##*/}'
FRAMEWORK_FIXED_MARKERS = ('basename "$fw" .framework', "${fw%.framework}")

# Working-tree changes are allowed only for these deliverable paths (any
# status form: M / ?? / A). Anything else is out-of-scope and fails.
ALLOWED_WORKTREE_PATHS = {
    "ios/Runner.xcodeproj/project.pbxproj",
    "ios/Runner/Info.plist",
    "ios/Runner/VpnManager.swift",
    ".github/workflows/main.yml",
    "codemagic.yaml",
    "tool/verify_trollstore_setup.py",
}

# The deliverables must be committed in HEAD (post-commit state).
EXPECTED_COMMITTED_PATHS = (
    "ios/Runner.xcodeproj/project.pbxproj",
    "ios/Runner/Info.plist",
    "ios/Runner/VpnManager.swift",
    ".github/workflows/main.yml",
    "codemagic.yaml",
    "tool/verify_trollstore_setup.py",
)

BUNDLE_ID_RE = re.compile(r"PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);")


class CheckResult:
    """Outcome of a single assertion inside a check group."""

    __slots__ = ("ok", "label", "detail")

    def __init__(self, ok: bool, label: str, detail: str = "") -> None:
        self.ok = ok
        self.label = label
        self.detail = detail


class TrollstoreSetupVerifier:
    """Runs every static consistency check and aggregates a report."""

    def __init__(self) -> None:
        self.results: list[tuple[str, list[CheckResult]]] = []

    # -- harness -------------------------------------------------------------
    def run_group(self, name: str, func) -> None:
        try:
            self.results.append((name, func()))
        except Exception as exc:  # a raising check is a failed check
            self.results.append((name, [CheckResult(False, name, f"exception: {exc}")]))

    @property
    def total(self) -> int:
        return sum(len(items) for _, items in self.results)

    @property
    def failed(self) -> int:
        return sum(1 for _, items in self.results for r in items if not r.ok)

    # -- check groups --------------------------------------------------------
    def check_bundle_ids(self) -> list[CheckResult]:
        out: list[CheckResult] = []
        text = IOS_PBXPROJ.read_text(encoding="utf-8")

        troll_count = text.count("com.proxy.pin.troll")
        out.append(
            CheckResult(
                troll_count == EXPECTED_TROLL_ID_COUNT,
                "'com.proxy.pin.troll' occurrences in ios pbxproj",
                f"expected {EXPECTED_TROLL_ID_COUNT}, got {troll_count}",
            )
        )

        old_count = text.count(OLD_APPEX_BUNDLE_ID)
        out.append(
            CheckResult(
                old_count == 0,
                "old appex bundle id absent from ios pbxproj",
                f"expected 0, got {old_count}",
            )
        )

        ids = BUNDLE_ID_RE.findall(text)
        for bundle_id in (EXPECTED_RUNNER_BUNDLE_ID, EXPECTED_APPEX_BUNDLE_ID):
            count = ids.count(bundle_id)
            out.append(
                CheckResult(
                    count == EXPECTED_CONFIG_COUNT,
                    f"bundle id {bundle_id} build configs",
                    f"expected {EXPECTED_CONFIG_COUNT}, got {count}",
                )
            )

        test_count = ids.count(RUNNER_TESTS_BUNDLE_ID)
        out.append(
            CheckResult(
                test_count == EXPECTED_CONFIG_COUNT,
                "RunnerTests bundle id untouched (3 configs)",
                f"expected {EXPECTED_CONFIG_COUNT}, got {test_count}",
            )
        )

        leftovers = sorted(set(ids) - {EXPECTED_RUNNER_BUNDLE_ID,
                                       EXPECTED_APPEX_BUNDLE_ID,
                                       RUNNER_TESTS_BUNDLE_ID})
        out.append(
            CheckResult(
                not leftovers,
                "no unexpected bundle ids in ios pbxproj",
                f"unexpected: {leftovers}" if leftovers else "none",
            )
        )
        return out

    def check_provider_bundle_id(self) -> list[CheckResult]:
        out: list[CheckResult] = []
        text = VPN_MANAGER.read_text(encoding="utf-8")
        match = re.search(r'providerBundleIdentifier\s*=\s*"([^"]+)"', text)
        value = match.group(1) if match else None
        out.append(
            CheckResult(
                value == EXPECTED_APPEX_BUNDLE_ID,
                "VpnManager.createProviderManager providerBundleIdentifier",
                f"expected {EXPECTED_APPEX_BUNDLE_ID}, got {value}",
            )
        )

        # Cross-file contract: the value must equal an appex PRODUCT_BUNDLE_IDENTIFIER.
        pbx_text = IOS_PBXPROJ.read_text(encoding="utf-8")
        appex_declared = f"PRODUCT_BUNDLE_IDENTIFIER = {value};" in pbx_text if value else False
        out.append(
            CheckResult(
                appex_declared,
                "providerBundleIdentifier matches appex PRODUCT_BUNDLE_IDENTIFIER",
                "consistent" if appex_declared else "mismatch with pbxproj",
            )
        )

        # The whole ios scope must be free of the old appex bundle id.
        leftovers: list[str] = []
        for path in sorted((REPO_ROOT / "ios").rglob("*")):
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if OLD_APPEX_BUNDLE_ID in content:
                leftovers.append(str(path.relative_to(REPO_ROOT)))
        out.append(
            CheckResult(
                not leftovers,
                "old appex bundle id absent from entire ios/ scope",
                f"found in: {leftovers}" if leftovers else "none",
            )
        )
        return out

    def check_display_name(self) -> list[CheckResult]:
        out: list[CheckResult] = []
        try:
            with INFO_PLIST.open("rb") as handle:
                data = plistlib.load(handle)
            name = data.get("CFBundleDisplayName")
            out.append(
                CheckResult(
                    name == EXPECTED_DISPLAY_NAME,
                    "Info.plist CFBundleDisplayName",
                    f"expected {EXPECTED_DISPLAY_NAME!r}, got {name!r}",
                )
            )
        except Exception as exc:
            out.append(CheckResult(False, "Info.plist parse", f"exception: {exc}"))
        return out

    def check_entitlements(self) -> list[CheckResult]:
        out: list[CheckResult] = []
        for path in (RUNNER_ENTITLEMENTS, APPEX_ENTITLEMENTS):
            label = path.relative_to(REPO_ROOT).as_posix()
            try:
                with path.open("rb") as handle:
                    data = plistlib.load(handle)
                values = data.get(NE_ENTITLEMENTS_KEY) or []
                out.append(
                    CheckResult(
                        NE_VALUE in values,
                        f"{label} contains {NE_VALUE}",
                        f"values={values}",
                    )
                )
            except Exception as exc:
                out.append(CheckResult(False, f"{label} parse", f"exception: {exc}"))

        # 跨应用悬浮窗需要 springboard.overlay 授权（仅 Runner，appex 不需要）
        try:
            with RUNNER_ENTITLEMENTS.open("rb") as handle:
                rdata = plistlib.load(handle)
            has_overlay = bool(rdata.get("com.apple.private.springboard.overlay"))
            out.append(
                CheckResult(
                    has_overlay,
                    "Runner.entitlements contains com.apple.private.springboard.overlay",
                    f"keys={list(rdata.keys())}",
                )
            )
        except Exception as exc:
            out.append(CheckResult(False, "Runner.entitlements overlay check", f"exception: {exc}"))
        return out

    def check_workflow(self) -> list[CheckResult]:
        out: list[CheckResult] = []
        if not MAIN_YML.is_file():
            out.append(CheckResult(False, "main.yml exists", f"missing: {MAIN_YML}"))
            return out
        text = MAIN_YML.read_text(encoding="utf-8")
        lines = text.splitlines()

        missing = [s for s in MAIN_YML_KEY_STRINGS if s not in text]
        out.append(
            CheckResult(
                not missing,
                "main.yml key step strings present",
                f"missing: {missing}" if missing else "all present",
            )
        )

        if HAVE_YAML:
            try:
                doc = yaml.safe_load(text)
                triggers_raw = doc.get("on")
                if triggers_raw is None:
                    triggers_raw = doc.get(True)  # YAML 1.1 parses unquoted `on:` as boolean key
                triggers = triggers_raw or {}
                out.append(
                    CheckResult(
                        "workflow_dispatch" in triggers and "push" in triggers,
                        "workflow triggers (workflow_dispatch + push)",
                        f"triggers={sorted(triggers)}",
                    )
                )
                out.append(
                    CheckResult(
                        (doc.get("permissions") or {}).get("contents") == "write",
                        "permissions contents: write",
                        f"permissions={doc.get('permissions')}",
                    )
                )
                job = (doc.get("jobs") or {}).get("build", {})
                out.append(
                    CheckResult(
                        job.get("runs-on") in ACCEPTABLE_RUNNERS,
                        "job runs-on an available macOS runner",
                        f"runs-on={job.get('runs-on')}",
                    )
                )
                steps = [s.get("name", "") for s in job.get("steps", [])]
                positions = []
                for name in MAIN_YML_STEP_ORDER:
                    positions.append(steps.index(name) if name in steps else -1)
                ordered = all(a != -1 for a in positions) and positions == sorted(positions)
                out.append(
                    CheckResult(
                        ordered,
                        "signing steps ordered build -> sign -> package -> upload -> release",
                        f"positions={dict(zip(MAIN_YML_STEP_ORDER, positions))}",
                    )
                )
            except Exception as exc:
                out.append(CheckResult(False, "main.yml YAML parse", f"exception: {exc}"))
        else:
            out.append(
                CheckResult(
                    True,
                    "main.yml YAML structural validation",
                    "SKIPPED: PyYAML not installed; degraded to key-string presence checks",
                )
            )

        # Framework signing: the binary must live at Foo.framework/Foo, i.e. the
        # ".framework" suffix must be stripped. The unfixed derivation keeps the
        # suffix and silently misses every framework.
        line_no = next((i for i, line in enumerate(lines, 1) if FRAMEWORK_BUGGY_MARKER in line), None)
        fixed = any(marker in text for marker in FRAMEWORK_FIXED_MARKERS)
        out.append(
            CheckResult(
                fixed,
                "framework signing derives binary path without .framework suffix",
                f"main.yml:{line_no} still uses {FRAMEWORK_BUGGY_MARKER!r}"
                if line_no and not fixed
                else "fixed derivation present",
            )
        )
        return out

    def check_codemagic(self) -> list[CheckResult]:
        out: list[CheckResult] = []
        if not CODEMAGIC_YML.is_file():
            out.append(CheckResult(False, "codemagic.yaml exists", f"missing: {CODEMAGIC_YML}"))
            return out
        text = CODEMAGIC_YML.read_text(encoding="utf-8")

        missing = [s for s in CODEMAGIC_KEY_STRINGS if s not in text]
        out.append(
            CheckResult(
                not missing,
                "codemagic.yaml key step strings present",
                f"missing: {missing}" if missing else "all present",
            )
        )

        if HAVE_YAML:
            try:
                doc = yaml.safe_load(text)
                workflows = doc.get("workflows") or {}
                wf = workflows.get(CODEMAGIC_WORKFLOW_ID)
                out.append(
                    CheckResult(
                        wf is not None,
                        f"workflow {CODEMAGIC_WORKFLOW_ID!r} defined",
                        f"workflows={sorted(workflows)}",
                    )
                )
                if wf is not None:
                    env = wf.get("environment") or {}
                    out.append(
                        CheckResult(
                            wf.get("instance_type") == "mac_mini_m2",
                            "instance_type mac_mini_m2 (free plan)",
                            f"instance_type={wf.get('instance_type')}",
                        )
                    )
                    out.append(
                        CheckResult(
                            env.get("flutter") == "stable",
                            "flutter stable",
                            f"flutter={env.get('flutter')}",
                        )
                    )
                    out.append(
                        CheckResult(
                            str(env.get("xcode", "")).startswith("26"),
                            "xcode 26.x (iOS 26 SDK for NE/SceneDelegate APIs)",
                            f"xcode={env.get('xcode')}",
                        )
                    )
                    out.append(
                        CheckResult(
                            bool(wf.get("artifacts")),
                            "artifacts declared (.tipa + sign report)",
                            f"artifacts={wf.get('artifacts')}",
                        )
                    )
                    scripts = [s.get("name", "") for s in wf.get("scripts", [])]
                    positions = []
                    for name in CODEMAGIC_SCRIPT_ORDER:
                        positions.append(scripts.index(name) if name in scripts else -1)
                    ordered = all(a != -1 for a in positions) and positions == sorted(positions)
                    out.append(
                        CheckResult(
                            ordered,
                            "signing steps ordered build -> sign -> package",
                            f"positions={dict(zip(CODEMAGIC_SCRIPT_ORDER, positions))}",
                        )
                    )
            except Exception as exc:
                out.append(CheckResult(False, "codemagic.yaml YAML parse", f"exception: {exc}"))
        else:
            out.append(
                CheckResult(
                    True,
                    "codemagic.yaml YAML structural validation",
                    "SKIPPED: PyYAML not installed; degraded to key-string presence checks",
                )
            )

        # Framework signing must derive the binary path without the .framework
        # suffix (Foo.framework/Foo), otherwise every framework is silently
        # missed. The buggy derivation keeps the suffix.
        buggy = FRAMEWORK_BUGGY_MARKER in text
        fixed = any(marker in text for marker in FRAMEWORK_FIXED_MARKERS)
        out.append(
            CheckResult(
                fixed and not buggy,
                "codemagic framework signing derives binary path without .framework suffix",
                "fixed derivation present" if fixed and not buggy else
                f"buggy marker present={buggy}, fixed marker present={fixed}",
            )
        )
        return out

    def check_git_scope(self) -> list[CheckResult]:
        out: list[CheckResult] = []
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            out.append(
                CheckResult(
                    True,
                    "git working-tree scope",
                    "SKIPPED: git not available in PATH",
                )
            )
            return out
        if proc.returncode != 0:
            out.append(
                CheckResult(
                    False,
                    "git working-tree scope",
                    f"git status failed: {proc.stderr.strip()}",
                )
            )
            return out

        entries = {line.rstrip("\n") for line in proc.stdout.splitlines() if line.strip()}

        def path_of(entry: str) -> str:
            rest = entry[3:] if len(entry) > 3 else entry
            if " -> " in rest:
                rest = rest.split(" -> ", 1)[1]
            return rest.strip().strip('"')

        unexpected = sorted(e for e in entries if path_of(e) not in ALLOWED_WORKTREE_PATHS)
        out.append(
            CheckResult(
                not unexpected,
                "no out-of-scope working-tree changes",
                f"unexpected: {unexpected}" if unexpected else
                ("clean tree" if not entries else "only allowed deliverables"),
            )
        )
        try:
            ls_tree = subprocess.run(
                ["git", "ls-tree", "-r", "HEAD", "--name-only"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            ls_tree = None
        if ls_tree is not None and ls_tree.returncode == 0:
            committed = set(ls_tree.stdout.splitlines())
            missing_committed = [p for p in EXPECTED_COMMITTED_PATHS if p not in committed]
            out.append(
                CheckResult(
                    not missing_committed,
                    "deliverable files committed in HEAD",
                    f"missing: {missing_committed}" if missing_committed else "all present",
                )
            )
        else:
            out.append(
                CheckResult(
                    False,
                    "deliverable files committed in HEAD",
                    "git ls-tree unavailable",
                )
            )
        return out

    def check_macos_scope(self) -> list[CheckResult]:
        out: list[CheckResult] = []
        if not MACOS_PBXPROJ.is_file():
            out.append(CheckResult(True, "macOS pbxproj scope", "SKIPPED: file not found"))
            return out
        text = MACOS_PBXPROJ.read_text(encoding="utf-8")
        out.append(
            CheckResult(
                "com.proxy.pin.troll" not in text,
                "macOS pbxproj not contaminated with troll bundle ids",
                "unchanged" if "com.proxy.pin.troll" not in text else "troll id leaked",
            )
        )
        out.append(
            CheckResult(
                "com.proxy.pin" in text,
                "macOS pbxproj still uses original bundle id (expected: out of scope)",
                "original id present",
            )
        )
        return out

    # -- reporting -----------------------------------------------------------
    def report(self) -> str:
        lines = [
            "=" * 72,
            "ProxyPin TrollStore setup - static verification",
            f"repo: {REPO_ROOT}",
            f"python: {sys.version.split()[0]}",
            f"pyyaml: {'available (structural checks enabled)' if HAVE_YAML else 'NOT installed (degraded to key-string checks)'}",
            "=" * 72,
        ]
        for index, (group, items) in enumerate(self.results, 1):
            lines.append(f"\n[{index}] {group}")
            for result in items:
                mark = "PASS" if result.ok else "FAIL"
                lines.append(f"  [{mark}] {result.label}")
                if result.detail:
                    lines.append(f"        -> {result.detail}")
        passed = self.total - self.failed
        lines += [
            "",
            "-" * 72,
            f"Summary: {self.total} checks | {passed} passed | {self.failed} failed",
            f"RESULT: {'PASS' if self.failed == 0 else 'FAIL'}",
            "-" * 72,
        ]
        return "\n".join(lines)

    def run(self) -> bool:
        self.run_group("pbxproj bundle identifiers (ios)", self.check_bundle_ids)
        self.run_group("VpnManager / provider bundle id contract", self.check_provider_bundle_id)
        self.run_group("Info.plist display name", self.check_display_name)
        self.run_group("entitlements (Runner + appex)", self.check_entitlements)
        self.run_group("CI workflow .github/workflows/main.yml", self.check_workflow)
        self.run_group("CI pipeline codemagic.yaml (primary builder)", self.check_codemagic)
        self.run_group("git working-tree scope", self.check_git_scope)
        self.run_group("macOS scope guard", self.check_macos_scope)
        return self.failed == 0


def main() -> int:
    verifier = TrollstoreSetupVerifier()
    ok = verifier.run()
    print(verifier.report())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
