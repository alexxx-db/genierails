# Central governance, self-service Genie (moved)

A central governance team with business teams that self-serve their own agents
is now the persona split: one repo, per-agent files, and a CODEOWNERS rule per
team.

- **Business teams:** [agents playbook](playbooks/agents.md)
- **Central governance team:** [governance playbook](playbooks/governance.md)
- **Who owns which file and CODEOWNERS:** [pipelines.md](pipelines.md#who-owns-which-file)
- **Legacy `MODE=governance` / `MODE=genie`, `apply-genie` and `genie_only`:** [legacy mode](legacy-mode.md#legacy-commands)

<a id="when-to-use-this-pattern"></a>
- **When to use this pattern:** [legacy mode](legacy-mode.md#legacy-commands) (legacy); deterministic: [who owns which file](pipelines.md#who-owns-which-file)
<a id="roles-and-responsibilities"></a>
- **Roles:** [agents](playbooks/agents.md) and [governance](playbooks/governance.md)
<a id="architecture"></a>
- **Architecture:** [architecture.md](architecture.md) and [who owns which file](pipelines.md#who-owns-which-file)
<a id="least-privilege-service-principal-for-bu-teams"></a>
- **Least-privilege SP for BU teams:** [genie-only (legacy)](legacy-mode.md#genie-only)
<a id="git-repository-strategies"></a>
- **Git / CODEOWNERS:** [pipelines.md](pipelines.md#who-owns-which-file)
<a id="promotion-in-self-service-genie-mode"></a>
- **Promotion:** [agents playbook — promote](playbooks/agents.md#promote-to-prod); legacy: [legacy-mode.md](legacy-mode.md#legacy-commands)
<a id="cicd-integration"></a>
- **CI/CD:** [pipelines.md](pipelines.md)
<a id="faq"></a>
- **FAQ / legacy commands:** [legacy-mode.md](legacy-mode.md#legacy-commands)
