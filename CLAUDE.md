# kubestepup

Claude Code skill and CLI that plans a Kubernetes minor version upgrade and finds what would block it.

## Architecture
`plugin/scripts/kubestepup.py` holds all the logic: each check adds findings and coverage notes to a report, then a renderer prints it. `plugin/skills/planning-kubernetes-upgrades/SKILL.md` tells Claude when and how to run it. See `docs/architecture.md`.

## Commands
- `make test`: unit tests and project checks
- `make lint`: manifest validation
- `make demo`: run on made-up data through `tests/fakes`

## Conventions
- Python 3.10+, standard library only.
- Commands must stay read-only, and must never request Secrets. A test checks this.
- Every rule cites the Kubernetes docs. Link the doc in the PR when adding or changing one.
- Something that can't be read must show as a failure, never as a pass.

## Do and don't
- Do pass outside text through `clean`.
- Don't add network calls of its own or telemetry.
- Don't say an upgrade is safe.

## Definition of done
`make test` and `make lint` pass, and `ROADMAP.md` and docs are updated when relevant.
