# Investigate recorded incidents with logs, metrics and traces

Build an investigator that examines a supplied telemetry snapshot, separates
the originating failure from downstream symptoms, and delivers a diagnosis
with exact evidence references, alternatives and a next test.

The agent is advisory. It does not connect to production monitoring, execute
remediation, change deployments or establish that a mitigation works.

## What you build

One Microsoft Copilot harness prompt agent with three pinned Skills:

| Skill | Contents |
|-------|----------|
| [Investigation method](../../skills/incident-investigation-telemetry/SKILL.md) | Investigation instructions, runtime input verifier, metrics and logs |
| [Trace data part one](../../skills/incident-trace-data-part-one/SKILL.md) | First complete trace shard |
| [Trace data part two](../../skills/incident-trace-data-part-two/SKILL.md) | Second complete trace shard |

The authored Skill folders contain instructions and verifier source, **not the
telemetry assets**. The preparer downloads pinned public inputs and builds
complete publishable packages locally. Do not upload the source-only folders
as if they were complete data packages.

Each request delivers:

- `diagnosis.json`: qualified service/mechanism, exact cited source cells,
  alternatives, a falsifiable next test, advisory mitigation and limitations.
- `report.md`: readable findings referencing the same evidence IDs.

The model chooses its investigation steps. Independent local checks verify
actual Skill loading, standalone input verification and exact source cells.
Human review must assess whether those cells actually support the explanation.

## Data, attribution and boundaries

