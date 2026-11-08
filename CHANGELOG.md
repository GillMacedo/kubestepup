# Changelog

All notable changes are documented here, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. This project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-08

### Added
- Warn about data loss from drainable pods using `emptyDir` volumes, excluding unscheduled, finished, DaemonSet-managed and mirror pods; tolerate null optional pod fields.

## [0.1.1] - 2026-11-01

### Fixed
- Include `.yaml.tpl` files when scanning manifest directories; cover Windows line endings in multi-document manifests.

## [0.1.0] - 2026-11-01

### Added
- Upgrade plan with one step per minor version, in text, Markdown and JSON.
- Checks for removed APIs in use, in last-applied objects and in manifests.
- Checks for kubelet and kubectl skew, node readiness, PodDisruptionBudgets, webhooks, CRD stored versions, single-replica Deployments and unowned pods.
- `planning-kubernetes-upgrades` skill and plugin manifest.
