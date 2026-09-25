import hashlib
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.cleanup import (_atomic_delete,
                                   approve_cleanup_plan, build_cleanup_plan,
                                   execute_cleanup, _lease_established,
                                   write_cleanup_plan)
from jev_git_graph.cli import main as jg_main
from jev_git_graph.coordinator import (CleanupActionJournal,
                                       CooperativeBranchLeaseAdapter,
                                       CreatorLeaseCapability,
                                       CreatorRuntimeValidationError,
                                       _REVIEWED_COOPERATIVE_BRANCH_LEASE_RUNTIME_8603_SHA,
                                       _REVIEWED_CANDIDATE_LIFECYCLE_RUNTIME_FF1EF33_SHA,
                                       _REVIEWED_DEPLOY_SYNC_RUNTIME_COMPAT_SHA,
                                       _REVIEWED_DEPLOY_SYNC_RUNTIME_COMPAT_7227_SHA,
                                       _REVIEWED_DEPLOY_SYNC_RUNTIME_8607_SHA,
                                       _REVIEWED_DEPLOY_SYNC_RUNTIME_8617_SHA,
                                       _REVIEWED_VERDICT_LIFECYCLE_RUNTIME_COMPAT_SHA,
                                       _REVIEWED_VERDICT_LIFECYCLE_RUNTIME_EB54_SHA,
                                       _REVIEWED_VERDICT_LIFECYCLE_RUNTIME_7A305FF_SHA,
                                       _REVIEWED_HOOK_FILES,
                                       _is_reviewed_runtime_hook_digest,
                                       _common_dir,
                                       _attest_process_generation,
                                       _verify_process_startup_attestation,
                                       _verify_loaded_runtime_jobs,
                                       _verify_train_construction_runtime,
                                       _verify_runtime_hook_files,
                                       resolve_production_creator_capability_when_ready,
                                       build_disposable_fixture_inventory,
                                       capability_receipt_metadata,
                                       cleanup_action_id,
                                       production_capability_diagnostic,
                                       reconcile_interrupted_cleanup)
from jev_git_graph.errors import JgError
from jev_git_graph.inventory import build_inventory
from jev_git_graph.safety import digest, opaque_path_id


