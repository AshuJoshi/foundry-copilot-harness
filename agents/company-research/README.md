# Company research with Web IQ

Give the agent a public company name, website and research question. It uses
Web IQ to gather evidence, runs a packaged calculator and delivers a cited
brief, source ledger and scenario results.

Use this example to learn how a Copilot harness agent combines connected
research, reusable instructions, code execution and file delivery.

## What the agent produces

| File | Contents |
|------|----------|
| `report.md` | Research findings, citations, competing explanations and follow-up questions |
| `evidence.json` | Retrieved sources with URLs, attribution and retrieval timestamps |
| `scenarios.json` | Calculator assumptions and results, labeled as illustrative |

### Preview the files before deploying

Open the [authored output previews](output-preview/README.md):
[brief](output-preview/report.md),
[source-ledger structure](output-preview/evidence.json) and
[calculator results](output-preview/scenarios.json).

The research preview uses fictional company details and reserved example URLs;
it is not a saved research run. The scenario preview matches the bundled
calculator. Real requests produce different research findings and sources.

Run outputs, logs and response records belong under `.local/`, which is
excluded by [.gitignore](../../.gitignore). Keep those separate from the
documentation previews. The runner's default output location is already under
`.local/`; retain that convention if you supply `--output-dir`.

The caller supplies the company, public website and question for each request.
The agent definition does not contain a fixed company.

For example:

> Research the company's product and competitors. Which differentiators have
> independent support, and what should I investigate next?

## Files in this example

| Source | Purpose |
|--------|---------|
| [azure.yaml](azure.yaml) | Agent definition, Web IQ connection reference and Skill version |
| [Research Skill](../../skills/company-research/SKILL.md) | Research procedure, evidence requirements and deliverables |
| [Calculator](../../skills/company-research/calculate_scenarios.py) | Decimal-based illustrative scenario calculations |
| [Assumptions](../../skills/company-research/assumptions.json) | Downside, base and upside inputs |
| [Configuration helper](../../scripts/configure_company_research.py) | Fill in the template without calling Azure |
| [Research runner](../../scripts/run_company_research.py) | Invoke one request, check execution and download the files |
| [Shared client](../../harness_client.py) | Foundry Responses/Files requests and safe file downloads |
| [Research tests](../../tests/test_company_research.py) | Configuration, calculation and mocked execution/output checks |
| [Client tests](../../tests/test_harness_client.py) | Transport, response completion, file references and error handling |

Run the commands below from the repository root in Bash or WSL. Python 3.12+,
Azure CLI (`az`) and Azure Developer CLI (`azd`) are required for the remote
workflow. The Python scripts use only the standard library.

## Install and check the tools

Install the tools in the environment where you will run these commands:

- [Python 3.12 or later](https://www.python.org/downloads/).
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) for sign-in
  and project role assignments.
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
  for agent deployment and Skill registration.

The instructions use Bash. With VS Code connected to WSL, install the tools
inside WSL rather than assuming a Windows installation is available there.
The source does not require `pip install`, a virtual environment or a
coding-agent extension.

Install the Foundry extensions from azd's official registry:

```bash
azd extension install microsoft.foundry --source azd
```

