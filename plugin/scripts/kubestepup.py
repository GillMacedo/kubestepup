#!/usr/bin/env python3
"""Plan a Kubernetes minor version upgrade, one step at a time, and find what would block it. Read-only."""

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

EXIT_READY, EXIT_ATTENTION, EXIT_USAGE = 0, 1, 2
COMMAND_TIMEOUT = 120  # seconds; listing every object in a large cluster can be slow
TEXT_LIMIT = 200  # keeps one finding on one readable line
NAMES_SHOWN = 10  # enough to act on without burying the report
KUBELET_MAX_SKEW = 3  # version skew policy, for kubelet 1.25 and newer
KUBELET_MAX_SKEW_OLD = 2  # version skew policy, for kubelet older than 1.25
KUBECTL_MAX_SKEW = 1  # version skew policy
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
METRIC = re.compile(r"^apiserver_requested_deprecated_apis\{(?P<labels>[^}]*)\}\s+\S+")
LABEL = re.compile(r'(\w+)="([^"]*)"')
MANIFEST_SUFFIXES = (".yaml", ".yml", ".json", ".yaml.tpl")
SKIP_DIRS = {".git", "node_modules", "vendor"}
TABLE_CHECKED = "2026-11-01"
GUIDE = "https://kubernetes.io/docs/reference/using-api/deprecation-guide/"
CHANGELOG = "https://github.com/kubernetes/kubernetes/blob/master/CHANGELOG/CHANGELOG-1.{minor}.md"

# From the Kubernetes deprecation guide: (removed in minor, apiVersion, kinds, use instead).
REMOVALS = [
    (32, "flowcontrol.apiserver.k8s.io/v1beta3", ("FlowSchema", "PriorityLevelConfiguration"), "flowcontrol.apiserver.k8s.io/v1"),
    (29, "flowcontrol.apiserver.k8s.io/v1beta2", ("FlowSchema", "PriorityLevelConfiguration"), "flowcontrol.apiserver.k8s.io/v1"),
    (27, "storage.k8s.io/v1beta1", ("CSIStorageCapacity",), "storage.k8s.io/v1"),
    (26, "flowcontrol.apiserver.k8s.io/v1beta1", ("FlowSchema", "PriorityLevelConfiguration"), "flowcontrol.apiserver.k8s.io/v1"),
    (26, "autoscaling/v2beta2", ("HorizontalPodAutoscaler",), "autoscaling/v2"),
    (25, "batch/v1beta1", ("CronJob",), "batch/v1"),
    (25, "discovery.k8s.io/v1beta1", ("EndpointSlice",), "discovery.k8s.io/v1"),
    (25, "events.k8s.io/v1beta1", ("Event",), "events.k8s.io/v1"),
    (25, "autoscaling/v2beta1", ("HorizontalPodAutoscaler",), "autoscaling/v2"),
    (25, "policy/v1beta1", ("PodDisruptionBudget",), "policy/v1"),
    (25, "policy/v1beta1", ("PodSecurityPolicy",), "Pod Security Admission"),
    (25, "node.k8s.io/v1beta1", ("RuntimeClass",), "node.k8s.io/v1"),
    (22, "admissionregistration.k8s.io/v1beta1", ("MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"), "admissionregistration.k8s.io/v1"),
    (22, "apiextensions.k8s.io/v1beta1", ("CustomResourceDefinition",), "apiextensions.k8s.io/v1"),
    (22, "apiregistration.k8s.io/v1beta1", ("APIService",), "apiregistration.k8s.io/v1"),
    (22, "authentication.k8s.io/v1beta1", ("TokenReview",), "authentication.k8s.io/v1"),
    (22, "authorization.k8s.io/v1beta1", ("LocalSubjectAccessReview", "SelfSubjectAccessReview", "SubjectAccessReview", "SelfSubjectRulesReview"), "authorization.k8s.io/v1"),
    (22, "certificates.k8s.io/v1beta1", ("CertificateSigningRequest",), "certificates.k8s.io/v1"),
    (22, "coordination.k8s.io/v1beta1", ("Lease",), "coordination.k8s.io/v1"),
    (22, "extensions/v1beta1", ("Ingress",), "networking.k8s.io/v1"),
    (22, "networking.k8s.io/v1beta1", ("Ingress", "IngressClass"), "networking.k8s.io/v1"),
    (22, "rbac.authorization.k8s.io/v1beta1", ("ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding"), "rbac.authorization.k8s.io/v1"),
    (22, "scheduling.k8s.io/v1beta1", ("PriorityClass",), "scheduling.k8s.io/v1"),
    (22, "storage.k8s.io/v1beta1", ("CSIDriver", "CSINode", "StorageClass", "VolumeAttachment"), "storage.k8s.io/v1"),
    (16, "extensions/v1beta1", ("NetworkPolicy",), "networking.k8s.io/v1"),
    (16, "extensions/v1beta1", ("DaemonSet", "Deployment", "ReplicaSet"), "apps/v1"),
    (16, "extensions/v1beta1", ("PodSecurityPolicy",), "policy/v1beta1"),
    (16, "apps/v1beta2", ("DaemonSet", "Deployment", "StatefulSet", "ReplicaSet"), "apps/v1"),
    (16, "apps/v1beta1", ("Deployment", "StatefulSet", "ReplicaSet"), "apps/v1"),
]

