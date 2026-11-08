import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "scripts"))
import kubestepup as ks  # noqa: E402

FAKES = str(ROOT / "tests" / "fakes")
EXAMPLES = str(ROOT / "examples" / "manifests")


def run_cli(*args, env=None):
    out = io.StringIO()
    values = {"PATH": FAKES + os.pathsep + os.environ["PATH"]}
    values.update(env or {})
    with mock.patch.dict(os.environ, values), contextlib.redirect_stdout(out):
        code = ks.main(list(args))
    return code, out.getvalue()


def report_for(*args, env=None):
    code, output = run_cli(*args, "--format", "json", env=env)
    return code, json.loads(output)


def checks(report):
    return {finding["check"]: finding for finding in report["findings"]}


class VersionTest(unittest.TestCase):
    def test_parse_minor(self):
        for text in ("1.33", "v1.33.2", "1.33+", "v1.33.1+k3s1"):
            self.assertEqual(ks.parse_minor(text), 33)
        with self.assertRaises(ValueError):
            ks.parse_minor("two")

    def test_every_minor_is_a_step(self):
        self.assertEqual(ks.hops_between(31, 33), [32, 33])


class TableTest(unittest.TestCase):
    def test_lookup(self):
        self.assertEqual(ks.removals_for("batch/v1beta1", "CronJob")[0][0], 25)
        self.assertEqual(ks.removals_for("storage.k8s.io/v1beta1", "StorageClass")[0][0], 22)
        self.assertEqual(ks.removals_for("storage.k8s.io/v1beta1", "CSIStorageCapacity")[0][0], 27)
        self.assertEqual(ks.removals_for("apps/v1", "Deployment"), [])