The `microsoft.foundry` bundle includes the agent, project and Skill commands.
It installs local tooling; it does not deploy an Azure resource. On an existing
installation, inspect the installed versions before changing them. Follow the
[extension guide](https://learn.microsoft.com/azure/developer/azure-developer-cli/extensions/overview)
for azd compatibility requirements.

The public-preview path requires azd 1.37.0+ and `azure.ai.agents`
1.0.0-beta.22+. Use the current bundle, including `azure.ai.projects`
1.0.0-beta.15+ and `azure.ai.skills` 1.0.0-beta.9+.
Enable harness commands in every shell or CI job where you deploy:

```bash
export AZD_ALPHA_ENABLE_AGENTS_MANAGED_HARNESS=true
```

This is a CLI opt-in, not an Azure permission grant. Child deployment processes
inherit it. Do not store credentials in the manifest or source.

Check the commands before signing in or deploying:

```bash
python3 --version
az version
azd version
azd extension list --installed
azd ai skill create --help
azd ai agent show --help
python3 -m scripts.configure_company_research --help
python3 -m scripts.run_company_research --help
```

If `azd ai skill` or `azd ai agent` is unavailable, check the installed Foundry
extensions and their required azd version. Do not substitute an unrelated
extension feed or proceed until the commands resolve.

The original live starter check used Python 3.12.3, Azure CLI 2.89.1, azd 1.35.1,
Foundry bundle 1.0.0-beta.2, agents 1.0.0-beta.18, projects
1.0.0-beta.13 and Skills 1.0.0-beta.8. The official registry currently offers
newer versions. The compatibility update uses the upgraded azd 1.37.1,
Foundry bundle beta.4, agents beta.22, projects beta.15 and Skills beta.9.
Upgrading an existing installation is separate from verifying installation on
a fresh machine. The original run is not evidence that the older tooling
supports the new public-preview contract.

## Check the calculator locally

From the repository root, using Python 3.12+:

```bash
python3 skills/company-research/calculate_scenarios.py
```

The script needs only the Python standard library. It reads the assumptions
beside the script and prints JSON to standard output. It does not call Azure
or Web IQ.

The supplied base scenario produces USD 510 revenue, USD 180 variable cost
and USD 130 contribution after the monthly fixed cost. These numbers come from
the sample assumptions, not from company research or an inference about a
company's finances.

## Deploy and run

You need an existing Foundry project and a supported model deployment.
If the project has no model, deploy one before setting up the agent.
Web IQ requires separate access and a project connection.

### 1. Select the existing project and model

Sign in to Azure CLI and Azure Developer CLI with the intended tenant:

```bash
az login --tenant "<tenant-id>"
azd auth login

export AZURE_SUBSCRIPTION_ID="<subscription-id>"
export AZURE_RESOURCE_GROUP="<existing-resource-group>"
export AZURE_LOCATION="<project-region>"
export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export FOUNDRY_PROJECT_ARM_ID="/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>"
export MODEL_DEPLOYMENT="<supported-model-deployment-name>"
export WEBIQ_CONNECTION_NAME="<webiq-project-connection-name>"
export AGENT_NAME="company-research-starter"

az account show --subscription "$AZURE_SUBSCRIPTION_ID" \
  --query '{subscription:id,tenant:tenantId}' --output json
```

Use the model deployment's name, not just its catalog name. Choose an agent
name that is not already in use unless you intend to create a new version of
that agent. This guide reuses a project and model; it does not provision them.

If the project has no supported model deployment, create one through its model
deployment management first. Select a supported model/version, deployment type
and capacity within your available quota, then use its deployment name for
`MODEL_DEPLOYMENT`. See the
[Foundry model deployment guide](https://learn.microsoft.com/azure/ai-foundry/how-to/deploy-models-openai).

### 2. Configure the Web IQ connection

Obtain Web IQ access using its
[setup guide](https://webiq.microsoft.ai/documentation/quick-start/).
Create a connection in the same Foundry project:

| Setting | Value |
|---------|-------|
| Name | The value of `WEBIQ_CONNECTION_NAME` |
| Category | `RemoteTool` |
| Target | `https://api.microsoft.ai/v3/mcp` |
| Authentication | `CustomKeys` |
| Credential header | `x-apikey` |
| Credential value | Your Web IQ API key |

Store the key in the connection, not in these source files, your shell commands
or the agent's instructions. The local Python runner does not need the key.

### 3. Register the Skill

Use a new local package directory containing only the three Skill files:

```bash
mkdir -p .local
mkdir .local/company-research-skill &&
  cp skills/company-research/SKILL.md \
    skills/company-research/calculate_scenarios.py \
    skills/company-research/assumptions.json \
    .local/company-research-skill/ &&
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai skill create company-research \
    --file .local/company-research-skill \
    --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" --no-prompt
```

The `&&` sequence stops if package creation or copying fails. Keep the package
directory new so it cannot contain files from a previous build. If the project
already has a `company-research` Skill, inspect its contents/version and decide
whether to reuse it rather than running the create command:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai skill list \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" --output json
```

Do not use `--force` to resolve a name collision: for Skill creation, it deletes
the existing Skill before recreating it.

Registering a Skill stores a versioned package in Foundry. Referencing that
version in the agent definition makes it available to the agent. Neither step
publishes this repository.

Record the version returned by the command:

```bash
export SKILL_VERSION="<published-numeric-version>"
```

### 4. Generate the configured definition

This command runs locally and makes no Azure calls:

```bash
python3 -m scripts.configure_company_research \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --model-deployment "$MODEL_DEPLOYMENT" \
  --connection-name "$WEBIQ_CONNECTION_NAME" \
  --agent-name "$AGENT_NAME" \
  --skill-version "$SKILL_VERSION" \
  --output-dir .local/company-research-deploy
```

It creates `.local/company-research-deploy/azure.yaml` and refuses to overwrite
an existing manifest. Review it before deployment. Model deployment, agent name
and Skill version must be explicit; no unresolved placeholders are accepted.

[azure.yaml](azure.yaml) is a template. Its placeholders are:

| Placeholder | Value to supply |
|-------------|-----------------|
| `__PROJECT_ENDPOINT__` | Your Foundry project's endpoint |
| `__MODEL_DEPLOYMENT__` | An existing supported model deployment name |
| `__WEBIQ_CONNECTION_NAME__` | The Web IQ project connection reference |
| `__AGENT_NAME__` | The name for this agent |
| `__SKILL_VERSION__` | The published `company-research` Skill version |

Do not deploy the template with unresolved placeholders. Use a distinct agent
name for a new example; deploying under an existing name creates a version of
that existing agent.

The configured Web IQ tools are `web`, `news` and `browse`. The manifest uses
`require_approval: never`, allowing those calls without per-call approval.
Review the tool permissions before deployment. Public-data limits in the
instructions are not a substitute for service access controls.

### 5. Deploy the agent

These commands create Azure agent resources and configure a local azd environment.
Use the existing project values from step 1:

```bash
(
  set -e
  cd .local/company-research-deploy

  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env new company-research \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --location "$AZURE_LOCATION" --no-prompt
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_RESOURCE_GROUP "$AZURE_RESOURCE_GROUP"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_AI_PROJECT_ID "$FOUNDRY_PROJECT_ARM_ID"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_AI_PROJECT_ENDPOINT "$FOUNDRY_PROJECT_ENDPOINT"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set FOUNDRY_PROJECT_ENDPOINT "$FOUNDRY_PROJECT_ENDPOINT"

  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy company-research-agent --no-prompt
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent show company-research-agent --output json
)
```

`company-research-agent` is the service key in the manifest; `AGENT_NAME` is the
remote agent name. Use `azd deploy` for this definition, not `azd up` or
`azd provision`, which can involve provisioning infrastructure.

Check that the published record has the intended model, agent name, Skill
version and `microsoft_copilot_preview` harness. Record the active agent version
and its `instance_identity.principal_id`.

The template creates a new Microsoft-harness agent. To keep an existing legacy
agent, preserve its original discriminator; the client accepts both names.
Do not attempt an in-place harness switch by deploying this template over it.
The Web IQ connection, allowed tools and pinned Skill workflow are unchanged.

### 6. Grant file-output access

An administrator should check whether the agent instance identity already has
the appropriate project-scoped access. If it needs Foundry User, the assignment
is:

```bash
az role assignment create \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --assignee-object-id "<agent-instance-principal-id>" \
  --assignee-principal-type ServicePrincipal \
  --role "53ca6127-db72-4b80-b1b0-d745d6d5456d" \
  --scope "$FOUNDRY_PROJECT_ARM_ID"
```

Use the agent's instance principal, not the project identity. Do not duplicate
an existing assignment or grant a broader scope. Allow several minutes for a
new role assignment to propagate. The caller also needs data-plane access;
this grant does not configure Web IQ authentication.

### 7. Run one research request

This command makes one remote research request and can incur model and Web IQ
charges. Use the actual active version returned by deployment:

```bash
python3 -m scripts.run_company_research \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --agent-name "$AGENT_NAME" \
  --agent-version "<active-numeric-version>" \
  --company "TinyFish" \
  --domain "https://www.tinyfish.ai/" \
  --question "Which product differentiators have independent support, and what should I investigate next?" \
  --output-dir .local/company-research-results \
  --allow-paid-invocation
```

Omitting `--allow-paid-invocation` stops before any remote request. Each run
uses a fresh conversation and writes a uniquely named local directory beneath
the output directory. It records the conversation/request before submission,
the response, the answer, downloaded files and a verification result.
Errors return a nonzero exit code and retain available evidence without an
automatic client retry.

The public domain must be an HTTPS host, without a URL path, credentials or IP
address. The prompt includes the UTC research date, so later runs can retrieve
different sources.

`--timeout` defaults to 900 seconds. It controls the client's HTTP socket
timeout and response-polling deadline, not a hard end-to-end wall-clock or
billing cap. It does not cancel remote execution. Inspect the saved
conversation/service state before considering a new request after a timeout.

## Inspect the results

Check that the brief addresses the question and that its material claims are
supported by the sources. The scenario assumptions and calculations should
remain separate from company facts.

A useful review checks:

- Actual Web IQ results support the source ledger.
- Company claims are distinguished from independent evidence and inference.
- The packaged calculator ran and its output matches `scenarios.json`.
- All three files were delivered, rather than only described in the answer.
- Missing evidence, tool errors and upload failures are reported.

The automated checks verify Skill loading, successful Web IQ retrieval,
standalone calculator execution, source-ledger consistency and scenario
values. They do not prove every narrative claim is supported by its citation.
If Web IQ returns a `retryAfter` result without retrieved content, the runner
prints a warning and excludes it from source evidence. A successful search
must still return actual results. The agent may finish using other successful
retrievals, but its report should explain the missing coverage.

## Run the offline tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use temporary files and mocked service responses. They do not
authenticate to Azure, deploy agents or invoke models. Local tests are separate
from an end-to-end check of the new deployment and current Web IQ connection.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Web IQ authentication fails | Connection reference, credentials and Web IQ access |
| Skill does not load | Registered name and published version |
| Calculation fails | Skill package includes both the script and assumptions file |
| Output upload is denied | Agent instance identity's project-scoped file access |
| An output file is missing | Tool/upload results, not just the final answer |
| Research output cannot be parsed | Preserve the result and investigate the integration; do not silently repair it into a successful result |

The current starter was deployed and physically invoked for a public company
research task. Skill loading, the packaged calculation and all three output
files passed checks; the response and files were independently downloaded again.
Search and news supplied the cited evidence. Browse requests returned
`retryAfter` results, and the report disclosed that it relied on search/news
passages rather than successful page browsing.

Earlier integration tests observed malformed nested JSON in the Foundry/Web IQ
result path. That specific symptom did not occur in this run, but its original
cause remains unresolved. A new task's success does not establish that the
historical issue is fixed.

## Usage and cleanup

Remote research can incur model and Web IQ charges. Use only public information
and follow the applicable service and source-content terms.

The Skill's limit of 12 external calls is an instruction to the agent, not an
enforced billing cap. The scenarios are illustrative and are not investment
advice.

Remove sample-specific agents, Skills, connections and role assignments only
after checking whether other applications use them. Keep the existing project
and model deployment unless you intend to remove those shared resources.
