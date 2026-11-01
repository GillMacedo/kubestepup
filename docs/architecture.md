# Architecture

    plan --> version, then hops_between --> one step per minor
    check_* --> findings (BLOCKER or WARNING, with the step they affect) + coverage notes
    verdict --> BLOCKED, INCOMPLETE or NO BLOCKERS FOUND
    render_* --> text, Markdown or JSON

- **Each check** reads what it needs. A failed read goes in `failures`, and the other checks continue.
- **`REMOVALS`** comes from the Kubernetes deprecation guide. The API server's metric also reports removals missing from the table.
- **The skill** tells Claude to give the result first, say what wasn't checked, and leave actions to the user.

Key choice: a skill with one standard-library script, not an MCP server. See `decisions/`.