class ClusterTest(unittest.TestCase):
    def test_plan_lists_each_step(self):
        _, report = report_for("--kube", "--to", "1.33")
        self.assertEqual(report["current"], "1.31")
        self.assertEqual(report["hops"], ["1.32", "1.33"])
        self.assertEqual(report["context"], "demo-cluster")
        self.assertEqual(report["verdict"], "BLOCKED")

    def test_api_in_use_comes_from_the_metric_for_steps_only(self):
        _, report = report_for("--kube", "--to", "1.33")
        finding = checks(report)["api-in-use"]
        self.assertIn("flowcontrol.apiserver.k8s.io/v1beta3 flowschemas, removed in 1.32", finding["summary"])
        self.assertEqual(finding["hop"], 32)
        self.assertNotIn("things", json.dumps(report["findings"]))
        self.assertNotIn("componentstatuses", json.dumps(report["findings"]))

    def test_metric_reports_a_removal_missing_from_the_table(self):
        _, report = report_for("--kube", "--to", "1.40")
        self.assertIn("example.com/v1 things, removed in 1.40", json.dumps(report["findings"]))

    def test_last_applied_version(self):
        _, report = report_for("--kube", "--to", "1.33")
        finding = checks(report)["api-applied"]
        self.assertIn("demo-flow", finding["summary"])
        self.assertNotIn("current", finding["summary"])

    def test_removal_after_the_target_isnt_a_blocker(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "a.yaml").write_text("apiVersion: flowcontrol.apiserver.k8s.io/v1beta3\nkind: FlowSchema\n")
            code, report = report_for("--current", "1.28", "--to", "1.31", "--manifests", temp)
        self.assertEqual(code, ks.EXIT_READY)
        self.assertEqual(report["findings"], [])

    def test_pdb_blocks_only_when_it_has_pods_and_no_room(self):
        _, report = report_for("--kube", "--to", "1.32")
        summary = checks(report)["pdb-blocks-drain"]["summary"]
        self.assertIn("shop/checkout", summary)
        self.assertNotIn("search", summary)
        self.assertNotIn("unused", summary)

    def test_emptydir_warning_includes_disk_and_memory_but_not_persistent_volumes(self):
        pods = [
            {"metadata": {"namespace": "shop", "name": "disk"}, "spec": {"nodeName": "worker-1", "volumes": [{"emptyDir": {}}]}},
            {"metadata": {"namespace": "shop", "name": "memory"}, "spec": {"nodeName": "worker-1", "volumes": [{"emptyDir": {"medium": "Memory"}}]}},
            {"metadata": {"namespace": "shop", "name": "persistent"}, "spec": {"nodeName": "worker-1", "volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}},
            {"metadata": {"namespace": "shop", "name": "no-volume"}},
        ]
        cluster = mock.Mock()
        cluster.items.side_effect = [[], pods]
        report = ks.Report()
        ks.check_workloads(cluster, report)
        finding = next(f for f in report.findings if f.check == "emptydir-data-loss")
        self.assertEqual(finding.severity, "WARNING")
        self.assertIn("shop/disk", finding.summary)
        self.assertIn("shop/memory", finding.summary)
        self.assertNotIn("shop/persistent", finding.summary)
        self.assertNotIn("shop/no-volume", finding.summary)
        self.assertIn("--delete-emptydir-data", finding.summary)
        self.assertIn("lost", finding.summary)

    def test_emptydir_warning_excludes_pods_drain_does_not_delete(self):
        pods = [
            {"metadata": {"namespace": "logging", "name": f"fluent-bit-{i}",
                          "ownerReferences": [{"kind": "DaemonSet", "controller": True}]},
             "spec": {"nodeName": "worker-1", "volumes": [{"emptyDir": {}}]}}
            for i in range(20)
        ]
        pods.append({"metadata": {"namespace": "kube-system", "name": "etcd-n1",
                                  "annotations": {"kubernetes.io/config.mirror": "hash"}},
                     "spec": {"nodeName": "worker-1", "volumes": [{"emptyDir": {}}]}})
        cluster = mock.Mock()
        cluster.items.side_effect = [[], pods]
        report = ks.Report()
        ks.check_workloads(cluster, report)
        self.assertFalse(any(f.check == "emptydir-data-loss" for f in report.findings))

        pods.append({"metadata": {"namespace": "shop", "name": "zz-cache",
                                  "ownerReferences": [{"kind": "ReplicaSet", "controller": True}]},
                     "spec": {"nodeName": "worker-1", "volumes": [{"emptyDir": {}}]}})
        cluster.items.side_effect = [[], pods]
        report = ks.Report()
        ks.check_workloads(cluster, report)
        finding = next(f for f in report.findings if f.check == "emptydir-data-loss")
        self.assertIn("shop/zz-cache", finding.summary)
        self.assertNotIn("fluent-bit", finding.summary)
        self.assertNotIn("etcd-n1", finding.summary)
        self.assertNotIn("more", finding.summary)

    def test_emptydir_warning_keeps_noncontroller_daemonset_references(self):
        cluster = mock.Mock()
        cluster.items.side_effect = [[], [{"metadata": {"name": "cache",
            "ownerReferences": [{"kind": "DaemonSet", "controller": False}]},
            "spec": {"nodeName": "worker-1", "volumes": [{"emptyDir": {}}]}}]]
        report = ks.Report()
        ks.check_workloads(cluster, report)
        self.assertTrue(any(f.check == "emptydir-data-loss" for f in report.findings))

    def test_null_volumes_do_not_warn_or_fail(self):
        cluster = mock.Mock()
        cluster.items.side_effect = [[], [{"metadata": {"name": "no-volumes"}, "spec": {"nodeName": "worker-1", "volumes": None}}]]
        report = ks.Report()
        ks.check_workloads(cluster, report)
        self.assertFalse(any(f.check == "emptydir-data-loss" for f in report.findings))
        self.assertEqual(report.failures, [])

    def test_emptydir_warning_excludes_finished_pods_before_truncation(self):
        pods = [{"metadata": {"namespace": "a", "name": f"job-{i}",
                             "ownerReferences": [{"kind": "Job", "controller": True}]},
                 "status": {"phase": phase}, "spec": {"nodeName": "worker-1", "volumes": [{"emptyDir": {}}]}}
                for phase in ("Succeeded", "Failed") for i in range(15)]
        cluster = mock.Mock()
        cluster.items.side_effect = [[], pods]
        report = ks.Report()
        ks.check_workloads(cluster, report)
        self.assertFalse(any(f.check == "emptydir-data-loss" for f in report.findings))
        pods.append({"metadata": {"namespace": "shop", "name": "zz-cache",
                                  "deletionTimestamp": "2026-11-01T00:00:00Z",
                                  "ownerReferences": [{"kind": "ReplicaSet", "controller": True}]},
                     "status": {"phase": "Running"}, "spec": {"nodeName": "worker-1", "volumes": [{"emptyDir": {}}]}})
        cluster.items.side_effect = [[], pods]
        report = ks.Report()
        ks.check_workloads(cluster, report)
        finding = next(f for f in report.findings if f.check == "emptydir-data-loss")
        self.assertIn("shop/zz-cache", finding.summary)
        self.assertNotIn("a/job-", finding.summary)
        self.assertNotIn("more", finding.summary)

    def test_null_pod_fields_do_not_abort_workload_checks(self):
        for owned in (False, True, "null-entry"):
            for spec in (None, {"nodeName": "worker-1", "volumes": [None]}, {"nodeName": "worker-1", "volumes": [None, {"emptyDir": {}}]}):
                with self.subTest(owned=owned, spec=spec):
                    metadata = {"name": "cache", "annotations": None}
                    if owned:
                        metadata["ownerReferences"] = [None] if owned == "null-entry" else [{"kind": "ReplicaSet", "controller": True}]
                    cluster = mock.Mock()
                    cluster.items.side_effect = [[], [{"metadata": metadata, "spec": spec, "status": None}]]
                    report = ks.Report()
                    ks.check_workloads(cluster, report)
                    checks = {f.check for f in report.findings}
                    self.assertEqual("emptydir-data-loss" in checks, spec == {"nodeName": "worker-1", "volumes": [None, {"emptyDir": {}}]})
                    self.assertEqual("unowned-pod" in checks, not owned)
                    self.assertEqual(report.failures, [])

    def test_emptydir_warning_excludes_unscheduled_pods_before_truncation(self):
        pods = [{"metadata": {"namespace": "shop", "name": f"unscheduled-{i}"},
                 "status": {"phase": "Pending"}, "spec": {"volumes": [{"emptyDir": {}}]}}
                for i in range(15)]
        pods.append({"metadata": {"namespace": "shop", "name": "scheduled"},
                     "status": {"phase": "Pending"},
                     "spec": {"nodeName": "worker-1", "volumes": [{"emptyDir": {}}]}})
        cluster = mock.Mock()
        cluster.items.side_effect = [[], pods]
        report = ks.Report()
        ks.check_workloads(cluster, report)
        finding = next(f for f in report.findings if f.check == "emptydir-data-loss")
        self.assertIn("shop/scheduled", finding.summary)
        self.assertNotIn("unscheduled", finding.summary)
        self.assertNotIn("more", finding.summary)

    def test_unreadable_pods_do_not_claim_emptydir_coverage(self):
        report = ks.Report()
        cluster = ks.Cluster(None, report)
        with mock.patch.object(cluster, "json", side_effect=[{"items": []}, ks.ReadError("forbidden")]):
            ks.check_workloads(cluster, report)
        self.assertEqual(report.failures, ["pods: forbidden"])
        self.assertEqual(report.coverage, [])
        self.assertEqual(report.findings, [])

    def test_webhook_fail_closed_without_ready_backend(self):
        _, report = report_for("--kube", "--to", "1.32")
        summary = checks(report)["webhook-no-backend"]["summary"]
        self.assertIn("gone.example.com", summary)
        self.assertIn("down.example.com", summary)
        self.assertNotIn("soft.example.com", summary)
        self.assertNotIn("good.example.com", summary)

    def test_kubelet_skew_rules(self):
        _, report = report_for("--kube", "--to", "1.33")
        skew = checks(report)["kubelet-skew"]
        self.assertIn("node-b runs kubelet 1.29, too old for an API server at 1.33", skew["summary"])
        self.assertEqual(skew["hop"], 33)
        _, nearer = report_for("--kube", "--to", "1.32")
        self.assertNotIn("kubelet-skew", checks(nearer))

    def test_old_kubelets_allow_two_minors(self):
        report = ks.Report()
        cluster = mock.Mock()
        cluster.items.return_value = [{"metadata": {"name": "n"}, "status": {"nodeInfo": {"kubeletVersion": "v1.24.0"}, "conditions": [{"type": "Ready", "status": "True"}]}}]
        ks.check_nodes(cluster, 25, [26, 27], report)
        self.assertEqual([finding.hop for finding in report.findings], [27])

    def test_kubelet_newer_than_server(self):
        report = ks.Report()
        cluster = mock.Mock()
        cluster.items.return_value = [{"metadata": {"name": "n"}, "status": {"nodeInfo": {"kubeletVersion": "v1.32.0"}, "conditions": [{"type": "Ready", "status": "True"}]}}]
        ks.check_nodes(cluster, 31, [32], report)
        self.assertIn("newer than the API server", report.findings[0].summary)

    def test_not_ready_node(self):
        _, report = report_for("--kube", "--to", "1.32")
        self.assertIn("node-c", checks(report)["node-not-ready"]["summary"])

    def test_warnings(self):
        _, report = report_for("--kube", "--to", "1.33")
        found = checks(report)
        self.assertIn("widgets.example.com (v1alpha1)", found["crd-stored-version"]["summary"])
        self.assertNotIn("fine.example.com", found["crd-stored-version"]["summary"])
        self.assertIn("shop/checkout", found["single-replica"]["summary"])
        self.assertNotIn("search", found["single-replica"]["summary"])
        self.assertNotIn("coredns", found["single-replica"]["summary"])
        self.assertIn("shop/debug", found["unowned-pod"]["summary"])
        self.assertNotIn("owned", found["unowned-pod"]["summary"])
        self.assertNotIn("mirror", found["unowned-pod"]["summary"])
        self.assertIn("kubectl is 1.31", found["kubectl-skew"]["summary"])

    def test_only_reads_and_never_touches_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp, "log")
            run_cli("--kube", "--to", "1.33", env={"FAKE_LOG": str(log)})
            calls = log.read_text().splitlines()
        self.assertGreater(len(calls), 10)
        for call in calls:
            self.assertRegex(call, r"^kubectl (version -o json|config current-context|get --raw /metrics|get [a-z0-9.]+ -o json( -A)?)$")
            self.assertNotIn("secret", call)

    def test_context_is_passed(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp, "log")
            run_cli("--kube", "--context", "stage", "--to", "1.32", env={"FAKE_LOG": str(log)})
            calls = log.read_text().splitlines()
        self.assertTrue(all(call.startswith("kubectl --context stage ") for call in calls))

    def test_forbidden_read_is_reported_and_others_continue(self):
        code, report = report_for("--kube", "--to", "1.32", env={"FAKE_DENY": "poddisruptionbudgets,metrics"})
        self.assertEqual(code, ks.EXIT_ATTENTION)
        joined = " ".join(report["failures"])
        self.assertIn("poddisruptionbudgets", joined)
        self.assertIn("/metrics", joined)
        self.assertIn("webhook-no-backend", checks(report))

    def test_a_failed_read_is_never_called_clear(self):
        report = ks.Report(failures=["pods: forbidden"])
        self.assertEqual(ks.verdict(report), "INCOMPLETE")

    def test_missing_kubectl_is_incomplete(self):
        real_which = ks.shutil.which
        with mock.patch.object(ks.shutil, "which", lambda name: None if name == "kubectl" else real_which(name)):
            code, output = run_cli("--kube", "--to", "1.32")
        self.assertEqual(code, ks.EXIT_ATTENTION)
        self.assertIn("INCOMPLETE", output)
        self.assertIn("kubectl isn't installed", output)


class ManifestTest(unittest.TestCase):
    def test_finds_removed_and_already_removed(self):
        code, report = report_for("--current", "1.31", "--to", "1.33", "--manifests", EXAMPLES)
        self.assertEqual(code, ks.EXIT_ATTENTION)
        summaries = [finding["summary"] for finding in report["findings"]]
        self.assertTrue(any("FlowSchema uses flowcontrol.apiserver.k8s.io/v1beta3, removed in 1.32" in s for s in summaries))
        self.assertTrue(any("CronJob uses batch/v1beta1, already removed in 1.25" in s for s in summaries))
        self.assertFalse(any("HorizontalPodAutoscaler" in s for s in summaries))

    def test_kinds_parser(self):
        text = 'apiVersion: "batch/v1beta1"\nkind: CronJob\nspec:\n  template:\n    kind: Nested\n---\nkind: Only\n'
        self.assertEqual(list(ks.manifest_kinds(text)), [("batch/v1beta1", "CronJob")])

    def test_skips_vendored_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            vendor = Path(temp, "vendor")
            vendor.mkdir()
            Path(vendor, "a.yaml").write_text("apiVersion: batch/v1beta1\nkind: CronJob\n")
            _, report = report_for("--current", "1.31", "--to", "1.32", "--manifests", temp)
        self.assertEqual(report["findings"], [])

    def test_windows_line_endings_in_multiple_documents(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "windows.yaml")
            path.write_bytes(b"apiVersion: batch/v1beta1\r\nkind: CronJob\r\n---\r\n"
                             b"apiVersion: flowcontrol.apiserver.k8s.io/v1beta3\r\nkind: FlowSchema\r\n")
            code, report = report_for("--current", "1.31", "--to", "1.32", "--manifests", str(path))
        self.assertEqual(code, ks.EXIT_ATTENTION)
        self.assertEqual(len(report["findings"]), 2)
        summaries = " ".join(finding["summary"] for finding in report["findings"])
        self.assertIn("CronJob uses batch/v1beta1", summaries)
        self.assertIn("FlowSchema uses flowcontrol.apiserver.k8s.io/v1beta3", summaries)
        self.assertEqual(report["failures"], [])

    def test_directory_discovers_yaml_templates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "cronjob.yaml.tpl")
            path.write_text("apiVersion: batch/v1beta1\nkind: CronJob\nmetadata:\n  name: {{ .Values.name }}\n")
            Path(temp, "ignored.tpl").write_text(path.read_text())
            self.assertEqual(list(ks.manifest_files([temp])), [path])
            code, report = report_for("--current", "1.31", "--to", "1.32", "--manifests", temp)
        self.assertEqual(code, ks.EXIT_ATTENTION)
        self.assertEqual(len(report["findings"]), 1)
        self.assertIn("CronJob uses batch/v1beta1", report["findings"][0]["summary"])

    def test_missing_path_is_reported(self):
        code, report = report_for("--current", "1.31", "--to", "1.32", "--manifests", "/no/such/place")
        self.assertEqual(code, ks.EXIT_ATTENTION)
        self.assertEqual(report["verdict"], "INCOMPLETE")


