# Microsoft Copilot Harness for Foundry Agent Service

Build agents that research companies, analyze changing business evidence and
deliver files your application can use, without implementing the agent
execution loop yourself.

Start with company research using Web IQ. Then explore a retail analyst and
compare how model and instruction choices affect agent behavior.

These agents use the Microsoft Copilot harness public-preview authoring
contract. See the [official setup guide](https://learn.microsoft.com/azure/foundry/agents/how-to/use-tools-skills-copilot-harness?pivots=azd).

## Choose an example

| Example | What you build | What you learn |
|---------|----------------|----------------|
| **[Company research with Web IQ](agents/company-research/README.md)** | An agent that researches a public company, runs illustrative calculations and delivers a cited brief | Connect research tools, attach a Skill and work with generated files |
| **[Retail analyst](agents/retail-analysis/README.md)** | An agent that recalculates transaction data and updates its assessment as evidence arrives | Combine code execution, interpretation and explicit revisions to earlier conclusions |
| **[Model comparison](agents/model-comparison/README.md)** | A neutral agent configuration evaluated with different model bindings | Compare task completion, output formats, execution behavior, latency and reported usage |

Company research is the recommended starting point. Its outputs are easy to
inspect: a research brief, a source ledger and calculation results.

## Start with company research

1. [Install the tools](agents/company-research/README.md#install-and-check-the-tools).
2. Follow the [setup and deployment guide](agents/company-research/README.md#deploy-and-run)
   using your existing Foundry project and model deployment.
3. Run a research request from the command line and open its three downloaded
   files. The [output previews](agents/company-research/output-preview/README.md)
   show what to expect before you deploy.

The previews use fictional research content and reproducible scenario values.
Actual run outputs, responses and logs stay under `.local/`, which is excluded
by [.gitignore](.gitignore). Do not move those run directories into source.

All three agent definitions, local configuration/preparation tools, runners
and offline tests are available. Each guide separates local checks from Azure
deployment and paid invocation.

## Analyze retail evidence

Follow the [retail guide](agents/retail-analysis/README.md) to prepare real
historical transaction snapshots and run an assessment that updates as each
new week arrives. It includes a same-evidence check and a separate read-only
audit of the downloaded responses and files.

Retail does not need Web IQ or a published Skill. Full workbook preparation
uses the pinned dependency in [requirements-retail.txt](requirements-retail.txt);
the agent itself calculates with Python's standard library.

## Compare model configurations

The [model-comparison guide](agents/model-comparison/README.md) runs six fixed
tasks three times per ready model, using identical agent instructions and
enabled tools. You supply existing model-deployment names and an explicit
request budget.

The grader tests downloaded code inside restricted Docker containers. Reports
separate correctness, requested delivery format, failures, latency and reported
usage. No Web IQ connection is required.

The [verified comparison results](agents/model-comparison/RESULTS.md) distinguish
task-output mistakes from client interruptions and show where model choices
changed the result.

## How it works

The harness supplies the interaction loop between a model and its available
capabilities. You configure the task, tools and reusable Skills. The agent can
inspect results, choose further actions, execute code and deliver artifacts.

```text
Your request
    |
Configured agent: instructions + model + Copilot harness
    |
Connected tools / files / code execution / optional Skills
    |
Answer + downloadable artifacts
    |
Application checks and human review
```

In the company-research example, Web IQ supplies public research tools. A Skill
packages research instructions, a Python calculator and illustrative assumptions.
The agent combines retrieved evidence with those calculations and delivers the
results as files. The scenarios describe the supplied assumptions, not the
company's actual financial performance.

The retail analyst works with supplied transaction files rather than web
research. As additional evidence arrives, it recalculates the figures and
identifies which earlier conclusions to retain, revise or withdraw.

The model comparison keeps the scored tasks, inputs and instructions fixed
while changing the model binding. It helps you evaluate the configuration your
application will use.

### Copilot harness or a custom Foundry Hosted Agent?

Both use managed service infrastructure. They differ in the agent application
you supply:

| Copilot harness agent | Custom Foundry Hosted Agent |
|-----------------------|-----------------------------|
| Configure an agent around the supplied harness | Supply an agent application using your chosen framework or orchestration |
| Use the harness's supported execution behavior | Implement or integrate your application's execution behavior |
| Focus on the task, model, tools, Skills and outputs | Control the custom application's implementation and behavior |

For these harness examples, the agent definition selects a model and the
managed harness. There is no custom agent server or container image to build.
Local scripts configure and invoke the agent, download files and check results.

## Before you start

Use an existing Foundry project. A project does not necessarily contain a model
deployment: reuse a supported deployment or deploy a model before creating the
agent.

### Public-preview compatibility

- Use azd 1.37.0+ and the agent extension 1.0.0-beta.22+. The
  [installation guide](agents/company-research/README.md#install-and-check-the-tools)
  covers the bundle, versions and shell preview opt-in.
- Set `AZD_ALPHA_ENABLE_AGENTS_MANAGED_HARNESS=true` in each shell or CI job
  that runs harness deployment commands.
- New templates use `microsoft_copilot_preview`. The shared client also
  accepts existing `github_copilot_preview` agents without rewriting them.
  Harness selection is fixed at creation during this preview; use a distinct
  new name rather than changing the harness of an existing agent.
- Copilot toolset settings use `defaultConfig` in `azure.yaml` and
  `default_config` in persisted REST definitions. The comparison checks
  normalize only that field and still verify the exact tool capabilities.
- Web IQ's authored MCP `allowed_tools` list can be persisted as
  `{"tool_names": [...]}`. Definition checks normalize that representation
  while still requiring exactly the same allowed tools and approval setting.
- The shared REST client sends `Foundry-Features: MicrosoftCopilot=V1Preview`.
  Responses remain agent-scoped, with explicit conversations and pinned versions.

These are compatibility updates, not changes to task instructions, tool
permissions, Web IQ, Skill content, data policies or grading.

You will need:

- Python 3.12+, Git, Azure CLI (`az`) and Azure Developer CLI (`azd`).
- A Foundry project in a region supporting Copilot harness agents.
- A model deployment supported by the harness.
- Permission to create agents and register Skills in that project.
- Foundry data-plane access for the caller and project-scoped file access for
  the agent's instance identity.
- For company research, Web IQ access and a project connection.

Web IQ has its own setup and terms. It is not automatically provisioned with
the harness, and it is not required for retail or model comparison.

Disabling a harness Web tool is not network isolation when Shell is enabled.
Task instructions constrain intended behavior, not network egress. Foundry
supports [separate egress policies](https://learn.microsoft.com/azure/foundry/agents/how-to/use-tools-skills-copilot-harness#control-network-egress);
these agents do not create or attach one.

Model deployment and agent deployment are separate operations. The agent uses
the deployment name you supply; it should not silently create or resize a model.
Review model availability, quota, deployment type and capacity before creating
a deployment.

Running an agent can incur model usage charges. Company research can also incur
Web IQ usage charges. Use public information for the company-research example
and keep credentials in the project connection or an approved secret store.

## Run the offline checks

From the repository root:

```bash
python3 skills/company-research/calculate_scenarios.py
python3 -m unittest discover -s tests -v
```

These commands use only the Python standard library. The tests mock remote
operations; they do not sign in, deploy resources or invoke an agent.

## Public-preview verification

The compatibility update was physically checked on **October 28, 2026** using
azd 1.37.1, Foundry bundle 1.0.0-beta.4, agent extension 1.0.0-beta.22,
projects 1.0.0-beta.15 and Skills 1.0.0-beta.9.

| Agent | Actual verification |
|-------|---------------------|
| Company research | One Web IQ request; existing Skill and calculator executed; all three delivered files independently re-fetched and checked |
| Retail analysis | Four real stages; arithmetic, source-cell and claim-update checks; all eight delivered files independently re-fetched |
| Model comparison | One readiness and one implementation request with Sol6.1; delivered code passed 43/43 Docker checks; both responses and their files independently audited |

These were **seven requests with no client resubmissions**, against three
distinct new Microsoft-harness agents in the existing projects. Existing
agents and model deployments were preserved. Web IQ, Skill content, task
instructions, data policies, fixtures and graders were unchanged.

The checks used an owner-created clean Git-only export of
[compatibility source commit 150bcec](https://github.com/AshuJoshi/foundry-copilot-harness/commit/150bceca63fa762d09549043a846e4ce5f03c05f),
which also passed **132 offline tests without skips**, including the actual
Docker grader controls. Research was initially deployed from the preceding
compatibility commit and verified against this corrected source without a
redeployment; its definition was unchanged. The correction handles the API's
persisted MCP allow-list representation, not a tool-permission change.

Retail reused previously prepared, pinned data after verifying its hashes and
references; this check did not download or rebuild the workbook. The tooling
was upgraded in an existing environment, not installed on a fresh machine.
These checks do not rerun or supersede the
[dated nine-model battery](agents/model-comparison/RESULTS.md). Mechanical
output checks do not certify every narrative claim.

Raw verification responses, reports and logs remain under ignored `.local/`.
Only this authored verification summary belongs in source control.

## License

Source code and documentation are available under the [MIT license](LICENSE).
The retail dataset has its own [CC BY 4.0 attribution and data notice](agents/retail-analysis/DATA-NOTICE.txt).
Azure, model and Web IQ service access and terms are separate from this source license.