Inputs come from [RCAEval](https://doi.org/10.5281/zenodo.14590730), measurements
from **controlled microservice fault injection**, not customer production
outages. The [source catalog](cases.json) pins the converted dataset revision,
file sizes/hashes, shapes, complete CSV hashes and trace split positions.

The original record declares CC BY 4.0; the
[Hugging Face conversion](https://huggingface.co/datasets/phamquiluan/RCAEval)
separately declares MIT. The [data notice](DATA-NOTICE.txt) preserves both
declarations without claiming to resolve their differing scope. Review
applicable terms before distributing data. This repository publishes preparation
source and attribution, **not bundled telemetry or agent-run reports**.

All three existing cases are retained:

| Case | Metric rows | Log rows | Trace rows |
|------|------------:|---------:|-----------:|
| case-001 | 1,441 | 65,025 | 148,799 |
| case-002 | 1,441 | 70,558 | 165,302 |
| case-003 | 1,441 | 71,072 | 166,523 |

Start with case-001. Trace shards preserve every row and global record ID.
No sampling or imputation occurs. Empty strings, nulls and multiline log cells
are checked in a typed Parquet-to-CSV roundtrip.

The catalog contains upstream case identifiers that reveal benchmark identity.
It belongs to preparation/evaluation, **never model inputs**. The generated
packages use neutral case identifiers and omit upstream case names, benchmark
labels and injection-time files. Public benchmark memorization remains possible.

## Source guide

| Source | Purpose |
|--------|---------|
| [azure.yaml](azure.yaml) | Parameterized agent definition and unchanged investigation instructions |
| [cases.json](cases.json) | Pinned acquisition/conversion metadata |
| [prepare_incident.py](../../scripts/prepare_incident.py) | Bounded download, offline conversion, complete package building |
| [publish_incident_skills.py](../../scripts/publish_incident_skills.py) | Explicit Skill publication and exact downloaded-package roundtrip |
| [configure_incident.py](../../scripts/configure_incident.py) | Local manifest rendering using the verified publication versions |
| [run_incident.py](../../scripts/run_incident.py) | One paid request or read-only audit of an existing run |
| [incident_investigation.py](../../incident_investigation.py) | Execution proof, diagnosis schema and exact original-cell validation |
| [requirements-incident.txt](../../requirements-incident.txt) | PyArrow pin for preparation only |

No Web IQ, A2A, peer reviewer or custom agent container is needed.
The agent uses Python standard-library analysis in the managed harness;
PyArrow runs in the local preparation environment, not the agent sandbox.

## 1. Prepare local tooling

Run from the repository root in Linux or WSL with Python 3.12+.
See the [shared CLI installation guide](../company-research/README.md#install-and-check-the-tools)
for azd and the Foundry extensions. Public-preview deployment requires the
shell opt-in and compatible tools.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-incident.txt
python -m unittest discover -s tests -p 'test_*incident*.py' -v
```

The dependency is pinned to PyArrow 21.0.0 because CSV serialization and typed
roundtrip are part of the fixed input identity. Without PyArrow, its fixture
tests explicitly skip; that is not complete preparation verification.

## 2. Download and build the starter inputs

```bash
python -m scripts.prepare_incident download \
  --case case-001 \
  --output-dir .local/incident-data/case-001

python -m scripts.prepare_incident build \
  --case case-001 \
  --source-dir .local/incident-data/case-001 \
  --output-dir .local/incident-prepared/case-001
```

Download fetches only three exact pinned public Parquet files. Build is offline:
it verifies the source fingerprints and shapes, preserves values/trace rows,
checks package allowlists and executes the actual runtime input verifier.
It computes no diagnosis. Existing output directories are refused; keep failed
attempts instead of deleting them to manufacture a clean run.

```text
.local/incident-prepared/case-001/
  original/             complete CSVs for independent local checking
  original-input.zip    exact local-only evidence archive
  packages/
    incident-investigation-telemetry/
    incident-trace-data-part-one/
    incident-trace-data-part-two/
  preparation.json
  runtime-verification.json
```

**Only the three individual directories inside `packages/` are publication
candidates.** Never publish the prepared root, original archive, catalog,
source data or benchmark labels. Package sizes use the existing conservative
16,000,000-byte bound; consult current service limits rather than treating
this source policy as an eternal runtime guarantee.

Cases 002/003 use the same commands with different IDs and new directories.
Each produces its own complete, independently verified package set.

## 3. Select existing Azure resources

Sign in yourself to the approved tenant; do not let a script silently select
or create a project, model, region or connection.

```bash
az login --tenant "<tenant-id>"
azd auth login

export AZURE_SUBSCRIPTION_ID="<subscription-id>"
export AZURE_RESOURCE_GROUP="<existing-resource-group>"
export AZURE_LOCATION="<existing-project-region>"
export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export FOUNDRY_PROJECT_ARM_ID="/subscriptions/<subscription-id>/resourceGroups/<group>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>"
export MODEL_DEPLOYMENT="<existing-supported-model-deployment>"
export INCIDENT_AGENT_NAME="<new-distinct-agent-name>"
export AZD_ALPHA_ENABLE_AGENTS_MANAGED_HARNESS=true
```

Caller access, Skill publication and project role assignment are separate
permissions. Harness choice is fixed at creation during this preview. The
template creates a new Microsoft-harness agent; do not deploy it over an
existing default-runtime or legacy agent to change its harness.

The manifest's empty `tools` list means no external declared tool. It does
**not** disable managed built-ins. Local-only instructions are behavioral
constraints, not an enforced network-egress policy.

## 4. Publish the complete Skill packages

This step writes Azure Skill resources and requires explicit approval:

```bash
python -m scripts.publish_incident_skills \
  --scenario incident \
  --prepared-dir .local/incident-prepared/case-001 \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --operation create \
  --output-dir .local/incident-publication/case-001 \
  --allow-azure-writes
```

Use `create` only when the Skill names are unused. Inspect existing versions
first; choose `update` explicitly only if publishing new immutable versions
of those existing Skills is intended. Existing pinned versions are not
changed by referencing the returned versions.

The publisher rechecks source/package fingerprints and input verification,
publishes once per Skill, downloads each returned immutable version and
compares every file byte. It does not retry an ambiguous publication.
The resulting `publication.json` supplies the exact three versions.
For another case, publish its package set separately and render an agent
version pinned to that publication. Never mix cases across the three Skills.

## 5. Render, inspect and deploy the agent

Rendering makes no Azure call:

```bash
python -m scripts.configure_incident \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --model-deployment "$MODEL_DEPLOYMENT" \
  --agent-name "$INCIDENT_AGENT_NAME" \
  --publication .local/incident-publication/case-001/publication.json \
  --output-dir .local/incident-deploy
```

Review the generated manifest. Deploy only after authorization:

```bash
(
  set -e
  cd .local/incident-deploy
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env new incident-investigation \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --location "$AZURE_LOCATION" --no-prompt
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_RESOURCE_GROUP "$AZURE_RESOURCE_GROUP"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_AI_PROJECT_ID "$FOUNDRY_PROJECT_ARM_ID"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_AI_PROJECT_ENDPOINT "$FOUNDRY_PROJECT_ENDPOINT"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set FOUNDRY_PROJECT_ENDPOINT "$FOUNDRY_PROJECT_ENDPOINT"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy incident-investigation-agent --no-prompt
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent show incident-investigation-agent --output json
)
```

The service key is `incident-investigation-agent`; the remote name is the
explicit `INCIDENT_AGENT_NAME`. Check active version, model, Microsoft harness,
unchanged instructions and all three pinned Skill versions.

An authorized administrator should check the actual
`instance_identity.principal_id` for project-scoped file-output access. If absent:

```bash
az role assignment create \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --assignee-object-id "<agent-instance-principal-id>" \
  --assignee-principal-type ServicePrincipal \
  --role "53ca6127-db72-4b80-b1b0-d745d6d5456d" \
  --scope "$FOUNDRY_PROJECT_ARM_ID"
```

Use the instance identity, not the project identity. Do not duplicate an
existing appropriate assignment. Allow up to five minutes for propagation.

## 6. Invoke one investigation

This step authorizes **one paid model request**:

```bash
export INCIDENT_AGENT_VERSION="<active-numeric-version>"

python -m scripts.run_incident \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --agent-name "$INCIDENT_AGENT_NAME" \
  --agent-version "$INCIDENT_AGENT_VERSION" \
  --prepared-dir .local/incident-prepared/case-001 \
  --publication .local/incident-publication/case-001/publication.json \
  --manifest .local/incident-deploy/azure.yaml \
  --output-dir .local/incident-run/case-001 \
  --allow-paid-invocation
```

Before invoking, the runner checks the prepared input packages, verified
publication and exact pinned agent definition. It records source hashes,
prompt and conversation, and saves the request before submission.
It never runs Git commands. Committed-source provenance is a separate
owner-controlled export/verification step, not an invented commit field.

The default 900-second timeout controls HTTP/polling, not a hard total
wall-clock or billing bound and not remote cancellation. After an uncertain
failure, inspect its original conversation/service state before considering
any separately authorized new request.

## 7. Audit and review

Re-fetch the existing response and files without invoking the model:

```bash
python -m scripts.run_incident \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --agent-name "$INCIDENT_AGENT_NAME" \
  --agent-version "$INCIDENT_AGENT_VERSION" \
  --prepared-dir .local/incident-prepared/case-001 \
  --publication .local/incident-publication/case-001/publication.json \
  --manifest .local/incident-deploy/azure.yaml \
  --audit-run-dir .local/incident-run/case-001 \
  --output-dir .local/incident-audit/case-001
```

Keep the original source/packages/publication/run unchanged. Audit verifies
the task, identity, source and exact downloaded file bytes; a schema or citation
pass does not establish causal truth.

Read the diagnosis/report and ask:

1. Do cited records distinguish the originating failure from downstream symptoms?
2. Are timestamps and span status codes interpreted correctly?
3. Are alternatives and disconfirming evidence genuinely considered?
4. Does the next test say what would change the conclusion?
5. Are missing evidence, proposed mitigation and unobserved recovery explicit?

## Migration verification status

The migrated local preparation path downloaded and rebuilt **all three cases**
from the pinned public source on November 2, 2026 (Pacific time). Complete CSV
hashes match the retained historical identities, typed conversion/shard checks
passed, and each complete package set passed its standalone verifier.

The owner then committed the source as
[4dcc1fa](https://github.com/AshuJoshi/foundry-copilot-harness/commit/4dcc1fa0c774c113de2c28cd31256f27b7d38f35).
Its clean Git-only export passed **175 offline tests without skips**, including
the existing Docker and PyArrow controls. All three package sets were rebuilt
from that export using explicitly imported, verified pinned source downloads;
every package file and preparation record matched the earlier local build.

With separate approval, case-001's rebuilt packages were published as new
immutable Skill versions and downloaded byte-for-byte. A distinct new
Microsoft-harness agent was deployed in the existing Canada Central project
using the existing model deployment. One actual investigation request completed:

| Check | Result |
|-------|--------|
| Three Skills and standalone input verifier | Actual correlated execution verified |
| Delivered output | `diagnosis.json` and `report.md` downloaded |
| Diagnosis evidence | Six cited original records checked across logs, metrics and traces |
| Independent audit | Same response content and exact file bytes re-fetched without invoking |
| Qualified explanation | Cartservice/Int32 overflow supported; exact parse operand and upstream input source remain unproven |
| Resource preservation | All 19 prior agents and five model deployments retained unchanged |

Independent full-input review confirmed 394 oversized cart requests, 394
overflow logs and 394 failed frontend AddItem spans, alongside 419 normal
requests/successful caller spans. Missing-cell counts also matched the report.
Caller-span start times and log timestamps have different precision; the
temporal/operation correlation does not establish exact causal ordering.
No mitigation, production impact or recovery was demonstrated.

This was **one paid request, with no resubmissions**, not a three-case rerun
or a population accuracy benchmark. Cases 002/003 remain preparation-verified
in this migration; their newly rebuilt packages were not published or invoked.
Earlier research runs remain separate historical evidence. The published agent
and its pinned Skill versions are source-verification facts, not version
numbers to copy into another deployment.

All downloaded data, generated packages, concrete configurations, responses,
reports and audits stay under ignored `.local/`. Do not check them in.
