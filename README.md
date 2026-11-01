# kubestepup: Kubernetes upgrade planner and blocker check

[![CI](https://github.com/GillMacedo/kubestepup/actions/workflows/ci.yml/badge.svg)](https://github.com/GillMacedo/kubestepup/actions/workflows/ci.yml) [![MIT license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**A Claude Code skill and read-only CLI that plans a Kubernetes upgrade one minor version at a time, and finds what would block it: removed APIs in use, kubelet version skew, PodDisruptionBudgets that hang drains, and fail-closed webhooks with no backend.**

Tools like [kube-no-trouble](https://github.com/doitintl/kube-no-trouble) and [pluto](https://github.com/FairwindsOps/pluto) are good at finding deprecated APIs. An upgrade can also stall on a drain, a broken webhook or an old kubelet. `kubestepup` checks those together and gives you an ordered plan for each step.

## What you can ask Claude

- "Can I upgrade this cluster to 1.33?"
- "Which manifests in ./deploy break if we go from 1.31 to 1.32?"
- "What would block a node drain right now?"
- "Write me an upgrade checklist for this cluster."

## Install

As a Claude Code plugin:

    claude plugin marketplace add GillMacedo/kubestepup
    claude plugin install kubestepup@kubestepup-marketplace

Or run the script directly. It needs Python 3.10 or newer, with nothing from pip, plus your own `kubectl` if you read a cluster:

    git clone https://github.com/GillMacedo/kubestepup
    python3 kubestepup/plugin/scripts/kubestepup.py --to 1.33 --kube --context NAME
    python3 kubestepup/plugin/scripts/kubestepup.py --current 1.31 --to 1.33 --manifests ./deploy

## Example

On made-up data, through the stand-in `kubectl` in `tests/fakes`:

    $ make demo
    Upgrade plan: 1.31 to 1.33: BLOCKED
    Cluster context: demo-cluster

    Fix before starting:
      BLOCKER  node-not-ready: Nodes not Ready: node-c.
      BLOCKER  pdb-blocks-drain: PodDisruptionBudgets allowing 0 disruptions right now, so draining their nodes will hang: shop/checkout.
      BLOCKER  webhook-no-backend: Webhooks that fail closed and have no ready backend, so matching API requests fail during the upgrade: policy (down.example.com), policy (gone.example.com).
      WARNING  crd-stored-version: CRDs with stored versions no longer served. Migrate that data: widgets.example.com (v1alpha1).
      WARNING  single-replica: Deployments with one replica have downtime when their node drains: shop/checkout.
      WARNING  unowned-pod: Pods with no controller. `kubectl drain` refuses them without --force, and they won't come back: shop/debug.
      BLOCKER  api-in-manifest: CronJob uses batch/v1beta1, already removed in 1.25: examples/manifests/app.yaml. Change it to batch/v1.

    Step to 1.32:
      BLOCKER  api-in-use: Something is calling flowcontrol.apiserver.k8s.io/v1beta3 flowschemas, removed in 1.32. Find the client and move it.
      BLOCKER  api-applied: FlowSchema last applied as flowcontrol.apiserver.k8s.io/v1beta3, removed in 1.32: demo-flow. Update the source manifests to flowcontrol.apiserver.k8s.io/v1.
      BLOCKER  api-in-manifest: FlowSchema uses flowcontrol.apiserver.k8s.io/v1beta3, removed in 1.32: examples/manifests/app.yaml. Change it to flowcontrol.apiserver.k8s.io/v1.
      Upgrade the control plane, then node pools. Read the urgent upgrade notes:
      https://github.com/kubernetes/kubernetes/blob/master/CHANGELOG/CHANGELOG-1.32.md

    Step to 1.33:
      WARNING  kubectl-skew: This kubectl is 1.31, more than one minor from 1.33. Use a matching kubectl for that step.
      BLOCKER  kubelet-skew: Node node-b runs kubelet 1.29, too old for an API server at 1.33. Upgrade this node first.
      Upgrade the control plane, then node pools. Read the urgent upgrade notes:
      https://github.com/kubernetes/kubernetes/blob/master/CHANGELOG/CHANGELOG-1.33.md

    What this read:
      3 nodes: readiness and kubelet versions.
      Requests to deprecated APIs, from one API server's metrics since it last started. Clients that haven't called it since then won't show.
      Objects' last-applied apiVersion, for objects applied with kubectl. Helm releases are stored in Secrets and aren't read.
      3 PodDisruptionBudgets.
      1 webhook configurations.
      2 CustomResourceDefinitions. Whether their controllers support the target version isn't checked.
      3 pods and their owners.
      1 manifest files, matching top-level apiVersion and kind. Rendered Helm charts are more reliable than templates.

    Not checked: add-on and controller compatibility, cloud provider rules, and behavior changes outside API removals. Removal table checked 2026-11-01 against https://kubernetes.io/docs/reference/using-api/deprecation-guide/

Add `--format markdown` for a checklist, or `--format json` to process the results. Exit code 0 means no blockers were found and everything was read, 1 means there are blockers or something couldn't be read, and 2 means bad options.

## What it checks

| Check | Result | How |
|---|---|---|
| `api-in-use` | Blocker | The API server's `apiserver_requested_deprecated_apis` metric, which also covers CRDs and removals newer than this tool's table |
| `api-applied` | Blocker | Objects' `last-applied-configuration` annotation |
| `api-in-manifest` | Blocker | Top-level `apiVersion` and `kind` in your files |
| `kubelet-skew` | Blocker | [Version skew policy](https://kubernetes.io/releases/version-skew-policy/): kubelet can't be newer than the API server, or more than 3 minors older |
| `node-not-ready` | Blocker | Node conditions |
| `pdb-blocks-drain` | Blocker | PodDisruptionBudgets with pods and 0 disruptions allowed |
| `webhook-no-backend` | Blocker | Fail-closed webhooks whose service has no ready endpoint |
| `crd-stored-version` | Warning | Stored CRD versions that are no longer served |
| `single-replica` | Warning | Deployments with one replica, outside `kube-system` |
| `unowned-pod` | Warning | Pods with no controller, which `kubectl drain` refuses without `--force` |
| `kubectl-skew` | Warning | A `kubectl` more than one minor from a step |

## Safety

- **Read-only:** it only runs `kubectl version`, `config current-context` and `get`. It can't drain, upgrade or edit anything.
- **No Secrets:** it never requests them. Helm stores releases in Secrets, so Helm releases aren't read.
- **No data leaves your machine** beyond your own `kubectl` calls. There's no telemetry.

## Limits

- "No blockers found" doesn't mean an upgrade is safe. It doesn't check add-ons and controllers such as CNI, ingress or cert-manager, cloud provider rules, or behavior changes outside API removals. Each step links its urgent upgrade notes.
- The metric comes from one API server, since it last started, and clients that haven't called a deprecated API since then won't show.
- The last-applied check only sees objects applied with `kubectl`. For Helm, use `pluto` or `kube-no-trouble`.
- The PodDisruptionBudget check reflects this moment, and pod health can change before you drain.
- The removal table covers the [deprecation guide](https://kubernetes.io/docs/reference/using-api/deprecation-guide/) as of 2026-11-01. Later removals only show through the metric.

## FAQ

**How do I upgrade Kubernetes across several versions?** One minor at a time. The API server can't skip minors, so `kubestepup` lists every step and what affects each one.

**How do I find deprecated Kubernetes APIs in use?** Run it with `--kube`. It reads the API server's own metric and objects' last-applied versions.

**Why does my node drain hang?** Often a PodDisruptionBudget allows 0 disruptions. `pdb-blocks-drain` lists them.

**Does it replace kube-no-trouble or pluto?** No. They cover Helm releases and more manifest formats. Use them together.

**Is it an MCP server?** No. It's a skill with a bundled script, so it needs no server or package install.

## Tested on

Unit tests use a stand-in `kubectl`. The removed API, PodDisruptionBudget, webhook and single-replica checks also ran on a real k3s 1.31 cluster, and k3s 1.32 refused the removed API as the table says. It hasn't been run on a managed cloud cluster or a real production upgrade yet.

## Contributing

Contributions are welcome, from people and from people working with AI agents. See [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are [listed here](https://github.com/GillMacedo/kubestepup/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).

### How pull requests work

1. **Open a PR.** Fork the repo, work on a branch, and keep it to one change. Write a title that says what changes for users, because it becomes the changelog line: `feat:` goes under Added and `fix:` under Fixed.
2. **Automatic check.** Code changes need tests, and `make test` must pass. Changes over 30 lines need a linked open issue (`Closes #<number>`), and a PR can have at most 800 changed lines. If something is missing, a comment tells you what to fix.
3. **Review.** Claude reviews each PR automatically. Code PRs also get a security review and are tested in a sandbox. The review then merges the PR or asks for changes. PRs that change CI, agent settings, dependencies or build files go to the maintainer, as does anything the security review doesn't clear.
4. **Release.** A merge that changes what users install becomes a new version within about 20 minutes, with a GitHub Release and, if the project has one, its npm package. Docs and CI changes wait for the next release.

## AI disclosure

This project was largely generated with Claude Code and reviewed by its maintainer.

## About me

I'm Giulliano Macedo, and I build developer tools at GoTech Solutions. Follow along or get in touch:

- GitHub: [GillMacedo](https://github.com/GillMacedo)
- LinkedIn: [giulliano](https://www.linkedin.com/in/giulliano/)
- X: [@giumacedo](https://x.com/giumacedo)

If `kubestepup` helped with an upgrade, a star helps others find it.

---

Built by [Giulliano Macedo](https://github.com/GillMacedo), GoTech Solutions · [LinkedIn](https://www.linkedin.com/in/giulliano/) · [X](https://x.com/giumacedo)