class UsageTest(unittest.TestCase):
    def usage_error(self, *args):
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            run_cli(*args)
        self.assertEqual(caught.exception.code, ks.EXIT_USAGE)

    def test_errors(self):
        self.usage_error("--to", "1.33")
        self.usage_error("--to", "soon", "--kube")
        self.usage_error("--to", "1.31", "--kube")
        self.usage_error("--to", "1.33", "--manifests", EXAMPLES)


class OutputTest(unittest.TestCase):
    def test_text_has_steps_links_and_limits(self):
        _, output = run_cli("--kube", "--to", "1.33")
        self.assertIn("Step to 1.32:", output)
        self.assertIn("CHANGELOG-1.33.md", output)
        self.assertIn("Not checked: add-on", output)
        self.assertIn("Cluster context: demo-cluster", output)

    def test_markdown_is_a_checklist(self):
        _, output = run_cli("--kube", "--to", "1.32", "--format", "markdown")
        self.assertIn("- [ ] **BLOCKER**", output)
        self.assertIn("## Step to 1.32", output)

    def test_clean_flattens_outside_text(self):
        self.assertEqual(ks.clean("a\nb\x1b[0m"), "a b [0m")


class ProjectTest(unittest.TestCase):
    def test_script_has_no_network_or_third_party_imports(self):
        source = (ROOT / "plugin" / "scripts" / "kubestepup.py").read_text()
        imported = set(re.findall(r"^(?:from|import) (\w+)", source, re.M))
        self.assertLessEqual(imported, {"argparse", "json", "re", "shutil", "subprocess", "sys", "dataclasses", "pathlib"})
        self.assertNotIn("shell=True", source)

    def test_skill_frontmatter(self):
        text = (ROOT / "plugin" / "skills" / "planning-kubernetes-upgrades" / "SKILL.md").read_text()
        self.assertRegex(re.search(r"^name: (.+)$", text, re.M).group(1), r"^[a-z0-9-]{1,64}$")
        self.assertLessEqual(len(re.search(r"^description: (.+)$", text, re.M).group(1)), 1024)

    def test_evals_are_valid(self):
        self.assertGreaterEqual(len(json.loads((ROOT / "evals" / "evals.json").read_text())), 3)


if __name__ == "__main__":
    unittest.main()
