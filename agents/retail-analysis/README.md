# Retail analysis with changing evidence

Build an analyst that receives transaction data, calculates the figures and
updates its assessment when new evidence arrives.

The agent produces a structured assessment and a readable report at each step.
It identifies which earlier claims to retain, revise or withdraw, with references
to the source rows. Your application controls when evidence arrives; the
agent performs the analysis within each step.

The business question is:

> How has measured transaction performance changed, what does the cancellation
> evidence establish, and what should the commercial team investigate next?

## What this agent does

The workflow has four steps in one conversation:

| Step | Evidence supplied | Expected work |
|------|-------------------|---------------|
| Initial assessment | Baseline and week 1 | Calculate both periods and explain the change |
| First update | Add week 2 and the actual prior assessment | Recalculate and account for earlier claims |
| Final update | Add week 3 and the actual prior assessment | Explain the new period without changing historical facts |
| Unchanged-data check | Same final CSV bytes and filename | Preserve the measured facts; explain any interpretation change |

This combines exact calculation with flexible analysis. For example, more
cancellation rows need not mean a higher cancellation-value magnitude. The
agent must examine both rather than infer a cause from a count alone.

It uses Python's standard-library `csv` and `Decimal` modules inside the
harness. No Web IQ connection, published Skill or custom agent container is
required. The local caller attaches the permitted files and checks the outputs.

## Data and metric definitions