def run(repo: Path, *args: str, env=None) -> str:
    result = subprocess.run(("git", "-C", str(repo), *args), check=True, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.decode().strip()


def _ref_exists(repo: Path, name: str) -> bool:
    result = subprocess.run(
        ("git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{name}"),
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def coverage_for(repo: Path, records: list[dict]) -> dict:
    main_tip = run(repo, "rev-parse", "main")
    branches = [{"name": "main", "tip": main_tip, "main_tip": main_tip,
                 "verdict": "EXACT", "reason": None,
                 "last_activity_epoch": None, "paths": []}]
    branches.extend(records)
    return {
        "kind": "branch-coverage", "schema_version": 1,
        "repository_id": opaque_path_id(repo), "inventory_digest": "0" * 64,
        "main": {"name": "main", "tip": main_tip}, "recent_hours": 24,
        "activity_cutoff_epoch": 100,
        "branches": branches, "network_performed": False,
        "destructive_action_authorized": False,
    }


def build_old_plan(*args, **kwargs):
    # Fixture commits are created now; simulate a verified old reflog epoch.
    with patch("jev_git_graph.cleanup._activity", return_value=1):
        return build_cleanup_plan(*args, **kwargs)


class CleanupTests(unittest.TestCase):
    @staticmethod
    def _readiness_capability(root: Path) -> CreatorLeaseCapability:
        label = "com.mikebook.pr-convergence-wake-consumer"
        return CreatorLeaseCapability(
            common_dir=root, runtime_roots=(root / "stable", root / "compat", root / "canonical"),
            hook_digests=((str(root / "stable"), "hook.py", "1" * 64),),
            loaded_runtime_jobs=((label, str(root / "consumer.plist"), str(root / "stable"), "a" * 64),),
            lock_root=root / "locks", train_construction_runtime=root / "train",
            train_construction_commit=None,
        )

    @staticmethod
    def _readiness_wrapper_error(*, label="com.mikebook.pr-convergence-wake-consumer",
                                 pid=7312, start="start-one", root="/runtime",
                                 cwd="/runtime", kind="reviewed_wrapper"):
        return CreatorRuntimeValidationError(
            f"runtime_adoption_unverified: wrapper {label}",
            failure_stage="process_image_mismatch", label=label, pid=pid,
            expected_root=root, observed_root=cwd, process_command="bash reviewed launcher",
            process_cwd=cwd, process_start=start, observed_process_kind=kind,
        )

    def test_pr_wake_readiness_wrapper_to_verified_daemon_returns_fresh_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capability = self._readiness_capability(root)
            wrapper = self._readiness_wrapper_error()
            wrapper._readiness_hook_context = "hooks-one"
            observations = {"com.mikebook.pr-convergence-wake-consumer": {
                "kind": "reviewed_daemon", "pid": 7312,
                "start_sha256": hashlib.sha256(b"start-one").hexdigest(),
                "root_sha256": hashlib.sha256(b"/runtime").hexdigest(),
                "cwd_sha256": hashlib.sha256(b"/runtime").hexdigest(),
            }}
            with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                       side_effect=[wrapper, (capability, observations, "hooks-one")]) as resolver, \
                    patch("jev_git_graph.coordinator.time.sleep") as sleep:
                result = resolve_production_creator_capability_when_ready(root)
            self.assertIs(capability, result)
            self.assertEqual(2, resolver.call_count)
            sleep.assert_called_once_with(0.2)

    def test_pr_wake_readiness_wrapper_to_valid_idle_returns_fresh_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capability = self._readiness_capability(root)
            wrapper = self._readiness_wrapper_error()
            wrapper._readiness_hook_context = "hooks-one"
            observations = {"com.mikebook.pr-convergence-wake-consumer": {
                "kind": "idle", "pid": None,
            }}
            with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                       side_effect=[wrapper, (capability, observations, "hooks-one")]) as resolver, \
                    patch("jev_git_graph.coordinator.time.sleep"):
                result = resolve_production_creator_capability_when_ready(root)
            self.assertIs(capability, result)
            self.assertEqual(2, resolver.call_count)

    def test_pr_wake_readiness_permanent_wrapper_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = self._readiness_wrapper_error()
            wrapper._readiness_hook_context = "hooks-one"
            with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                       side_effect=wrapper) as resolver, \
                    patch("jev_git_graph.coordinator.time.sleep") as sleep:
                with self.assertRaises(CreatorRuntimeValidationError) as caught:
                    resolve_production_creator_capability_when_ready(root)
            self.assertIs(wrapper, caught.exception)
            self.assertEqual(6, resolver.call_count)
            self.assertEqual(5, sleep.call_count)

    def test_pr_wake_readiness_elapsed_deadline_stops_early(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = self._readiness_wrapper_error()
            wrapper._readiness_hook_context = "hooks-one"
            with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                       side_effect=wrapper) as resolver, \
                    patch("jev_git_graph.coordinator.time.monotonic",
                          side_effect=[0.0, 0.0, 3.0]), \
                    patch("jev_git_graph.coordinator.time.sleep") as sleep:
                with self.assertRaises(CreatorRuntimeValidationError):
                    resolve_production_creator_capability_when_ready(root)
            self.assertEqual(1, resolver.call_count)
            sleep.assert_called_once_with(0.2)

    def test_pr_wake_readiness_unrelated_failure_fails_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = self._readiness_wrapper_error(label="com.mikebook.merge-safe-prs-loop")
            with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                       side_effect=unrelated) as resolver, \
                    patch("jev_git_graph.coordinator.time.sleep") as sleep:
                with self.assertRaises(CreatorRuntimeValidationError) as caught:
                    resolve_production_creator_capability_when_ready(root)
            self.assertIs(unrelated, caught.exception)
            resolver.assert_called_once_with(root)
            sleep.assert_not_called()

    def test_pr_wake_readiness_wrong_stage_or_kind_fails_without_retry(self):
        label = "com.mikebook.pr-convergence-wake-consumer"
        cases = (
            CreatorRuntimeValidationError(
                "cwd mismatch", failure_stage="cwd_mismatch", label=label,
                observed_process_kind="reviewed_wrapper"),
            CreatorRuntimeValidationError(
                "not reviewed wrapper", failure_stage="process_image_mismatch",
                label=label, observed_process_kind="other"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for failure in cases:
                with self.subTest(diagnostic=failure.safe_diagnostic()):
                    with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                               side_effect=failure) as resolver, \
                            patch("jev_git_graph.coordinator.time.sleep") as sleep:
                        with self.assertRaises(CreatorRuntimeValidationError) as caught:
                            resolve_production_creator_capability_when_ready(root)
                    self.assertIs(failure, caught.exception)
                    resolver.assert_called_once_with(root)
                    sleep.assert_not_called()

    def test_pr_wake_readiness_rejects_changed_process_or_hook_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self._readiness_wrapper_error()
            original._readiness_hook_context = "hooks-one"
            changed_pid = self._readiness_wrapper_error(pid=7313)
            changed_pid._readiness_hook_context = "hooks-one"
            with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                       side_effect=[original, changed_pid]) as resolver, \
                    patch("jev_git_graph.coordinator.time.sleep") as sleep:
                with self.assertRaises(CreatorRuntimeValidationError) as caught:
                    resolve_production_creator_capability_when_ready(root)
            self.assertIs(changed_pid, caught.exception)
            self.assertEqual(2, resolver.call_count)
            sleep.assert_called_once_with(0.2)

            original = self._readiness_wrapper_error()
            original._readiness_hook_context = "hooks-one"
            changed_hooks = self._readiness_wrapper_error()
            changed_hooks._readiness_hook_context = "hooks-two"
            with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                       side_effect=[original, changed_hooks]) as resolver:
                with self.assertRaises(CreatorRuntimeValidationError) as caught:
                    resolve_production_creator_capability_when_ready(root)
            self.assertIs(changed_hooks, caught.exception)
            self.assertEqual(2, resolver.call_count)

            for changed in (
                    self._readiness_wrapper_error(start="start-two"),
                    self._readiness_wrapper_error(root="/other", cwd="/other")):
                original = self._readiness_wrapper_error()
                original._readiness_hook_context = "hooks-one"
                changed._readiness_hook_context = "hooks-one"
                with self.subTest(changed=changed.safe_diagnostic()):
                    with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                               side_effect=[original, changed]) as resolver:
                        with self.assertRaises(CreatorRuntimeValidationError) as caught:
                            resolve_production_creator_capability_when_ready(root)
                    self.assertIs(changed, caught.exception)
                    self.assertEqual(2, resolver.call_count)

            original = self._readiness_wrapper_error()
            original._readiness_hook_context = "hooks-one"
            terminal = self._readiness_capability(root)
            different_process = {"com.mikebook.pr-convergence-wake-consumer": {
                "kind": "reviewed_daemon", "pid": 7313,
                "start_sha256": hashlib.sha256(b"start-one").hexdigest(),
                "root_sha256": hashlib.sha256(b"/runtime").hexdigest(),
                "cwd_sha256": hashlib.sha256(b"/runtime").hexdigest(),
            }}
            with patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                       side_effect=[original, (terminal, different_process, "hooks-one")]):
                with self.assertRaises(CreatorRuntimeValidationError) as caught:
                    resolve_production_creator_capability_when_ready(root)
            self.assertEqual("other", caught.exception.safe_diagnostic()["failure_stage"])

    def test_post_intent_wrapper_readiness_keeps_generation_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            label = "com.mikebook.pr-convergence-wake-consumer"
            expected = self._readiness_capability(root)
            current = replace(expected, loaded_runtime_jobs=((
                label, str(root / "consumer.plist"), str(root / "stable"), "b" * 64),))
            wrapper = self._readiness_wrapper_error()
            wrapper._readiness_hook_context = "hooks-one"
            observations = {label: {
                "kind": "reviewed_daemon", "pid": 7312,
                "start_sha256": hashlib.sha256(b"start-one").hexdigest(),
                "root_sha256": hashlib.sha256(b"/runtime").hexdigest(),
                "cwd_sha256": hashlib.sha256(b"/runtime").hexdigest(),
            }}
            with patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                       side_effect=wrapper), \
                    patch("jev_git_graph.coordinator._resolve_production_creator_capability_snapshot",
                          return_value=(current, observations, "hooks-one")), \
                    patch("jev_git_graph.coordinator.time.sleep"):
                result = production_capability_diagnostic(expected, root)
            self.assertFalse(result["ok"])
            self.assertEqual(["loaded_job_generation"], result["changed_fields"])
            self.assertEqual(1, len(result["current_generation_hashes"]))

    def test_cli_plan_keeps_private_recovery_bundle_beside_manifest(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{
            "name": "topic", "tip": topic_tip, "main_tip": main_tip,
            "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
            "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}],
        }])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage_path = root / "coverage.json"
            coverage_path.write_text(json.dumps(coverage))
            with patch("jev_git_graph.cleanup._activity", return_value=1):
                plan_path = write_cleanup_plan(repo, coverage_path, root / "plan")
            plan = json.loads(plan_path.read_text())
            bundle = Path(plan["bundle"]["path"])
            self.assertEqual(bundle, (root / "plan/recovery/cleanup.bundle").resolve())
            self.assertEqual(plan["bundle"]["restoration_verified"], True)
            self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
            self.assertEqual(hashlib.sha256(bundle.read_bytes()).hexdigest(),
                             plan["bundle"]["sha256"])
            with patch("jev_git_graph.cleanup._activity", return_value=1):
                with self.assertRaisesRegex(JgError, "bundle already exists"):
                    write_cleanup_plan(repo, coverage_path, root / "plan")

    def test_idle_runtime_job_requires_matching_loaded_command(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            runtime = home / "code/home-lab"
            (runtime / "launchd").mkdir(parents=True)
            (runtime / "scripts").mkdir()
            launcher = runtime / "launchd/start.sh"
            process = runtime / "scripts/entry.py"
            launcher.write_text("#!/bin/sh\n")
            process.write_text("pass\n")
            plist_path = home / "Library/LaunchAgents/com.example.creator.plist"
            plist_path.parent.mkdir(parents=True)
            plist_path.write_bytes(plistlib.dumps({
                "Label": "com.example.creator",
                "ProgramArguments": ["/bin/sh", str(launcher)],
            }))
            launchctl_result = subprocess.CompletedProcess(
                ["launchctl"], 0, stdout=(f"path = {plist_path}\nstate = not running\n"
                    f"arguments = {{\n    /bin/sh\n    {launcher}\n}}\n"), stderr="",
            )
            with patch("jev_git_graph.coordinator._RUNTIME_SELECTORS", (
                    "code/.runtime/releases/home-lab/stable", "code/.runtime/home-lab", "code/home-lab")), \
                    patch("jev_git_graph.coordinator._REQUIRED_LAUNCHD_SELECTORS", {
                        "com.example.creator": "code/home-lab"}), \
                    patch("jev_git_graph.coordinator._REQUIRED_LAUNCHD_PATHS", {
                        "com.example.creator": ("launchd/start.sh", "scripts/entry.py", "runtime")}), \
                    patch("jev_git_graph.coordinator.subprocess.run", return_value=launchctl_result):
                jobs = _verify_loaded_runtime_jobs(home, (runtime, runtime, runtime))
                self.assertEqual(len(jobs), 1)
                self.assertEqual(len(jobs[0][3]), 64)
                launchctl_result.stdout = (f"path = {plist_path}\nstate = not running\n"
                    "arguments = {\n    /bin/sh\n    /tmp/unreviewed.sh\n}\n")
                with self.assertRaisesRegex(JgError, "loaded creator command differs"):
                    _verify_loaded_runtime_jobs(home, (runtime, runtime, runtime))
                unavailable = subprocess.CompletedProcess(["launchctl"], 113, stdout="", stderr="not found")
                disabled = subprocess.CompletedProcess(
                    ["launchctl"], 0,
                    stdout='"com.example.creator" => disabled\n', stderr="")
                with patch("jev_git_graph.coordinator.subprocess.run",
                           side_effect=lambda command, **_kwargs: (
                               disabled if command[1] == "print-disabled" else unavailable)):
                    self.assertEqual(len(_verify_loaded_runtime_jobs(
                        home, (runtime, runtime, runtime))), 1)
                    disabled.stdout = '"com.example.creator" => enabled\n'
                    with self.assertRaisesRegex(JgError, "unavailable and not disabled"):
                        _verify_loaded_runtime_jobs(home, (runtime, runtime, runtime))

    def test_running_creator_failures_have_hashed_diagnostics_and_wrapper_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            runtime = (home / "code/home-lab").resolve(strict=False)
            launcher = runtime / "launchd/start.sh"
            daemon = runtime / "scripts/entry.py"
            launcher.parent.mkdir(parents=True)
            daemon.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/bash\nexit 0\n")
            daemon.write_text("pass\n")
            label = "com.mikebook.pr-convergence-wake-consumer"
            plist_path = home / f"Library/LaunchAgents/{label}.plist"
            plist_path.parent.mkdir(parents=True)
            arguments = ["/bin/bash", str(launcher)]
            plist_path.write_bytes(plistlib.dumps({
                "Label": label,
                "ProgramArguments": arguments,
                "WorkingDirectory": str(runtime),
            }))
            launchctl = subprocess.CompletedProcess(
                ["launchctl"], 0,
                stdout=(f"path = {plist_path}\nstate = running\n"
                        f"arguments = {{\n    /bin/bash\n    {launcher}\n}}\n"
                        f"working directory = {runtime}\n"
                        "pid = 4812\n"),
                stderr="",
            )
            start_text = "Thu Sep 24 21:28:29 2026"
            command_text = f"/bin/bash {launcher}"
            cwd_text = str(runtime)

            def capture(fault=None, command=command_text, cwd=cwd_text):
                calls = []

                def fake_run(argv, **_kwargs):
                    calls.append(tuple(argv))
                    if argv[0] == "launchctl":
                        output = launchctl.stdout
                        if fault == "pid_lookup_failed":
                            output = output.replace("pid = 4812", "pid = invalid")
                        if fault == "loaded_args_missing":
                            output = output.replace(
                                f"arguments = {{\n    /bin/bash\n    {launcher}\n}}\n", "")
                        if fault == "loaded_command_mismatch":
                            output = output.replace(str(launcher), str(base / "unreviewed.sh"))
                        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
                    if argv[0] == "ps" and argv[-1] == "command=":
                        rc = 1 if fault == "process_lookup_failed" else 0
                        return subprocess.CompletedProcess(argv, rc,
                                                           stdout=command if rc == 0 else "",
                                                           stderr="PRIVATE_ERROR_MARKER")
                    if argv[0] == "ps" and argv[-1] == "lstart=":
                        rc = 1 if fault == "start_lookup_failed" else 0
                        text = "invalid start" if fault == "start_parse_failed" else start_text
                        return subprocess.CompletedProcess(argv, rc,
                                                           stdout=text if rc == 0 else "",
                                                           stderr="PRIVATE_ERROR_MARKER")
                    if argv[0] == "lsof":
                        rc = 1 if fault == "cwd_lookup_failed" else 0
                        return subprocess.CompletedProcess(
                            argv, rc, stdout=f"p4812\nfcwd\nn{cwd}\n" if rc == 0 else "",
                            stderr="PRIVATE_ERROR_MARKER",
                        )
                    raise AssertionError(argv)

                with patch("jev_git_graph.coordinator._RUNTIME_SELECTORS", (
                        "code/home-lab", "code/home-lab", "code/home-lab")), \
                        patch("jev_git_graph.coordinator._REQUIRED_LAUNCHD_SELECTORS", {
                            label: "code/home-lab"}), \
                        patch("jev_git_graph.coordinator._REQUIRED_LAUNCHD_PATHS", {
                            label: ("launchd/start.sh", "scripts/entry.py", "runtime")}), \
                        patch("jev_git_graph.coordinator.subprocess.run", side_effect=fake_run):
                    with self.assertRaises(CreatorRuntimeValidationError) as caught:
                        _verify_loaded_runtime_jobs(home, (runtime, runtime, runtime))
                return caught.exception, calls

            expected_root_hash = hashlib.sha256(str(runtime.resolve(strict=False)).encode()).hexdigest()
            cases = (
                ("pid_lookup_failed", "pid_lookup_failed"),
                ("loaded_args_missing", "process_lookup_failed"),
                ("process_lookup_failed", "process_lookup_failed"),
                ("start_lookup_failed", "start_lookup_failed"),
                ("cwd_lookup_failed", "cwd_lookup_failed"),
                ("cwd_mismatch", "cwd_mismatch"),
                ("start_parse_failed", "start_lookup_failed"),
                ("reviewed_wrapper", "process_image_mismatch"),
                ("loaded_command_mismatch", "loaded_job_contract_mismatch"),
                ("wrong_process", "process_image_mismatch"),
            )
            for fault, stage in cases:
                with self.subTest(fault=fault):
                    if fault == "cwd_mismatch":
                        wrong_root = (base / "wrong-root").resolve(strict=False)
                        error, calls = capture(fault, cwd=str(wrong_root))
                    elif fault == "wrong_process":
                        error, calls = capture(fault, command=f"/bin/bash {base / 'unreviewed.sh'}")
                    elif fault == "start_parse_failed":
                        error, calls = capture(fault, command=f"/usr/bin/python3 {daemon}")
                    else:
                        error, calls = capture(fault)
                    diagnostic = error.safe_diagnostic()
                    self.assertEqual(stage, diagnostic["failure_stage"])
                    self.assertEqual(label, diagnostic["failed_label"])
                    if fault == "pid_lookup_failed":
                        self.assertNotIn("failed_pid", diagnostic)
                    else:
                        self.assertEqual(4812, diagnostic["failed_pid"])
                    self.assertEqual(expected_root_hash,
                                     diagnostic["expected_runtime_root_sha256"])
                    self.assertNotIn(str(runtime), json.dumps(diagnostic))
                    self.assertNotIn("PRIVATE_ERROR_MARKER", json.dumps(diagnostic))
                    self.assertEqual(1, sum(call[0] == "launchctl" for call in calls))
                    if fault == "reviewed_wrapper":
                        self.assertEqual("reviewed_wrapper", diagnostic["observed_process_kind"])
                        self.assertEqual(hashlib.sha256(command_text.encode()).hexdigest(),
                                         diagnostic["process_command_sha256"])
                        self.assertEqual(hashlib.sha256(cwd_text.encode()).hexdigest(),
                                         diagnostic["process_cwd_sha256"])
                        self.assertEqual(hashlib.sha256(start_text.encode()).hexdigest(),
                                         diagnostic["process_start_sha256"])
                    elif fault == "wrong_process":
                        self.assertEqual("other", diagnostic["observed_process_kind"])
                    elif fault == "cwd_mismatch":
                        self.assertEqual(hashlib.sha256(str(wrong_root).encode()).hexdigest(),
                                         diagnostic["observed_runtime_root_sha256"])
                    elif fault == "loaded_command_mismatch":
                        self.assertEqual(["launchctl"], [call[0] for call in calls])
                        for field in (
                            "observed_runtime_root_sha256", "process_command_sha256",
                            "process_cwd_sha256", "process_start_sha256",
                            "observed_process_kind",
                        ):
                            self.assertNotIn(field, diagnostic)
                    with patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                               side_effect=error) as resolve:
                        safe = production_capability_diagnostic(
                            CreatorLeaseCapability(
                                common_dir=base, runtime_roots=(runtime, runtime, runtime),
                                hook_digests=(), loaded_runtime_jobs=((label, "plist", str(runtime), "a" * 64),),
                                lock_root=base / "locks", train_construction_runtime=runtime,
                                train_construction_commit=None,
                            ), base,
                        )
                    self.assertEqual(1, resolve.call_count)
                    self.assertEqual(stage, safe["failure_stage"])
                    self.assertEqual(diagnostic, {key: value for key, value in safe.items()
                                                  if key in diagnostic})
                    self.assertNotIn(str(runtime), json.dumps(safe))

    def test_creator_generation_is_bound_to_pid_and_reviewed_hook_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            helper = runtime / "scripts/cooperative_branch_lease.py"
            helper.parent.mkdir()
            contents = 'CONTRACT = "jev-git-graph/cooperative-branch-lease-v1"\n'
            helper.write_text(contents)
            os.utime(helper, (1_000, 1_000))
            with patch("jev_git_graph.coordinator._REVIEWED_HOOK_FILES", {
                    "scripts/cooperative_branch_lease.py": hashlib.sha256(contents.encode()).hexdigest()}):
                first_generation = _attest_process_generation(runtime, 123, 999)
                same_generation = _attest_process_generation(runtime, 123, 999)
                restarted_generation = _attest_process_generation(runtime, 456, 999)
                self.assertEqual(first_generation, same_generation)
                self.assertNotEqual(first_generation, restarted_generation)

    def test_startup_attestation_requires_exact_process_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir()
            run(runtime, "init", "-q", "-b", "main")
            run(runtime, "config", "user.name", "Fixture")
            run(runtime, "config", "user.email", "fixture@example.invalid")
            source = runtime / "scripts/entry.py"
            helper = runtime / "scripts/cooperative_branch_lease.py"
            shell = runtime / "launchd/start.sh"
            source.parent.mkdir()
            shell.parent.mkdir()
            source.write_text("pass\n")
            helper.write_text('CONTRACT = "jev-git-graph/cooperative-branch-lease-v1"\n')
            shell.write_text("#!/bin/bash\nexit 0\n")
            run(runtime, "add", ".")
            run(runtime, "commit", "-qm", "reviewed runtime")
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            helper_sha = hashlib.sha256(helper.read_bytes()).hexdigest()
            shell_sha = hashlib.sha256(shell.read_bytes()).hexdigest()
            label = "com.example.creator"
            process_start = "Wed Sep 24 08:00:00 2026"
            home = root / "home"
            directory_path = home / ".local/state/jev-git-graph/creator-runtime-attestations"
            directory_path.mkdir(parents=True)
            os.chmod(directory_path, 0o700)
            pid = os.getpid()
            receipt = {
                "contract": "jev-git-graph/creator-runtime-attestation-v1",
                "pid": pid,
                "process_start": process_start,
                "runtime_root_sha256": hashlib.sha256(str(runtime.resolve()).encode()).hexdigest(),
                "runtime_commit": run(runtime, "rev-parse", "HEAD"),
                "creator": label,
                "attestations": [{
                    "creator": label,
                    "kind": "python_code",
                    "source_path": "scripts/entry.py",
                    "source_sha256": source_sha,
                    "loaded_code_sha256": "a" * 64,
                    "helper_path": "scripts/cooperative_branch_lease.py",
                    "helper_sha256": helper_sha,
                }],
            }
            path = directory_path / f"{pid}.json"
            path.write_text(json.dumps(receipt))
            os.chmod(path, 0o600)
            with patch("jev_git_graph.coordinator._REVIEWED_HOOK_FILES", {
                    "scripts/cooperative_branch_lease.py": helper_sha,
                    "scripts/entry.py": source_sha,
                    "launchd/start.sh": shell_sha}):
                self.assertEqual(_verify_process_startup_attestation(
                    home, runtime, pid, process_start, label, "scripts/entry.py"),
                    digest(receipt))
                with self.assertRaises(CreatorRuntimeValidationError) as shell_error:
                    _verify_process_startup_attestation(
                        home, runtime, pid, process_start, label, "launchd/start.sh")
                self.assertEqual("attestation_source_mismatch",
                                 shell_error.exception.safe_diagnostic()["failure_stage"])
                with self.assertRaises(CreatorRuntimeValidationError) as generation_error:
                    _verify_process_startup_attestation(
                        home, runtime, pid, process_start + " stale", label, "scripts/entry.py")
                self.assertEqual("attestation_generation_mismatch",
                                 generation_error.exception.safe_diagnostic()["failure_stage"])
                path.unlink()
                with self.assertRaises(CreatorRuntimeValidationError) as missing_error:
                    _verify_process_startup_attestation(
                        home, runtime, pid, process_start, label, "scripts/entry.py")
                self.assertEqual("attestation_missing",
                                 missing_error.exception.safe_diagnostic()["failure_stage"])
                tampered = dict(receipt)
                tampered["attestations"] = [dict(receipt["attestations"][0], source_sha256="0" * 64)]
                path.write_text(json.dumps(tampered))
                os.chmod(path, 0o600)
                with self.assertRaises(CreatorRuntimeValidationError) as source_error:
                    _verify_process_startup_attestation(
                        home, runtime, pid, process_start, label, "scripts/entry.py")
                self.assertEqual("attestation_source_mismatch",
                                 source_error.exception.safe_diagnostic()["failure_stage"])
                path.write_text(json.dumps(receipt))
                with patch("jev_git_graph.coordinator._SUPERVISED_SHELL_SOURCES", {
                        label: ("launchd/start.sh",)}):
                    with self.assertRaisesRegex(JgError, "reviewed shell descriptor missing"):
                        _verify_process_startup_attestation(
                            home, runtime, pid, process_start, label, "scripts/entry.py")
                    receipt["attestations"].append({
                        "creator": label, "kind": "shell_fd",
                        "source_path": "launchd/start.sh", "source_sha256": shell_sha,
                        "loaded_code_sha256": shell_sha,
                        "helper_path": "scripts/cooperative_branch_lease.py",
                        "helper_sha256": helper_sha,
                    })
                    path.write_text(json.dumps(receipt))
                    self.assertEqual(_verify_process_startup_attestation(
                        home, runtime, pid, process_start, label, "scripts/entry.py",
                        {"launchd/start.sh": shell_sha}), digest(receipt))
                    merge_loop_shell = runtime / "scripts/merge-safe-prs-loop.sh"
                    original_merge_loop_shell = b"#!/bin/bash\n# original test bytes\nexit 0\n"
                    original_merge_loop_sha = hashlib.sha256(original_merge_loop_shell).hexdigest()
                    merge_loop_shell.write_bytes(b"#!/bin/bash\n# compatible test bytes\nexit 0\n")
                    merge_loop_sha = hashlib.sha256(merge_loop_shell.read_bytes()).hexdigest()
                    merge_loop_rel = "scripts/merge-safe-prs-loop.sh"
                    merge_loop_receipt = {
                        "creator": label, "kind": "shell_fd",
                        "source_path": merge_loop_rel,
                        "source_sha256": merge_loop_sha,
                        "loaded_code_sha256": merge_loop_sha,
                        "helper_path": "scripts/cooperative_branch_lease.py",
                        "helper_sha256": helper_sha,
                    }
                    receipt["attestations"].append(merge_loop_receipt)
                    path.write_text(json.dumps(receipt))
                    reviewed_files = {
                        "scripts/cooperative_branch_lease.py": helper_sha,
                        "scripts/entry.py": source_sha,
                        "launchd/start.sh": shell_sha,
                        merge_loop_rel: original_merge_loop_sha,
                    }
                    with patch("jev_git_graph.coordinator._REVIEWED_HOOK_FILES", reviewed_files), \
                            patch("jev_git_graph.coordinator._SUPERVISED_SHELL_SOURCES", {
                                label: (merge_loop_rel,)}), \
                            patch("jev_git_graph.coordinator._REVIEWED_MERGE_LOOP_SHELL_RUNTIME_COMPAT_SHA",
                                  merge_loop_sha):
                        self.assertEqual(_verify_process_startup_attestation(
                            home, runtime, pid, process_start, label, "scripts/entry.py",
                            {merge_loop_rel: merge_loop_sha}), digest(receipt))
                        merge_loop_receipt["source_sha256"] = original_merge_loop_sha
                        merge_loop_receipt["loaded_code_sha256"] = original_merge_loop_sha
                        path.write_text(json.dumps(receipt))
                        with self.assertRaisesRegex(JgError, "reviewed shell descriptor missing"):
                            _verify_process_startup_attestation(
                                home, runtime, pid, process_start, label, "scripts/entry.py",
                                {merge_loop_rel: merge_loop_sha})
                        merge_loop_receipt["source_sha256"] = merge_loop_sha
                        merge_loop_receipt["loaded_code_sha256"] = merge_loop_sha
                        path.write_text(json.dumps(receipt))
                        merge_loop_shell.write_bytes(original_merge_loop_shell)
                        with self.assertRaisesRegex(JgError, "reviewed shell descriptor missing"):
                            _verify_process_startup_attestation(
                                home, runtime, pid, process_start, label, "scripts/entry.py",
                                {merge_loop_rel: "0" * 64})

    def test_unreviewed_creator_hook_digest_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            helper = runtime / "scripts/cooperative_branch_lease.py"
            helper.parent.mkdir()
            helper.write_text('CONTRACT = "jev-git-graph/cooperative-branch-lease-v1"\n')
            with patch("jev_git_graph.coordinator._REVIEWED_HOOK_FILES", {
                    "scripts/cooperative_branch_lease.py": "0" * 64}):
                with self.assertRaisesRegex(JgError, "creator runtime hook digest mismatch"):
                    _verify_runtime_hook_files(runtime)

    def test_deploy_sync_runtime_digest_accepts_only_reviewed_variants(self):
        reviewed = _REVIEWED_HOOK_FILES["scripts/deploy_sync.py"]
        compatible = _REVIEWED_DEPLOY_SYNC_RUNTIME_COMPAT_SHA
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            "scripts/deploy_sync.py", reviewed, reviewed))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            "scripts/deploy_sync.py", reviewed, compatible))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            "scripts/deploy_sync.py", reviewed,
            _REVIEWED_DEPLOY_SYNC_RUNTIME_COMPAT_7227_SHA))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            "scripts/deploy_sync.py", reviewed,
            _REVIEWED_DEPLOY_SYNC_RUNTIME_8607_SHA))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            "scripts/deploy_sync.py", reviewed,
            _REVIEWED_DEPLOY_SYNC_RUNTIME_8617_SHA))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            "scripts/deploy_sync.py", reviewed, "0" * 64))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            "scripts/cooperative_branch_lease.py", "1" * 64, compatible))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            "scripts/cooperative_branch_lease.py", "1" * 64,
            _REVIEWED_DEPLOY_SYNC_RUNTIME_COMPAT_7227_SHA))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            "scripts/cooperative_branch_lease.py", "1" * 64,
            _REVIEWED_DEPLOY_SYNC_RUNTIME_8607_SHA))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            "scripts/cooperative_branch_lease.py", "1" * 64,
            _REVIEWED_DEPLOY_SYNC_RUNTIME_8617_SHA))

    def test_pr8603_runtime_hook_variants_are_exact_and_path_bound(self):
        helper = "scripts/cooperative_branch_lease.py"
        candidate = "scripts/merge_train_parts/candidate_lifecycle.py"
        verdict = "scripts/merge_train_parts/verdict_lifecycle.py"
        unknown = "0" * 64

        self.assertTrue(_is_reviewed_runtime_hook_digest(
            helper, _REVIEWED_HOOK_FILES[helper], _REVIEWED_HOOK_FILES[helper]))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            helper, _REVIEWED_HOOK_FILES[helper],
            _REVIEWED_COOPERATIVE_BRANCH_LEASE_RUNTIME_8603_SHA))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            helper, _REVIEWED_HOOK_FILES[helper], unknown))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            candidate, _REVIEWED_HOOK_FILES[candidate],
            _REVIEWED_COOPERATIVE_BRANCH_LEASE_RUNTIME_8603_SHA))

        self.assertTrue(_is_reviewed_runtime_hook_digest(
            candidate, _REVIEWED_HOOK_FILES[candidate],
            _REVIEWED_HOOK_FILES[candidate]))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            candidate, _REVIEWED_HOOK_FILES[candidate],
            _REVIEWED_CANDIDATE_LIFECYCLE_RUNTIME_FF1EF33_SHA))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            candidate, _REVIEWED_HOOK_FILES[candidate],
            "b798333fe2373716c80520ec37d9349e98e6dcc09147b808058addfab707d6f7"))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            candidate, _REVIEWED_HOOK_FILES[candidate], unknown))

        self.assertTrue(_is_reviewed_runtime_hook_digest(
            verdict, _REVIEWED_HOOK_FILES[verdict],
            _REVIEWED_HOOK_FILES[verdict]))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            verdict, _REVIEWED_HOOK_FILES[verdict],
            _REVIEWED_VERDICT_LIFECYCLE_RUNTIME_7A305FF_SHA))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            verdict, _REVIEWED_HOOK_FILES[verdict],
            _REVIEWED_VERDICT_LIFECYCLE_RUNTIME_COMPAT_SHA))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            verdict, _REVIEWED_HOOK_FILES[verdict],
            _REVIEWED_VERDICT_LIFECYCLE_RUNTIME_EB54_SHA))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            verdict, _REVIEWED_HOOK_FILES[verdict], unknown))

    def test_deploy_sync_hash_change_remains_a_capability_change(self):
        root = Path("/fixture/home-lab")
        roots = (root / "stable", root / "compat", root / "canonical")
        relative = "scripts/deploy_sync.py"
        original_sha = _REVIEWED_HOOK_FILES[relative]
        expected = CreatorLeaseCapability(
            common_dir=root / ".git", runtime_roots=roots, hook_digests=tuple(
                (str(runtime_root), relative, original_sha) for runtime_root in roots
            ), loaded_runtime_jobs=(), lock_root=root / "locks",
            train_construction_runtime=root / "train-runtime",
            train_construction_commit=None,
        )
        for compatible_sha in (
            _REVIEWED_DEPLOY_SYNC_RUNTIME_COMPAT_SHA,
            _REVIEWED_DEPLOY_SYNC_RUNTIME_COMPAT_7227_SHA,
        ):
            with self.subTest(compatible_sha=compatible_sha):
                current_hooks = tuple(
                    (str(runtime_root), relative,
                     compatible_sha if runtime_root == roots[0] else original_sha)
                    for runtime_root in roots
                )
                current = replace(expected, hook_digests=current_hooks)
                with patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                           return_value=current):
                    diagnostic = production_capability_diagnostic(expected, root)
                self.assertFalse(diagnostic["ok"])
                self.assertEqual(["hook_digests"], diagnostic["changed_fields"])
                self.assertEqual({"added_count": 1, "removed_count": 1},
                                 diagnostic["hook_changes"])

                self.assertIn((str(roots[0]), relative, compatible_sha), current.hook_digests)
                self.assertIn((str(roots[1]), relative, original_sha), current.hook_digests)
                self.assertIn((str(roots[2]), relative, original_sha), current.hook_digests)
                self.assertNotEqual(
                    capability_receipt_metadata(expected)["creator_capability_sha256"],
                    capability_receipt_metadata(current)["creator_capability_sha256"],
                )

    def test_exact_reviewed_merge_loop_and_verdict_variants_are_path_bound(self):
        shell = "scripts/merge-safe-prs-loop.sh"
        lifecycle = "scripts/merge_train_parts/candidate_lifecycle.py"
        verdict_lifecycle = "scripts/merge_train_parts/verdict_lifecycle.py"
        shell_variant = "2ec5e69c594d813624161ccdffe4a92bd6ed184999f7469b3f8a219e67563086"
        lifecycle_variant = "b798333fe2373716c80520ec37d9349e98e6dcc09147b808058addfab707d6f7"
        verdict_variant = _REVIEWED_VERDICT_LIFECYCLE_RUNTIME_COMPAT_SHA
        verdict_eb54_variant = _REVIEWED_VERDICT_LIFECYCLE_RUNTIME_EB54_SHA
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            shell, _REVIEWED_HOOK_FILES[shell], _REVIEWED_HOOK_FILES[shell]))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            shell, _REVIEWED_HOOK_FILES[shell], shell_variant))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            lifecycle, _REVIEWED_HOOK_FILES[lifecycle], lifecycle_variant))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            verdict_lifecycle, _REVIEWED_HOOK_FILES[verdict_lifecycle],
            _REVIEWED_HOOK_FILES[verdict_lifecycle]))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            verdict_lifecycle, _REVIEWED_HOOK_FILES[verdict_lifecycle], verdict_variant))
        self.assertTrue(_is_reviewed_runtime_hook_digest(
            verdict_lifecycle, _REVIEWED_HOOK_FILES[verdict_lifecycle], verdict_eb54_variant))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            shell, _REVIEWED_HOOK_FILES[shell], lifecycle_variant))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            lifecycle, _REVIEWED_HOOK_FILES[lifecycle], shell_variant))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            verdict_lifecycle, _REVIEWED_HOOK_FILES[verdict_lifecycle], lifecycle_variant))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            lifecycle, _REVIEWED_HOOK_FILES[lifecycle], verdict_variant))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            lifecycle, _REVIEWED_HOOK_FILES[lifecycle], verdict_eb54_variant))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            verdict_lifecycle, _REVIEWED_HOOK_FILES[verdict_lifecycle], "0" * 64))
        self.assertFalse(_is_reviewed_runtime_hook_digest(
            shell, _REVIEWED_HOOK_FILES[shell], "0" * 64))

    def test_merge_loop_and_verdict_runtime_hash_changes_remain_capability_changes(self):
        root = Path("/fixture/home-lab")
        roots = (root / "stable", root / "compat", root / "canonical")
        variants = {
            "scripts/merge-safe-prs-loop.sh": (
                "2ec5e69c594d813624161ccdffe4a92bd6ed184999f7469b3f8a219e67563086"),
            "scripts/merge_train_parts/candidate_lifecycle.py": (
                "b798333fe2373716c80520ec37d9349e98e6dcc09147b808058addfab707d6f7"),
            "scripts/merge_train_parts/verdict_lifecycle.py": (
                (_REVIEWED_VERDICT_LIFECYCLE_RUNTIME_COMPAT_SHA,
                 _REVIEWED_VERDICT_LIFECYCLE_RUNTIME_EB54_SHA)),
        }
        for relative, compatible_shas in variants.items():
            for compatible_sha in (compatible_shas if isinstance(compatible_shas, tuple)
                                   else (compatible_shas,)):
                with self.subTest(relative=relative, compatible_sha=compatible_sha):
                    original_sha = _REVIEWED_HOOK_FILES[relative]
                    expected = CreatorLeaseCapability(
                        common_dir=root / ".git", runtime_roots=roots, hook_digests=tuple(
                            (str(runtime_root), relative, original_sha) for runtime_root in roots
                        ), loaded_runtime_jobs=(), lock_root=root / "locks",
                        train_construction_runtime=root / "train-runtime",
                        train_construction_commit=None,
                    )
                    changed = replace(expected, hook_digests=tuple(
                        (str(runtime_root), relative,
                         compatible_sha if runtime_root == roots[0] else original_sha)
                        for runtime_root in roots
                    ))
                    with patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                               return_value=changed):
                        diagnostic = production_capability_diagnostic(expected, root)
                    self.assertFalse(diagnostic["ok"])
                    self.assertEqual(["hook_digests"], diagnostic["changed_fields"])
                    self.assertNotEqual(
                        capability_receipt_metadata(expected)["creator_capability_sha256"],
                        capability_receipt_metadata(changed)["creator_capability_sha256"],
                    )

    def test_train_construction_requires_live_clean_origin_main_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            run(repository, "init", "-q", "-b", "main")
            run(repository, "config", "user.name", "Fixture")
            run(repository, "config", "user.email", "fixture@example.invalid")
            contents = {
                "scripts/cooperative_branch_lease.py": (
                    'CONTRACT = "jev-git-graph/cooperative-branch-lease-v1"\n'),
                "scripts/train_builder.py": "# pinned train builder\n",
                "scripts/train_construction_driver.py": "# pinned driver\n",
                "launchd/start-train-construction.sh": "#!/bin/sh\nexit 0\n",
            }
            expected = {}
            for relative, content in contents.items():
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                expected[relative] = hashlib.sha256(content.encode()).hexdigest()
            run(repository, "add", ".")
            run(repository, "commit", "-qm", "reviewed creator snapshot")
            run(repository, "update-ref", "refs/remotes/origin/main", "HEAD")

            home = root / "home"
            runtime = home / ".local/share/home-lab/train-promotion-runtime"
            runtime.parent.mkdir(parents=True)
            absent_root, absent_commit, absent_records = _verify_train_construction_runtime(
                home, _common_dir(repository))
            self.assertEqual(absent_root, runtime.resolve(strict=False))
            self.assertIsNone(absent_commit)
            self.assertEqual(absent_records, ())
            run(repository, "worktree", "add", "--detach", str(runtime), "HEAD")
            with patch("jev_git_graph.coordinator._REVIEWED_HOOK_FILES", expected):
                verified_root, verified_commit, records = _verify_train_construction_runtime(
                    home, _common_dir(repository))
                self.assertEqual(runtime.resolve(), verified_root)
                self.assertEqual(run(repository, "rev-parse", "HEAD"), verified_commit)
                self.assertEqual(len(expected), len(records))

                (repository / "scripts/train_construction_driver.py").write_text("# changed driver\n")
                run(repository, "add", ".")
                run(repository, "commit", "-qm", "new origin main")
                run(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
                with self.assertRaisesRegex(JgError, "train-construction-runtime_snapshot_mismatch"):
                    _verify_train_construction_runtime(home, _common_dir(repository))

    def integrated_lease(self, repository: Path) -> CooperativeBranchLeaseAdapter:
        fixture_root = repository.parent
        inventory = build_disposable_fixture_inventory(
            repository, ["topic"], operator="cleanup fixture test", fixture_root=fixture_root)
        return CooperativeBranchLeaseAdapter.for_disposable_fixture(
            repository, inventory, ["topic"], fixture_root=fixture_root)

    def test_planner_rechecks_activity_after_coverage(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_cleanup_plan(repo, coverage, bundle_dir=Path(out))
        self.assertEqual([], plan["candidates"])
        self.assertEqual("recent_or_unverifiable_activity", plan["observed"][1]["reason"])

    def make_repo(self, *, old_commits: bool = False) -> tuple[Path, str, str]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repo = Path(directory.name) / "repo"
        repo.mkdir()
        run(repo, "init", "-q", "-b", "main")
        run(repo, "config", "user.name", "Fixture")
        run(repo, "config", "user.email", "fixture@example.invalid")
        commit_env = ({**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                       "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"} if old_commits else None)
        (repo / "base").write_text("base\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "base", env=commit_env)
        run(repo, "switch", "-qc", "topic")
        (repo / "topic").write_text("preserved\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "topic", env=commit_env)
        topic_tip = run(repo, "rev-parse", "HEAD")
        run(repo, "switch", "-q", "main")
        (repo / "topic").write_text("preserved\n")
        (repo / "main-only").write_text("main\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "land topic and advance main", env=commit_env)
        return repo, topic_tip, run(repo, "rev-parse", "main")

    def test_plan_selects_old_exact_and_restores_every_tip(self):
        repo, topic_tip, main_tip = self.make_repo()
        records = [
            {"name": "topic", "tip": topic_tip, "main_tip": main_tip,
             "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
             "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]},
            {"name": "recent", "tip": main_tip, "main_tip": main_tip,
             "verdict": "UNKNOWN", "reason": "recent_activity", "last_activity_epoch": 200,
             "paths": []},
            {"name": "unknown", "tip": main_tip, "main_tip": main_tip,
             "verdict": "UNKNOWN", "reason": "activity_unverifiable", "last_activity_epoch": None,
             "paths": []},
        ]
        run(repo, "branch", "recent", main_tip)
        run(repo, "branch", "unknown", main_tip)
        coverage = coverage_for(repo, records)
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out) / "bundle")
            self.assertEqual(["topic"], [item["name"] for item in plan["candidates"]])
            observed = {item["name"]: item for item in plan["observed"]}
            self.assertEqual("recent_activity", observed["recent"]["reason"])
            self.assertEqual("activity_unverifiable", observed["unknown"]["reason"])
            self.assertFalse(plan["manifest_approved"])
            self.assertTrue(plan["bundle"]["restoration_verified"])
            self.assertEqual(hashlib.sha256(Path(plan["bundle"]["path"]).read_bytes()).hexdigest(), plan["bundle"]["sha256"])

    def test_pinned_exact_coverage_is_reproved_against_advanced_live_main(self):
        repo, topic_tip, coverage_main_tip = self.make_repo()
        coverage = coverage_for(repo, [{
            "name": "topic", "tip": topic_tip, "main_tip": coverage_main_tip,
            "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
            "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}],
        }])
        (repo / "unrelated").write_text("new main work\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "advance main without changing topic")
        live_main_tip = run(repo, "rev-parse", "main")
        with tempfile.TemporaryDirectory() as directory:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(directory))
            self.assertEqual(["topic"], [item["name"] for item in plan["candidates"]])
            self.assertEqual(plan["coverage_main_tip"], coverage_main_tip)
            self.assertEqual(plan["main"]["tip"], live_main_tip)
            self.assertEqual(plan["bundle"]["tips"]["main"], live_main_tip)

        (repo / "topic").write_text("replaced on main\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "replace topic behavior")
        with tempfile.TemporaryDirectory() as directory:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(directory))
            self.assertEqual([], plan["candidates"])
            self.assertEqual("live_content_not_exact", plan["observed"][1]["reason"])

    def test_checked_out_branch_is_held(self):
        repo, topic_tip, main_tip = self.make_repo()
        worktree = repo.parent / "linked"
        run(repo, "worktree", "add", "-q", str(worktree), "topic")
        self.addCleanup(lambda: run(repo, "worktree", "remove", "-f", str(worktree)))
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
        self.assertEqual([], plan["candidates"])
        self.assertEqual("checked_out", plan["observed"][1]["reason"])

    def test_executor_requires_exact_approval_and_lease(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            with self.assertRaisesRegex(JgError, "digest"):
                execute_cleanup(repo, plan, approved_digest="wrong")
            with self.assertRaisesRegex(JgError, "manifest"):
                execute_cleanup(repo, plan, approved_digest=plan["plan_digest"])
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            result = execute_cleanup(repo, approved, approved_digest=approved["plan_digest"],
                                     lease_contract={"established": True,
                                                     "acquire": lambda *_: True,
                                                     "release": lambda *_: True})
        self.assertEqual("cooperative_lease_unestablished", result["stopped"])
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))

    def test_cli_fixture_execute_journals_and_reconcile_restores(self, capsys=None):
        repo, topic_tip, main_tip = self.make_repo(old_commits=True)
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            plan_path = root / "plan.json"
            fixture_root = repo.parent
            inventory_path = fixture_root / "fixture-inventory.json"
            journal_path = root / "cleanup.jsonl"
            plan_path.write_text(json.dumps(approved), encoding="utf-8")
            inventory = build_disposable_fixture_inventory(
                repo, ["topic"], operator="explicit disposable fixture", fixture_root=fixture_root)
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            original_append = CleanupActionJournal.append

            def interrupted_append(journal, event):
                if event.get("event") == "result":
                    raise JgError("simulated interruption after ref delete")
                return original_append(journal, event)

            with patch("jev_git_graph.cli.DISPOSABLE_FIXTURE_ROOT", fixture_root), \
                 patch.object(CleanupActionJournal, "append", interrupted_append):
                exit_code = jg_main([
                    "cleanup", "execute", "--repo", str(repo), "--plan", str(plan_path),
                    "--approved-digest", approved["plan_digest"], "--journal", str(journal_path),
                    "--fixture-inventory", str(inventory_path),
                ])
            self.assertEqual(2, exit_code)
            self.assertFalse(_ref_exists(repo, "topic"))
            intent = CleanupActionJournal(journal_path).pending_intents(plan_digest=approved["plan_digest"])
            self.assertEqual(1, len(intent))
            self.assertEqual("disposable_fixture", intent[0]["scope"])

            with patch("jev_git_graph.cli.DISPOSABLE_FIXTURE_ROOT", fixture_root):
                reconcile_exit = jg_main([
                    "cleanup", "reconcile", "--repo", str(repo), "--plan", str(plan_path),
                    "--journal", str(journal_path), "--fixture-inventory", str(inventory_path),
                ])
            self.assertEqual(0, reconcile_exit)
            self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))
            events = CleanupActionJournal(journal_path).read_events()
            self.assertEqual(["intent", "reconciled"], [event["event"] for event in events])
            self.assertEqual("source_restored_after_interruption", events[-1]["status"])
            self.assertEqual("disposable_fixture", events[-1]["scope"])

    def test_executor_journals_intent_and_result_while_holding_integrated_lease(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            journal_path = root / "actions.jsonl"
            lease = self.integrated_lease(repo)
            with patch("jev_git_graph.cleanup._live_reproof", return_value=None):
                result = execute_cleanup(
                    repo, approved, approved_digest=approved["plan_digest"],
                    lease_contract=lease, journal_path=journal_path,
                )
            events = CleanupActionJournal(journal_path).read_events()
        self.assertEqual(["topic"], [item["name"] for item in result["deleted"]])
        self.assertEqual(["intent", "result"], [event["event"] for event in events])
        self.assertEqual("deleted", events[-1]["status"])
        self.assertEqual(topic_tip, events[0]["tip"])
        self.assertEqual(main_tip, events[-1]["observed_destination_tip"])
        self.assertEqual(events[0]["creator_capability_sha256"],
                         events[-1]["creator_capability_sha256"])
        self.assertEqual(64, len(events[-1]["creator_capability_sha256"]))
        self.assertIsNone(events[-1]["creator_runtime_commit"])
        self.assertEqual(events[-1]["creator_capability_sha256"],
                         result["creator_capability_sha256"])
        self.assertFalse(_ref_exists(repo, "topic"))

    def test_capability_drift_after_branch_lock_blocks_ref_transaction(self):
        repo, topic_tip, main_tip = self.make_repo(old_commits=True)
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            lease = self.integrated_lease(repo)
            journal_path = root / "actions.jsonl"
            original = _lease_established
            calls = 0

            def drift_after_intent(contract, repository):
                nonlocal calls
                calls += 1
                return False if calls == 4 else original(contract, repository)

            with patch("jev_git_graph.cleanup._lease_established", drift_after_intent):
                result = execute_cleanup(
                    repo, approved, approved_digest=approved["plan_digest"],
                    lease_contract=lease, journal_path=journal_path,
                )
            events = CleanupActionJournal(journal_path).read_events()
        self.assertEqual("creator_capability_changed", result["stopped"])
        self.assertEqual(4, calls)
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))
        self.assertEqual(["intent", "result"], [event["event"] for event in events])

    def test_post_intent_capability_drift_is_recorded_from_exact_check(self):
        repo, topic_tip, main_tip = self.make_repo(old_commits=True)
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip,
                                        "main_tip": main_tip, "verdict": "EXACT",
                                        "reason": None, "last_activity_epoch": 1,
                                        "paths": [{"path": "topic",
                                                   "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            common_dir = _common_dir(repo)
            lease_root = root / "leases"
            resolved_lease_root = lease_root.resolve(strict=False)
            label = "com.mikebook.merge-safe-prs-loop"
            expected = CreatorLeaseCapability(
                common_dir=common_dir,
                runtime_roots=(root / "stable", root / "compat", root / "canonical"),
                hook_digests=((str(root / "stable"), "scripts/cooperative_branch_lease.py",
                               "1" * 64),),
                loaded_runtime_jobs=((label, str(root / "launchd.plist"),
                                      str(root / "stable"), "a" * 64),),
                lock_root=resolved_lease_root,
                train_construction_runtime=root / "train-runtime",
                train_construction_commit="b" * 40,
            )
            changed = replace(expected, loaded_runtime_jobs=(
                (label, str(root / "launchd.plist"), str(root / "stable"), "c" * 64),
            ))
            adapter = CooperativeBranchLeaseAdapter(
                str(common_dir), resolved_lease_root, capability=expected,
            )
            adapter.common_dir = common_dir
            journal_path = root / "actions.jsonl"

            # The fourth fresh resolution (post-intent) sees a new PID-bound
            # generation. A fifth resolution would return the original
            # capability, proving the recorded result must come from check 4.
            with patch("jev_git_graph.cleanup._fixed_lock_root",
                       return_value=resolved_lease_root), \
                    patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                          side_effect=[expected, expected, expected, changed, expected]) as resolve:
                result = execute_cleanup(
                    repo, approved, approved_digest=approved["plan_digest"],
                    lease_contract=adapter, journal_path=journal_path,
                )

            self.assertEqual(4, resolve.call_count, repr(result))
            self.assertEqual("creator_capability_changed", result["stopped"])
            self.assertFalse(result["destructive_action_authorized"])
            self.assertEqual(topic_tip, run(repo, "rev-parse", "refs/heads/topic"))
            diagnostic = result["capability_diagnostic"]
            self.assertEqual(["loaded_job_generation"], diagnostic["changed_fields"])
            self.assertEqual([{
                "label": label,
                "change": "generation_changed",
                "expected_generation_sha256": "a" * 64,
                "current_generation_sha256": "c" * 64,
            }], diagnostic["changed_jobs"])
            events = CleanupActionJournal(journal_path).read_events()
            self.assertEqual(["intent", "result"], [event["event"] for event in events])
            self.assertEqual(diagnostic, events[-1]["capability_diagnostic"])

    def test_capability_diagnostic_pass_and_failure_are_sanitized(self):
        root = Path("/safe/test-root")
        capability = CreatorLeaseCapability(
            common_dir=root / ".git",
            runtime_roots=(root / "stable", root / "compat", root / "canonical"),
            hook_digests=((str(root / "stable"), "scripts/cooperative_branch_lease.py",
                           "1" * 64),),
            loaded_runtime_jobs=(("com.mikebook.merge-safe-prs-loop", "plist", "root",
                                  "a" * 64),),
            lock_root=root / "locks",
            train_construction_runtime=root / "train",
            train_construction_commit="b" * 40,
        )
        with patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                   return_value=capability) as resolve:
            passed = production_capability_diagnostic(capability, root)
        self.assertTrue(passed["ok"])
        self.assertEqual(1, resolve.call_count)

        hook_and_root_change = replace(
            capability,
            runtime_roots=(root / "stable-v2", root / "compat", root / "canonical"),
            hook_digests=((str(root / "stable"), "scripts/cooperative_branch_lease.py",
                           "2" * 64),),
        )
        with patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                   return_value=hook_and_root_change):
            changed = production_capability_diagnostic(capability, root)
        self.assertFalse(changed["ok"])
        self.assertEqual(["hook_digests", "runtime_roots"], changed["changed_fields"])
        self.assertEqual({"added_count": 1, "removed_count": 1}, changed["hook_changes"])

        with patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                   side_effect=JgError(
                       "runtime_adoption_unverified: SECRET_VALUE "
                       "com.mikebook.merge-safe-prs-loop")) as resolve:
            failed = production_capability_diagnostic(capability, root)
        self.assertFalse(failed["ok"])
        self.assertEqual("runtime_adoption_unverified", failed["validation_failure"])
        self.assertEqual("com.mikebook.merge-safe-prs-loop", failed["failed_label"])
        self.assertEqual(1, resolve.call_count)
        self.assertNotIn("SECRET_VALUE", json.dumps(failed))

    def test_post_intent_runtime_error_is_sanitized_in_result_and_journal(self):
        repo, topic_tip, main_tip = self.make_repo(old_commits=True)
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip,
                                        "main_tip": main_tip, "verdict": "EXACT",
                                        "reason": None, "last_activity_epoch": 1,
                                        "paths": [{"path": "topic",
                                                   "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            common_dir = _common_dir(repo)
            lease_root = (root / "leases").resolve(strict=False)
            label = "com.mikebook.merge-safe-prs-loop"
            capability = CreatorLeaseCapability(
                common_dir=common_dir,
                runtime_roots=(root / "stable", root / "compat", root / "canonical"),
                hook_digests=((str(root / "stable"), "scripts/cooperative_branch_lease.py",
                               "1" * 64),),
                loaded_runtime_jobs=((label, str(root / "launchd.plist"),
                                      str(root / "stable"), "a" * 64),),
                lock_root=lease_root,
                train_construction_runtime=root / "train-runtime",
                train_construction_commit="b" * 40,
            )
            adapter = CooperativeBranchLeaseAdapter(
                str(common_dir), lease_root, capability=capability,
            )
            adapter.common_dir = common_dir
            journal_path = root / "actions.jsonl"
            marker = "PRIVATE_PATH_SECRET_MARKER"
            resolver_error = CreatorRuntimeValidationError(
                f"runtime_adoption_unverified: failed {label}",
                failure_stage="process_image_mismatch", label=label, pid=4821,
                expected_root=root / "stable", observed_root=root / "stable",
                process_command=marker, process_cwd=root / "stable", process_start=marker,
                observed_process_kind="other",
            )
            with patch("jev_git_graph.cleanup._fixed_lock_root",
                       return_value=lease_root), \
                    patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                          side_effect=[capability, capability, capability,
                                       resolver_error]) as resolve:
                result = execute_cleanup(
                    repo, approved, approved_digest=approved["plan_digest"],
                    lease_contract=adapter, journal_path=journal_path,
                )
            self.assertEqual(4, resolve.call_count)
            self.assertEqual("creator_capability_changed", result["stopped"])
            self.assertEqual("runtime_adoption_unverified",
                             result["capability_diagnostic"]["validation_failure"])
            self.assertEqual("process_image_mismatch",
                             result["capability_diagnostic"]["failure_stage"])
            self.assertEqual(4821, result["capability_diagnostic"]["failed_pid"])
            self.assertNotIn(marker, json.dumps(result))
            self.assertEqual(topic_tip, run(repo, "rev-parse", "refs/heads/topic"))
            events = CleanupActionJournal(journal_path).read_events()
            self.assertEqual(result["capability_diagnostic"],
                             events[-1]["capability_diagnostic"])
            self.assertNotIn(marker, json.dumps(events))

    def test_creator_failure_diagnostic_before_first_transaction_has_no_journal_or_delete(self):
        repo, topic_tip, main_tip = self.make_repo(old_commits=True)
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip,
                                        "main_tip": main_tip, "verdict": "EXACT",
                                        "reason": None, "last_activity_epoch": 1,
                                        "paths": [{"path": "topic",
                                                   "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            common_dir = _common_dir(repo)
            lease_root = (root / "leases").resolve(strict=False)
            label = "com.mikebook.pr-convergence-wake-consumer"
            capability = CreatorLeaseCapability(
                common_dir=common_dir,
                runtime_roots=(root / "stable", root / "compat", root / "canonical"),
                hook_digests=((str(root / "stable"), "scripts/cooperative_branch_lease.py",
                               "1" * 64),),
                loaded_runtime_jobs=((label, str(root / "launchd.plist"),
                                      str(root / "stable"), "a" * 64),),
                lock_root=lease_root,
                train_construction_runtime=root / "train-runtime",
                train_construction_commit=None,
            )
            adapter = CooperativeBranchLeaseAdapter(
                str(common_dir), lease_root, capability=capability,
            )
            adapter.common_dir = common_dir
            journal_path = root / "actions.jsonl"
            marker = "PRIVATE_PRE_TRANSACTION_SECRET"
            resolver_error = CreatorRuntimeValidationError(
                f"runtime_adoption_unverified: failed {label}",
                failure_stage="cwd_mismatch", label=label, pid=7931,
                expected_root=root / "stable", observed_root=root / "other",
                process_command=marker, process_cwd=root / "other", process_start=marker,
                observed_process_kind="other",
            )
            with patch("jev_git_graph.cleanup._fixed_lock_root", return_value=lease_root), \
                    patch("jev_git_graph.coordinator.resolve_production_creator_capability",
                          side_effect=resolver_error) as resolve:
                result = execute_cleanup(
                    repo, approved, approved_digest=approved["plan_digest"],
                    lease_contract=adapter, journal_path=journal_path,
                )
            self.assertEqual(1, resolve.call_count)
            self.assertEqual("cooperative_lease_unestablished", result["stopped"])
            self.assertEqual([], result["deleted"])
            self.assertEqual("cwd_mismatch",
                             result["capability_diagnostic"]["failure_stage"])
            self.assertEqual(7931, result["capability_diagnostic"]["failed_pid"])
            self.assertFalse(result["destructive_action_authorized"])
            self.assertTrue(_ref_exists(repo, "topic"))
            self.assertFalse(journal_path.exists())
            self.assertNotIn(marker, json.dumps(result))

    def test_lease_path_runtime_error_has_fixed_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common_dir = root / "repo.git"
            lease_root = (root / "leases").resolve(strict=False)
            capability = CreatorLeaseCapability(
                common_dir=common_dir,
                runtime_roots=(root / "stable", root / "compat", root / "canonical"),
                hook_digests=(), loaded_runtime_jobs=(), lock_root=lease_root,
                train_construction_runtime=root / "train-runtime",
                train_construction_commit=None,
            )
            adapter = CooperativeBranchLeaseAdapter(
                str(common_dir), lease_root, capability=capability,
            )
            adapter.common_dir = common_dir
            marker = "PRIVATE_PATH_SECRET_MARKER"
            with patch("jev_git_graph.cleanup._fixed_lock_root",
                       return_value=lease_root), \
                    patch("jev_git_graph.cleanup._common_dir",
                          side_effect=RuntimeError(marker + " /private/path")):
                self.assertFalse(_lease_established(adapter, root))
            self.assertEqual("runtime_validation_error",
                             adapter.last_capability_diagnostic["validation_failure"])
            self.assertNotIn(marker, json.dumps(adapter.last_capability_diagnostic))

    def test_post_delete_capability_drift_restores_exact_tip_without_overwriting_recreation(self):
        # Exercise both outcomes after a real CAS deletion: restore the pinned
        # source only while the ref is still absent, and preserve a concurrent
        # recreation at a different tip.
        for recreate in (False, True):
            with self.subTest(recreate=recreate):
                repo, topic_tip, main_tip = self.make_repo(old_commits=True)
                coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                               "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                               "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
                with tempfile.TemporaryDirectory() as out:
                    root = Path(out)
                    plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
                    approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
                    lease = self.integrated_lease(repo)
                    original = _lease_established
                    calls = 0

                    def drift_after_delete(contract, repository):
                        nonlocal calls
                        calls += 1
                        if calls == 5:
                            if recreate:
                                run(repo, "update-ref", "refs/heads/topic", main_tip)
                            return False
                        return original(contract, repository)

                    with patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
                         patch("jev_git_graph.cleanup._lease_established", drift_after_delete):
                        result = execute_cleanup(
                            repo, approved, approved_digest=approved["plan_digest"],
                            lease_contract=lease, journal_path=root / "actions.jsonl",
                        )
                    observed_tip = run(repo, "rev-parse", "refs/heads/topic")
                self.assertEqual(5, calls)
                self.assertEqual("creator_capability_changed_after_delete", result["stopped"])
                self.assertTrue(result["restoration_attempted"])
                if recreate:
                    self.assertFalse(result["restored"])
                    self.assertEqual(main_tip, observed_tip)
                else:
                    self.assertTrue(result["restored"])
                    self.assertEqual(topic_tip, observed_tip)

    def test_interrupted_reconciliation_restores_absent_ref_from_bundle(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            action_id = cleanup_action_id(approved["plan_digest"], 0, "topic", topic_tip)
            fixture_root = repo.parent
            inventory = build_disposable_fixture_inventory(
                repo, ["topic"], operator="interrupted fixture", fixture_root=fixture_root)
            lease = CooperativeBranchLeaseAdapter.for_disposable_fixture(
                repo, inventory, ["topic"], fixture_root=fixture_root)
            receipt = capability_receipt_metadata(lease.capability)
            journal = CleanupActionJournal(root / "actions.jsonl")
            journal.append({
                "event": "intent", "action_id": action_id,
                "plan_digest": approved["plan_digest"], "candidate_index": 0,
                "branch": "topic",
                "tip": topic_tip, "destination": "main", "destination_tip": main_tip,
                "bundle_sha256": approved["bundle"]["sha256"],
                **receipt,
            })
            run(repo, "update-ref", "-d", "refs/heads/topic", topic_tip)
            results = reconcile_interrupted_cleanup(repo, approved, journal, lease)
            restored_tip = run(repo, "rev-parse", "refs/heads/topic")
            events = journal.read_events()
        self.assertEqual("source_restored_after_interruption", results[0]["status"])
        self.assertTrue(results[0]["restored"])
        self.assertEqual(topic_tip, restored_tip)
        self.assertEqual("reconciled", events[-1]["event"])
        self.assertEqual(receipt["creator_capability_sha256"],
                         events[-1]["creator_capability_sha256"])
        self.assertEqual([], journal.pending_intents(plan_digest=approved["plan_digest"]))

    def test_interrupted_reconciliation_rejects_changed_creator_receipt(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            lease = self.integrated_lease(repo)
            receipt = capability_receipt_metadata(lease.capability)
            action_id = cleanup_action_id(approved["plan_digest"], 0, "topic", topic_tip)
            journal = CleanupActionJournal(root / "actions.jsonl")
            journal.append({
                "event": "intent", "action_id": action_id,
                "plan_digest": approved["plan_digest"], "candidate_index": 0,
                "branch": "topic", "tip": topic_tip,
                "destination": "main", "destination_tip": main_tip,
                "bundle_sha256": approved["bundle"]["sha256"],
                **{**receipt, "creator_capability_sha256": "0" * 64},
            })
            with self.assertRaisesRegex(JgError, "creator capability"):
                reconcile_interrupted_cleanup(repo, approved, journal, lease)
            self.assertEqual(topic_tip, run(repo, "rev-parse", "refs/heads/topic"))

    def test_interrupted_reconciliation_preserves_recreated_ref(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            action_id = cleanup_action_id(approved["plan_digest"], 0, "topic", topic_tip)
            fixture_root = repo.parent
            receipt = capability_receipt_metadata(self.integrated_lease(repo).capability)
            journal = CleanupActionJournal(root / "actions.jsonl")
            journal.append({
                "event": "intent", "action_id": action_id,
                "plan_digest": approved["plan_digest"], "candidate_index": 0,
                "branch": "topic",
                "tip": topic_tip, "destination": "main", "destination_tip": main_tip,
                "bundle_sha256": approved["bundle"]["sha256"],
                **receipt,
            })
            run(repo, "branch", "--force", "topic", main_tip)
            lease = self.integrated_lease(repo)
            results = reconcile_interrupted_cleanup(repo, approved, journal, lease)
            current_tip = run(repo, "rev-parse", "refs/heads/topic")
        self.assertEqual("source_recreated_or_moved", results[0]["status"])
        self.assertFalse(results[0]["restored"])
        self.assertEqual(main_tip, current_tip)

    def test_destination_verify_and_source_delete_are_one_transaction(self):
        repo, topic_tip, main_tip = self.make_repo()
        self.assertFalse(_atomic_delete(repo, "topic", topic_tip, "main", "0" * 40))
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))
        self.assertEqual(main_tip, run(repo, "rev-parse", "main"))

    def test_failed_post_delete_readback_restores_only_confirmed_absence(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            plan = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            releases = []
            lease = self.integrated_lease(repo)
            original_release = lease.release
            lease.release = lambda name, tip: releases.append((name, tip)) or original_release(name, tip)
            with patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
                 patch("jev_git_graph.cleanup.git.local_branches",
                       side_effect=JgError("readback unavailable")):
                result = execute_cleanup(repo, plan,
                                         approved_digest=plan["plan_digest"],
                                         lease_contract=lease,
                                         journal_path=Path(out) / "actions.jsonl")
        self.assertEqual("delete_readback_uncertain", result["stopped"])
        self.assertTrue(result["restored"])
        self.assertEqual([("topic", topic_tip)], releases)
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))

    def test_executor_rejects_approved_plan_from_another_clone(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out) / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            clone = Path(out) / "clone"
            subprocess.run(("git", "clone", "--quiet", "--no-hardlinks", str(repo), str(clone)), check=True)
            with self.assertRaisesRegex(JgError, "different local repository"):
                execute_cleanup(clone, approved, approved_digest=approved["plan_digest"])

    def test_failed_lease_release_restores_deleted_fixture_ref(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            lease = self.integrated_lease(repo)
            original_release = lease.release
            lease.release = lambda name, tip: (original_release(name, tip), False)[1]
            with patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
                 patch("jev_git_graph.cleanup._ref_presence", return_value=None):
                result = execute_cleanup(repo, approved,
                                         approved_digest=approved["plan_digest"],
                                         lease_contract=lease,
                                         journal_path=Path(out) / "actions.jsonl")
        self.assertEqual("lease_release_failed", result["stopped"])
        self.assertTrue(result["restoration_attempted"])
        self.assertTrue(result["restored"])
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))

    def test_manifest_approval_rejects_tampered_plan(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            plan["candidates"][0]["tip"] = "0" * 40
            with self.assertRaisesRegex(JgError, "invalid digest"):
                approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])

    def test_plan_digest_is_stable_and_json_round_trips(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            encoded = json.loads(json.dumps(plan))
        self.assertEqual(plan["plan_digest"], digest({key: value for key, value in encoded.items() if key != "plan_digest"}))


if __name__ == "__main__":
    unittest.main()
