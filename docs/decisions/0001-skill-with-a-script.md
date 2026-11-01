# 0001: A skill with one standard-library script

**Decision:** ship a Claude Code skill and one Python script that uses the user's `kubectl`.

**Why:** installing it needs no server or package, and it inherits the access the user already has. An upgrade plan is also a one-off task, not something an agent calls constantly.

**Alternatives:** an MCP server would be easier to list in registries, but it adds an install and a process to run. Wrapping `pluto` or `kube-no-trouble` would add required dependencies, so they stay optional.