The source is [UCI Online Retail II](https://doi.org/10.24432/C5CG6D), by
Daqing Chen, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
This example selects United Kingdom invoice lines in four adjacent seven-day
windows starting September 27, 2010.

The full workbook contains 1,067,371 records. The selected windows contain
50,947 records, revealed as 24,491, then 37,509, then 50,947 rows.

- Positive quantities contribute positive transaction value.
- Negative quantities reduce that value.
- Rows with invalid quantity or nonpositive/invalid price are retained but
  excluded from the value calculation.
- Duplicate transactions on different source rows are preserved.
- Cancellation flags and missing customer IDs are recorded separately.

These are signed transaction values, not audited revenue, profit or cash flow.
The source does not establish cancellation reasons, costs, stock availability
or shipment outcomes. The agent can identify evidence needed next, not invent it.

The [fixed data plan and metric policy](data-policy.json) define the windows,
source hashes and calculations. The [data notice](DATA-NOTICE.txt) describes
attribution and transformations.

The [workflow diagram](assets/retail-flow.svg) and
[weekly-value illustration](assets/retail-evidence.svg) summarize the staged
analysis and reference totals. These are authored documentation graphics,
not captured agent-run outputs.

## Source guide

| File | Purpose |
|------|---------|
| [azure.yaml](azure.yaml) | Agent definition and analyst instructions |
| [data-policy.json](data-policy.json) | Pinned public source, time windows and metric policy |
| [prepare_retail.py](../../scripts/prepare_retail.py) | Download the public ZIP and build chronological CSV snapshots |
| [run_retail.py](../../scripts/run_retail.py) | Render a definition, run four bounded stages or audit a saved run |
| [retail_evidence.py](../../retail_evidence.py) | Independent Decimal calculations, source-cell and claim-update checks |
| [retail_support.py](../../retail_support.py) | Request recording, file-hash verification and definition checks |
| [Preparation tests](../../tests/test_retail_preparation.py) | Workbook preparation, arithmetic and input-policy fixtures |
| [Runner tests](../../tests/test_retail_analysis.py) | Stage order, file handoffs, failure handling, watchdog and read-only audit |

## 1. Prepare the local environment

Use Python 3.12+ from the repository root. Full workbook preparation needs only
one additional package, pinned in [requirements-retail.txt](../../requirements-retail.txt):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-retail.txt
python -m unittest discover -s tests -v
```

The commands use Bash or WSL. The tests use fixtures and scripted service
responses, not Azure credentials. Without openpyxl, the workbook fixture test
explicitly skips; that is not complete preparation coverage.

If you prefer uv, the equivalent test command is:

```bash
uv run --no-project --python 3.12 \
  --with-requirements requirements-retail.txt \
  python -m unittest discover -s tests -v
```

See the [tooling instructions](../company-research/README.md#install-and-check-the-tools)
for Azure CLI, azd and the Foundry extensions. You do not need the company
research agent or its Web IQ setup.

## 2. Download and prepare the transaction snapshots

The download fetches the public dataset. The build is local once the ZIP and
openpyxl are available:

```bash
python -m scripts.prepare_retail download \
  --output-dir .local/retail-source

python -m scripts.prepare_retail build \
  --source .local/retail-source/source.zip \
  --output-dir .local/retail-prepared
```

Both output directories must be new. The source ZIP and workbook hashes,
full sheet row counts, source columns and date windows are checked.
The build preserves row identities and independently calculates reference values.

```text
.local/retail-prepared/
├── stage-1/       evidence.csv, policy.json, manifest.json, DATA-NOTICE.txt
├── stage-2/       same file types, with more visible evidence
├── stage-3/       same file types, with the full selected windows
├── evaluator/     reference values and the complete stage schedule
└── preparation.json
```

**Never upload `evaluator/` or the whole prepared directory to the agent.**
The runner attaches only the current CSV, policy, attribution, current-stage
binding and the actual prior assessment. The unchanged-data step reuses stage 3's
CSV rather than inventing another period.

## 3. Configure an agent in your existing project

Use an existing project and a supported model deployment:

```bash
az login --tenant "<tenant-id>"
azd auth login

export AZURE_SUBSCRIPTION_ID="<subscription-id>"
export AZURE_RESOURCE_GROUP="<existing-resource-group>"
export AZURE_LOCATION="<project-region>"
export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export FOUNDRY_PROJECT_ARM_ID="/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>"
export MODEL_DEPLOYMENT="<supported-model-deployment-name>"
export RETAIL_AGENT_NAME="retail-analysis-starter"

python -m scripts.run_retail render \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --model "$MODEL_DEPLOYMENT" \
  --agent-name "$RETAIL_AGENT_NAME" \
  --output-dir .local/retail-deploy
```

Rendering is local and refuses to overwrite a definition. Review the generated
`.local/retail-deploy/azure.yaml`. The published agent must match its model,
instructions and harness selection before a run starts.

Choose a name not already in use unless you intend to create a new version of
an existing agent. These steps do not create or resize a model deployment.

## 4. Deploy and grant file-output access

This step changes Azure resources:

```bash
export AZD_ALPHA_ENABLE_AGENTS_MANAGED_HARNESS=true

(
  set -e
  cd .local/retail-deploy
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env new retail-analysis \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --location "$AZURE_LOCATION" --no-prompt
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_RESOURCE_GROUP "$AZURE_RESOURCE_GROUP"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_AI_PROJECT_ID "$FOUNDRY_PROJECT_ARM_ID"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_AI_PROJECT_ENDPOINT "$FOUNDRY_PROJECT_ENDPOINT"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set FOUNDRY_PROJECT_ENDPOINT "$FOUNDRY_PROJECT_ENDPOINT"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy "$RETAIL_AGENT_NAME" --no-prompt
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent show "$RETAIL_AGENT_NAME" --output json
)
```

The rendered service key is your agent name. Check that the version is active,
uses your intended model and includes the `microsoft_copilot_preview` harness.
Record its numeric version and `instance_identity.principal_id`.

Use a distinct new name for this template; the public preview doesn't support
changing an existing agent's harness. The client also accepts unchanged legacy
GitHub-harness agents. No explicit toolset block has been added to retail:
its existing tools, instructions, transaction policy and four-step task remain
unchanged. The shared client supplies the current preview header.

The caller needs Foundry data-plane access. An administrator should also check
the agent instance identity's project-scoped file access. If it needs Foundry
User, the assignment is:

```bash
az role assignment create \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --assignee-object-id "<agent-instance-principal-id>" \
  --assignee-principal-type ServicePrincipal \
  --role "53ca6127-db72-4b80-b1b0-d745d6d5456d" \
  --scope "$FOUNDRY_PROJECT_ARM_ID"
```

Use the instance identity, not the project identity. Reuse an appropriate
existing assignment and allow several minutes for a new grant to propagate.

## 5. Run the four-step assessment

This command makes **at most four paid agent requests**:

```bash
export RETAIL_AGENT_VERSION="<active-numeric-version>"

python -m scripts.run_retail run \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --agent-name "$RETAIL_AGENT_NAME" \
  --agent-version "$RETAIL_AGENT_VERSION" \
  --prepared .local/retail-prepared \
  --manifest .local/retail-deploy/azure.yaml \
  --output-dir .local/retail-run \
  --timeout 420 \
  --allow-paid-invocations
```

Each stage must pass the mechanical checks before the next request. The runner
uses one conversation and the actual previous assessment file. It records
requests before submission and saves responses, downloaded artifacts, input
hashes and validation results.

If a stage fails, later stages are not submitted. There is no automatic client
retry or resume command. The output directory must be new; never delete a
failed run to make it look like a first attempt.

Each stage runs in a separate local process with a total wall-clock deadline.
At the deadline, that process is terminated and reaped. **This does not prove
remote cancellation or cap remote billing.** Inspect the saved request and
service history before deciding whether another separately authorized run
is appropriate.

## 6. Inspect and independently audit the result

Each stage delivers:

- `assessment-<stage>.json`: exact metrics, source-cell references, claims,
  dispositions for prior claims and limitations.
- `report-<stage>.md`: the assessment in readable form.

They are saved in `.local/retail-run/<stage>/downloads/`. All data, generated
configuration, responses and logs stay under ignored `.local/`, not in source.

To re-fetch the saved responses and files and check them again:

```bash
python -m scripts.run_retail audit \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --agent-name "$RETAIL_AGENT_NAME" \
  --agent-version "$RETAIL_AGENT_VERSION" \
  --prepared .local/retail-prepared \
  --manifest .local/retail-deploy/azure.yaml \
  --run-dir .local/retail-run \
  --output-dir .local/retail-audit
```

Audit is read-only. It does not create conversations, invoke the model or
continue a stopped run. A partial audit does not imply a complete four-stage
result. Keep the original prepared source and run files unchanged so the
request and byte comparisons remain meaningful.

The automated checks cover arithmetic, input hashes, exact cited cells and
prior-claim accounting. Review the report as well:

1. Does it distinguish equal-week changes from cumulative totals?
2. Does it distinguish cancellation counts from their value?
3. Are claims revised for new evidence rather than for a more dramatic story?
4. With unchanged data, are facts stable and interpretation changes justified?
5. Does it ask for missing evidence rather than infer profit or business causes?

## Troubleshooting and cleanup

| Symptom | Action |
|---------|--------|
| ZIP/workbook hash mismatch | Keep the failure record; check whether the public source changed rather than disabling the hash check |
| Missing or wrong openpyxl version | Install the pinned dependency from `requirements-retail.txt` |
| Preparation fingerprint mismatch | Rebuild into a new directory using the current source; do not edit provenance records |
| Agent definition differs | Review the rendered definition and pinned version before invoking |
| File upload denied | Check the instance identity's project-scoped access |
| Stage deadline reached | Inspect original service state; local termination does not cancel the remote turn |
| Metrics or citations rejected | Review the preserved artifacts and source cells; do not relax validation to manufacture a pass |

Closing the runner leaves the deployed agent and its permission assignment in
place. Remove only sample-specific resources after checking for shared use.
Do not delete the existing project or model as a shortcut.
