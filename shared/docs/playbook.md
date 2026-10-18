# Playbook (moved)

The playbook is now one page per persona:

- **[Agents](playbooks/agents.md)** (data citizen): add, update, promote and remove an agent.
- **[Governance](playbooks/governance.md)** (governance engineer): masks, classes, tiers, `table_readers`, row filters, `ungovern`, acknowledgements; also [governing tables with no agents](playbooks/governance.md#govern-tables-only).
- **[One team](playbooks/one-team.md)**: both kinds of change in one PR.
- **[Deployment machine](deployment-machine.md)**: releasing prod, `maintain`, operations, tear down.

<a id="add-another-genie-agent"></a>
- **Add another Genie agent:** [agents](playbooks/agents.md#add-an-agent)
<a id="promote-dev--prod"></a>
- **Promote dev to prod:** [agents](playbooks/agents.md#promote-to-prod) and [governance](playbooks/governance.md#the-whole-flow)
<a id="schema-drift-detection"></a>
- **Schema drift:** [governance](playbooks/governance.md#keep-it-safe-between-changes)
<a id="abac-governance-only-no-genie-agent"></a>
- **Governance only (a central team owns the table rules):** [governance](playbooks/governance.md#govern-tables-only)
<a id="independent-bu-environment"></a>
- **Independent BU environment:** [advanced](advanced.md#independent-environments)
<a id="destroy-and-reset"></a>
- **Destroy and reset:** [deployment machine](deployment-machine.md#tear-down)
<a id="country-and-industry-overlays"></a>
<a id="central-governance-self-service-genie"></a>
<a id="import-genie-agent-to-prod-without-abac"></a>
<a id="advanced-scenarios"></a>
- **Overlays, `MODE=governance` / `MODE=genie`, and other legacy commands:** [legacy mode](legacy-mode.md#legacy-commands)