# Kinds whose last-applied annotation is worth reading. Secrets are never listed.
ANNOTATED_RESOURCES = ("deployments", "daemonsets", "statefulsets", "cronjobs", "horizontalpodautoscalers", "poddisruptionbudgets",
                       "ingresses", "ingressclasses", "flowschemas", "prioritylevelconfigurations", "runtimeclasses")


class ReadError(Exception):
    """Something couldn't be read. Reported in coverage, never hidden."""


@dataclass
class Finding:
    severity: str  # BLOCKER must be fixed before the step; WARNING needs a decision
    check: str
    summary: str
    hop: int = 0  # target minor of the step it affects; 0 means before any step


@dataclass
class Report:
    current: str = ""
    target: str = ""
    context: str = ""
    hops: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    coverage: list = field(default_factory=list)
    failures: list = field(default_factory=list)


def clean(text, limit=TEXT_LIMIT):
    """Flatten text from outside so it can't fake extra lines or terminal escapes."""
    text = CONTROL_CHARS.sub(" ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_minor(text):
    """Read the minor from forms like 1.33, v1.33.2 or 1.33+. Only Kubernetes 1.x exists."""
    match = re.match(r"^v?1\.(\d+)", str(text).strip())
    if not match:
        raise ValueError(f"{text} doesn't look like a Kubernetes version. Use a form like 1.33")
    return int(match.group(1))


def hops_between(current, target):
    """The control plane can't skip minors, so every minor on the way is a step."""
    return list(range(current + 1, target + 1))


def removals_for(api_version, kind):
    return [entry for entry in REMOVALS if entry[1] == api_version and kind in entry[2]]


def run(command):
    if shutil.which(command[0]) is None:
        raise ReadError(f"{command[0]} isn't installed")
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        raise ReadError(f"{command[0]} timed out after {COMMAND_TIMEOUT}s") from error
    if done.returncode != 0:
        raise ReadError(clean(done.stderr or f"{command[0]} exited {done.returncode}"))
    return done.stdout


class Cluster:
    def __init__(self, context, report):
        self.base = ["kubectl"] + (["--context", context] if context else [])
        self.report = report

    def json(self, *args):
        try:
            return json.loads(run(self.base + list(args)))
        except json.JSONDecodeError as error:
            raise ReadError("kubectl returned something other than JSON") from error

    def items(self, resource, namespaced=True):
        """List a resource. A failure is recorded and returns nothing, so other checks still run."""
        scope = ["-A"] if namespaced else []
        try:
            return self.json("get", resource, "-o", "json", *scope).get("items", [])
        except ReadError as error:
            self.report.failures.append(f"{resource}: {error}")
            return None

    def raw(self, path):
        return run(self.base + ["get", "--raw", path])


def ref(item):
    metadata = item.get("metadata", {})
    namespace = metadata.get("namespace")
    return f"{namespace}/{metadata.get('name')}" if namespace else str(metadata.get("name"))


def named(names):
    shown = ", ".join(clean(name, 60) for name in sorted(names)[:NAMES_SHOWN])
    more = f" and {len(names) - NAMES_SHOWN} more" if len(names) > NAMES_SHOWN else ""
    return shown + more


# --- removed APIs ---


def check_removal_metric(cluster, hops, report):
    """The API server counts requests to deprecated APIs, including ones not in REMOVALS."""
    try:
        text = cluster.raw("/metrics")
    except ReadError as error:
        report.failures.append(f"/metrics: {error}")
        return
    report.coverage.append("Requests to deprecated APIs, from one API server's metrics since it last started. Clients that haven't called it since then won't show.")
    seen = {}
    for line in text.splitlines():
        match = METRIC.match(line)
        if not match:
            continue
        labels = dict(LABEL.findall(match.group("labels")))
        try:
            removed = parse_minor(labels.get("removed_release", ""))
        except ValueError:
            continue
        if removed in hops:
            group = labels.get("group", "")
            version = f"{group}/{labels.get('version')}" if group else str(labels.get("version"))
            seen[(removed, version, labels.get("resource"))] = True
    for removed, version, resource in sorted(seen):
        report.findings.append(Finding("BLOCKER", "api-in-use", clean(f"Something is calling {version} {resource}, removed in 1.{removed}. Find the client and move it."), removed))


def check_last_applied(cluster, hops, report):
    report.coverage.append("Objects' last-applied apiVersion, for objects applied with kubectl. Helm releases are stored in Secrets and aren't read.")
    found = {}
    for resource in ANNOTATED_RESOURCES:
        items = cluster.items(resource)
        for item in items or []:
            text = item.get("metadata", {}).get("annotations", {}).get("kubectl.kubernetes.io/last-applied-configuration")
            if not text:
                continue
            try:
                applied = json.loads(text)
            except json.JSONDecodeError:
                continue
            api_version, kind = applied.get("apiVersion"), applied.get("kind")
            for removed, version, _, replacement in removals_for(api_version, kind):
                if removed in hops:
                    found.setdefault((removed, version, kind, replacement), []).append(ref(item))
    for (removed, version, kind, replacement), names in sorted(found.items()):
        report.findings.append(Finding("BLOCKER", "api-applied", f"{kind} last applied as {version}, removed in 1.{removed}: {named(names)}. Update the source manifests to {replacement}.", removed))


def manifest_files(paths):
    for path in paths:
        root = Path(path)
        if root.is_file():
            yield root
            continue
        if not root.is_dir():
            raise ReadError(f"{path} doesn't exist")
        for file in sorted(root.rglob("*")):
            if file.is_file() and file.name.endswith(MANIFEST_SUFFIXES) and not SKIP_DIRS.intersection(file.parts):
                yield file


def manifest_kinds(text):
    """Find top-level apiVersion and kind in each YAML document, without a YAML parser."""
    for document in re.split(r"(?m)^---\s*$", text):
        api_version = re.search(r"(?m)^(?:\"apiVersion\"|apiVersion)\s*:\s*[\"']?([\w./-]+)", document)
        kind = re.search(r"(?m)^(?:\"kind\"|kind)\s*:\s*[\"']?(\w+)", document)
        if api_version and kind:
            yield api_version.group(1), kind.group(1)


def check_manifests(paths, hops, current, report):
    files = 0
    found = {}
    try:
        for file in manifest_files(paths):
            files += 1
            try:
                text = file.read_text(errors="replace")
            except OSError as error:
                report.failures.append(f"{file}: {error}")
                continue
            for api_version, kind in manifest_kinds(text):
                for removed, version, _, replacement in removals_for(api_version, kind):
                    if removed in hops or (current is not None and removed <= current):
                        found.setdefault((removed, version, kind, replacement), []).append(str(file))
    except ReadError as error:
        report.failures.append(f"manifests: {error}")
    report.coverage.append(f"{files} manifest files, matching top-level apiVersion and kind. Rendered Helm charts are more reliable than templates.")
    for (removed, version, kind, replacement), names in sorted(found.items()):
        hop = removed if removed in hops else 0
        when = f"removed in 1.{removed}" if removed in hops else f"already removed in 1.{removed}"
        report.findings.append(Finding("BLOCKER", "api-in-manifest", f"{kind} uses {version}, {when}: {named(names)}. Change it to {replacement}.", hop))


# --- cluster readiness ---


def check_nodes(cluster, current, hops, report):
    nodes = cluster.items("nodes", namespaced=False)
    if nodes is None:
        return
    report.coverage.append(f"{len(nodes)} nodes: readiness and kubelet versions.")
    not_ready = []
    for node in nodes:
        conditions = node.get("status", {}).get("conditions", [])
        if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            not_ready.append(ref(node))
        kubelet_text = node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion", "")
        try:
            kubelet = parse_minor(kubelet_text)
        except ValueError:
            report.failures.append(f"node {ref(node)}: unreadable kubelet version")
            continue
        if kubelet > current:
            report.findings.append(Finding("BLOCKER", "kubelet-skew", f"Node {ref(node)} runs kubelet 1.{kubelet}, newer than the API server at 1.{current}. That isn't supported."))
        for hop in hops:
            allowed = KUBELET_MAX_SKEW if kubelet >= 25 else KUBELET_MAX_SKEW_OLD
            if hop - kubelet > allowed:
                report.findings.append(Finding("BLOCKER", "kubelet-skew", f"Node {ref(node)} runs kubelet 1.{kubelet}, too old for an API server at 1.{hop}. Upgrade this node first.", hop))
                break
    if not_ready:
        report.findings.append(Finding("BLOCKER", "node-not-ready", f"Nodes not Ready: {named(not_ready)}."))


def check_kubectl_skew(version, hops, report):
    text = version.get("clientVersion", {}).get("gitVersion", "")
    try:
        client = parse_minor(text)
    except ValueError:
        return
    too_far = [hop for hop in hops if abs(hop - client) > KUBECTL_MAX_SKEW]
    if too_far:
        report.findings.append(Finding("WARNING", "kubectl-skew", f"This kubectl is 1.{client}, more than one minor from 1.{too_far[0]}. Use a matching kubectl for that step.", too_far[0]))


def check_disruption_budgets(cluster, report):
    budgets = cluster.items("poddisruptionbudgets")
    if budgets is None:
        return
    report.coverage.append(f"{len(budgets)} PodDisruptionBudgets.")
    blocked = [ref(item) for item in budgets if item.get("status", {}).get("disruptionsAllowed") == 0 and item.get("status", {}).get("expectedPods", 0) > 0]
    if blocked:
        report.findings.append(Finding("BLOCKER", "pdb-blocks-drain", f"PodDisruptionBudgets allowing 0 disruptions right now, so draining their nodes will hang: {named(blocked)}."))


def ready_services(slices):
    ready = set()
    for item in slices:
        service = item.get("metadata", {}).get("labels", {}).get("kubernetes.io/service-name")
        for endpoint in item.get("endpoints") or []:
            if service and endpoint.get("conditions", {}).get("ready") is not False:
                ready.add((item["metadata"].get("namespace"), service))
    return ready


def check_webhooks(cluster, report):
    slices = cluster.items("endpointslices.discovery.k8s.io")
    mutating = cluster.items("mutatingwebhookconfigurations", namespaced=False)
    validating = cluster.items("validatingwebhookconfigurations", namespaced=False)
    if slices is None or mutating is None or validating is None:
        return
    ready = ready_services(slices)
    configs = mutating + validating
    report.coverage.append(f"{len(configs)} webhook configurations.")
    broken = []
    for config in configs:
        for webhook in config.get("webhooks", []):
            service = webhook.get("clientConfig", {}).get("service")
            if webhook.get("failurePolicy", "Fail") == "Fail" and service and (service.get("namespace"), service.get("name")) not in ready:
                broken.append(f"{ref(config)} ({webhook.get('name')})")
    if broken:
        report.findings.append(Finding("BLOCKER", "webhook-no-backend", f"Webhooks that fail closed and have no ready backend, so matching API requests fail during the upgrade: {named(broken)}."))


def check_crds(cluster, report):
    crds = cluster.items("customresourcedefinitions", namespaced=False)
    if crds is None:
        return
    report.coverage.append(f"{len(crds)} CustomResourceDefinitions. Whether their controllers support the target version isn't checked.")
    stale = []
    for crd in crds:
        served = {version.get("name") for version in crd.get("spec", {}).get("versions", []) if version.get("served")}
        unserved = [version for version in crd.get("status", {}).get("storedVersions", []) if version not in served]
        if unserved:
            stale.append(f"{ref(crd)} ({', '.join(unserved)})")
    if stale:
        report.findings.append(Finding("WARNING", "crd-stored-version", f"CRDs with stored versions no longer served. Migrate that data: {named(stale)}."))


def check_workloads(cluster, report):
    deployments = cluster.items("deployments")
    if deployments is not None:
        single = [ref(item) for item in deployments if item.get("spec", {}).get("replicas") == 1 and item["metadata"].get("namespace") != "kube-system"]
        if single:
            report.findings.append(Finding("WARNING", "single-replica", f"Deployments with one replica have downtime when their node drains: {named(single)}."))
    pods = cluster.items("pods")
    if pods is not None:
        report.coverage.append(f"{len(pods)} pods, their owners and emptyDir volumes.")
        local_data = [
            ref(item) for item in pods
            if (item.get("spec") or {}).get("nodeName")
            and (item.get("status") or {}).get("phase") not in ("Succeeded", "Failed")
            and "kubernetes.io/config.mirror" not in (item["metadata"].get("annotations") or {})
            and not any((owner or {}).get("kind") == "DaemonSet" and (owner or {}).get("controller")
                        for owner in item["metadata"].get("ownerReferences") or [])
            and any("emptyDir" in (volume or {}) for volume in (item.get("spec") or {}).get("volumes") or [])
        ]
        if local_data:
            report.findings.append(Finding("WARNING", "emptydir-data-loss", f"Pods using emptyDir: {named(local_data)}. Their data is lost when the pod is removed. Review that data before allowing drain to delete it with --delete-emptydir-data."))
        unowned = [ref(item) for item in pods if not item["metadata"].get("ownerReferences") and "kubernetes.io/config.mirror" not in (item["metadata"].get("annotations") or {})]
        if unowned:
            report.findings.append(Finding("WARNING", "unowned-pod", f"Pods with no controller. `kubectl drain` refuses them without --force, and they won't come back: {named(unowned)}."))


# --- output ---


def verdict(report):
    if any(f.severity == "BLOCKER" for f in report.findings):
        return "BLOCKED"
    if report.failures:
        return "INCOMPLETE"
    return "NO BLOCKERS FOUND"


def by_hop(report):
    general = [f for f in report.findings if not f.hop]
    per_hop = {hop: [f for f in report.findings if f.hop == hop] for hop in report.hops}
    return general, per_hop


def render_text(report):
    lines = [f"Upgrade plan: {report.current or 'unknown'} to {report.target}: {verdict(report)}"]
    if report.context:
        lines.append(f"Cluster context: {clean(report.context, 80)}")
    general, per_hop = by_hop(report)
    if general:
        lines += ["", "Fix before starting:"]
        lines.extend(f"  {f.severity:<8} {f.check}: {f.summary}" for f in general)
    for hop, findings in per_hop.items():
        lines += ["", f"Step to 1.{hop}:"]
        lines.extend(f"  {f.severity:<8} {f.check}: {f.summary}" for f in findings)
        lines.append("  Upgrade the control plane, then node pools. Read the urgent upgrade notes:")
        lines.append(f"  {CHANGELOG.format(minor=hop)}")
    lines += ["", "What this read:"]
    lines.extend(f"  {item}" for item in report.coverage)
    lines.extend(f"  COULDN'T READ {clean(item)}" for item in report.failures)
    lines += ["", f"Not checked: add-on and controller compatibility, cloud provider rules, and behavior changes outside API removals. Removal table checked {TABLE_CHECKED} against {GUIDE}"]
    return "\n".join(lines)


def render_markdown(report):
    lines = [f"# Upgrade plan: {report.current or 'unknown'} to {report.target}", "", f"**Result:** {verdict(report)}"]
    if report.context:
        lines.append(f"**Cluster context:** `{clean(report.context, 80)}`")
    general, per_hop = by_hop(report)
    if general:
        lines += ["", "## Fix before starting", ""]
        lines.extend(f"- [ ] **{f.severity}** `{f.check}`: {f.summary}" for f in general)
    for hop, findings in per_hop.items():
        lines += ["", f"## Step to 1.{hop}", ""]
        lines.extend(f"- [ ] **{f.severity}** `{f.check}`: {f.summary}" for f in findings)
        lines.append(f"- [ ] Read the [1.{hop} urgent upgrade notes]({CHANGELOG.format(minor=hop)})")
        lines.append("- [ ] Upgrade the control plane, then node pools")
    lines += ["", "## What this read", ""]
    lines.extend(f"- {item}" for item in report.coverage)
    lines.extend(f"- **Couldn't read** {clean(item)}" for item in report.failures)
    lines += ["", "Not checked: add-on and controller compatibility, cloud provider rules, and behavior changes outside API removals."]
    return "\n".join(lines)


def render_json(report):
    data = asdict(report)
    data["verdict"] = verdict(report)
    data["hops"] = [f"1.{hop}" for hop in report.hops]
    return json.dumps(data, indent=2)


RENDERERS = {"text": render_text, "markdown": render_markdown, "json": render_json}


def plan(args):
    target = parse_minor(args.to)
    report = Report(target=f"1.{target}")
    current = parse_minor(args.current) if args.current else None
    cluster = None
    version = {}
    if args.kube:
        cluster = Cluster(args.context, report)
        try:
            version = cluster.json("version", "-o", "json")
            current = parse_minor(version.get("serverVersion", {}).get("gitVersion", ""))
            report.context = args.context or run(cluster.base + ["config", "current-context"]).strip()
        except (ReadError, ValueError) as error:
            report.failures.append(f"cluster version: {error}")
            return report
    if current is None:
        raise ValueError("--current is needed when --kube isn't used")
    if target <= current:
        raise ValueError(f"The target 1.{target} isn't newer than 1.{current}")
    report.current = f"1.{current}"
    report.hops = hops_between(current, target)
    if cluster:
        check_kubectl_skew(version, report.hops, report)
        check_nodes(cluster, current, report.hops, report)
        check_removal_metric(cluster, report.hops, report)
        check_last_applied(cluster, report.hops, report)
        check_disruption_budgets(cluster, report)
        check_webhooks(cluster, report)
        check_crds(cluster, report)
        check_workloads(cluster, report)
    if args.manifests:
        check_manifests(args.manifests, report.hops, current, report)
    return report


def build_parser():
    parser = argparse.ArgumentParser(description="Plan a Kubernetes minor version upgrade and find what would block it. Read-only.")
    parser.add_argument("--to", required=True, help="target version, such as 1.33")
    parser.add_argument("--kube", action="store_true", help="read the cluster with kubectl")
    parser.add_argument("--context", help="kubectl context (default: the current one)")
    parser.add_argument("--current", help="current version, when --kube isn't used")
    parser.add_argument("--manifests", action="append", default=[], metavar="PATH", help="file or folder of manifests; repeatable")
    parser.add_argument("--format", choices=sorted(RENDERERS), default="text")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.kube or args.manifests):
        parser.error("choose at least one source: --kube or --manifests")
    try:
        report = plan(args)
    except ValueError as error:
        parser.error(str(error))
    print(RENDERERS[args.format](report))
    return EXIT_READY if verdict(report) == "NO BLOCKERS FOUND" else EXIT_ATTENTION


if __name__ == "__main__":
    sys.exit(main())
