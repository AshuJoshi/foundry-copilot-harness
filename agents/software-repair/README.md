# Software repair with independently checked patches

Give a managed Copilot harness agent a real issue and a complete pinned
repository snapshot. It investigates locally, proposes a minimal source patch,
and delivers the patch in JSON with a report. A separate local evaluator checks
the candidate against the official issue regression and existing tests.

Use this example to learn how to package code as Skills, preserve benchmark
answer boundaries, verify a delivered patch and distinguish an agent's own
test claims from independent evaluation.

This example uses one SWE-bench Verified task,
`pytest-dev__pytest-10051`. It is a small pipeline check, not a suite-level
score or a demonstration of general repair ability. The issue description
already explains much of the defect, and public-benchmark memorization is possible.

## What the agent produces

| File | Contents |
|---|---|
| `repair.json` | Task identity, complete unified patch, changed files, attempted tests and limitations |
| `report.md` | Evidence-backed justification, behavior change, regression considerations and uncertainty |

A result can be `candidate` or `blocked`. A blocked result has no patch and
explains why work could not proceed. It is not a successful repair.
The patch is transported as a JSON string; do not reconstruct it from prose
or silently fix an invalid patch.

The agent may lack Git or compatible dependencies. It can generate a standard
unified diff with Python, but it must report which tests it actually ran.
The host checks clean application independently with Git.

## Source and data boundaries

| Source | Purpose |
|---|---|
| [azure.yaml](azure.yaml) | Parameterized Microsoft harness agent and immutable Skill pins |
| [case.json](case.json) | Pinned dataset, task, complete repository identity and evaluator image |
| [Preparation](../../scripts/prepare_software_repair.py) | Public download followed by offline answer-separated package construction |
| [Repair Skill](../../skills/software-repair/SKILL.md) | Investigation procedure and output schema |
| [Repository verifier](../../skills/software-repair/verify_inputs.py) | Standalone check of complete input identity |
| [Data Skill](../../skills/software-repair-repository-data/SKILL.md) | Companion repository documentation |
| [Publisher](../../scripts/publish_repair_skills.py) | Verify source packages, publish once and compare downloaded bytes |
| [Configuration](../../scripts/configure_repair.py) | Render a concrete manifest without calling Azure |
| [Runner and auditor](../../scripts/run_software_repair.py) | One paid invocation or read-only reconciliation |
| [Patch validator](../../software_repair.py) | Output schema, allowed paths and independent Git application check |
| [Official evaluator](../../scripts/evaluate_software_repair.py) | Separate baseline, reference and downloaded-candidate Docker runs |
| [Data notice](DATA-NOTICE.txt) | Upstream attribution and distribution qualifications |

The original repository has 570 files. The preparer preserves every file,
including hidden files, and splits only `doc/` into a companion Skill to stay
within package file-count limits. The agent receives the original issue and
the complete source/tests/configuration, not an anonymized or reduced codebase.

Reference patches, benchmark test patches, hints and grading labels stay in a
separate local evaluator directory. They are never published to the agent.
The source repository license is preserved in the generated packages.
The [notice](DATA-NOTICE.txt) flags issue-text distribution terms for review
before redistributing generated packages; the source license here does not
relicense upstream code or issue text.

No Web IQ or custom Hosted container is required. The managed agent uses native
Skill, shell and file capabilities. An empty declared `tools` list is not a
ban on native built-ins. Instructions prohibit networking and installation,
but are not network-egress enforcement; see the repository's
[network boundary](../../README.md).

## 1. Prepare local tooling

Use Python 3.12, Git and Linux or WSL. Follow the shared
[uv-managed environment setup](../../README.md#local-python-environment) first,
then the
[shared CLI setup](../company-research/README.md#install-and-check-the-tools)
for Azure CLI, azd, the Foundry extension and managed-harness opt-in.
Docker and SWE-bench are needed only for official local grading, not agent
deployment. Do not install or change Docker implicitly.

```bash
uv pip install -r requirements-repair-prep.txt
python -m unittest discover -s tests -p '*software_repair*.py' -v
```

[PyArrow 21.0.0](../../requirements-repair-prep.txt) is pinned for dataset
extraction. Without it, preparation fixture tests explicitly skip. The
client, runtime verifier and patch validator use the Python standard library.

## 2. Acquire pinned inputs and build offline

```bash
python -m scripts.prepare_software_repair download \
  --output-dir .local/repair-data

python -m scripts.prepare_software_repair build \
  --source-dir .local/repair-data \
  --output-dir .local/repair-prepared
```

Download validates the immutable dataset bytes and the canonical hash mapping
of all 570 repository files. Archive compression metadata may vary; the complete
file-content mapping is the repository identity. Unsafe paths, links, duplicate
members and changed content are rejected.

Build makes no network requests. It rechecks source identity, packages the
complete repository, generates neutral task metadata and executes the
standalone input verifier. Both packages must be below the local policy limits
of 512 files and 16,000,000 uncompressed bytes each.

```text
.local/repair-prepared/
  packages/
    software-repair/                  task, source, tests, configuration and verifier
    software-repair-repository-data/  repository documentation and license
  evaluator/case.json                 withheld reference/test patches and grading labels
  preparation.json
  runtime-verification.json
```

**Only the two individual directories under `packages/` are publication
candidates. Never publish `evaluator/`, the prepared root or the raw dataset.**
Keep all downloads, generated packages and run evidence ignored under `.local/`.
Use new output directories; failed attempts are preserved, not repaired into passes.

## 3. Select an existing project and model

Sign in yourself and supply the intended scope. No command in this example
creates a model, project, resource group or connection.

```bash
az login --tenant "<tenant-id>"
azd auth login

export AZURE_SUBSCRIPTION_ID="<subscription-id>"
export AZURE_RESOURCE_GROUP="<existing-resource-group>"
export AZURE_LOCATION="<existing-project-region>"
export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export FOUNDRY_PROJECT_ARM_ID="/subscriptions/<subscription-id>/resourceGroups/<group>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>"
export MODEL_DEPLOYMENT="<existing-supported-model-deployment>"
export REPAIR_AGENT_NAME="<distinct-new-agent-name>"
export AZD_ALPHA_ENABLE_AGENTS_MANAGED_HARNESS=true
```

The public template uses `microsoft_copilot_preview`. Do not deploy it over an
existing agent to change its harness or overwrite a live agent as a test.

## 4. Publish and pin both Skills

Select `create` when the two Skill names do not exist. Use `update` only when
you intend to publish new immutable versions and move their defaults.
Older agents pinned to old versions stay unchanged.

```bash
python -m scripts.publish_repair_skills \
  --prepared-dir .local/repair-prepared \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --operation create \
  --output-dir .local/repair-publication \
  --allow-azure-writes

python -m scripts.configure_repair \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --model-deployment "$MODEL_DEPLOYMENT" \
  --agent-name "$REPAIR_AGENT_NAME" \
  --publication .local/repair-publication/publication.json \
  --output-dir .local/repair-deployment
```

Publication checks preparation/source hashes, exact repository contents and
runtime verifier output, then publishes each package once and downloads its
immutable version to compare all bytes. A partial failure stops without
repeating a write. Configuration pins the versions from verified publication.

On success, publication prints `Published and verified 2 Skills`, each returned
version and the path to `publication.json`. It changes Azure Skill resources
but does not deploy or invoke an agent. Configuration prints
`Configuration written: .../azure.yaml`, the selected agent/model and both
Skill pins, followed by a local-only message. That filename is output, not a
command to run. Inspect the generated file before continuing.

Deploy only the repair service:

```bash
(
  set -e
  cd .local/repair-deployment

  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env new repair-example \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --location "$AZURE_LOCATION" --no-prompt
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_RESOURCE_GROUP "$AZURE_RESOURCE_GROUP"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_AI_PROJECT_ID "$FOUNDRY_PROJECT_ARM_ID"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set FOUNDRY_PROJECT_ENDPOINT "$FOUNDRY_PROJECT_ENDPOINT"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_AI_PROJECT_ENDPOINT "$FOUNDRY_PROJECT_ENDPOINT"
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy software-repair-agent --no-prompt
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent show --output json
)
```

The subshell stops on a failed directory change or setup command and leaves
your terminal in the repository root. Deployment success means the agent is
created, not that it has repaired the task or passed official tests.

Record the active immutable agent version and instance principal. The runner
checks the exact published definition and Skill pins before invocation.
Agent file-upload permission is separate from the caller's access. An
administrator should inspect the instance identity's project-scoped access
before applying the [file-output grant](../company-research/README.md#6-grant-file-output-access)
if needed. No runner or publisher grants roles.

## 5. Invoke once, then audit read-only

```bash
python -m scripts.run_software_repair \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --agent-name "$REPAIR_AGENT_NAME" \
  --agent-version "<active-immutable-version>" \
  --prepared-dir .local/repair-prepared \
  --publication .local/repair-publication/publication.json \
  --manifest .local/repair-deployment/azure.yaml \
  --output-dir .local/repair-run \
  --allow-paid-invocation
```

This is one paid request with no client retry. The runner verifies both actual
Skill loads and a successful standalone repository verifier, downloads the
actual artifacts and checks the patch in a pristine temporary repository.
Allowed changes are to existing Python files under `src/` only; no tests,
configuration, dependencies, added/deleted files or binary patches.
An unresolved/blocked result is recorded honestly, not counted as a repair.

`result.json` includes original download hashes. Preserve those hashes with the
files; a hash newly assigned to an unexplained candidate is not agent provenance.
The read-only audit re-fetches the response and files and checks them against
the original task, source hashes and download fingerprints:

```bash
python -m scripts.run_software_repair \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --agent-name "$REPAIR_AGENT_NAME" \
  --agent-version "<active-immutable-version>" \
  --prepared-dir .local/repair-prepared \
  --publication .local/repair-publication/publication.json \
  --manifest .local/repair-deployment/azure.yaml \
  --audit-run-dir .local/repair-run \
  --output-dir .local/repair-audit
```

Neither candidate delivery nor audit proves the official tests passed.

## 6. Independently grade controls and the candidate

This runs repository code locally. Use reviewed, trusted inputs, an available
Docker daemon and separate authorization. It is not a general untrusted-code
sandbox. No agent invocation occurs.

```bash
uv pip install -r requirements-repair-eval.txt
```

[SWE-bench 4.1.0](../../requirements-repair-eval.txt) is pinned, but its transitive
dependencies are not fully locked. The official image digest is pinned in
[case.json](case.json). The evaluator does not pull, rebuild or retag images.
Acquire it deliberately:

```bash
docker pull swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-10051@sha256:e38365e835d4ba57f3e7331778894f51ac20bf6c5893827065da8657525e123f
```

The official harness derives its own image tag. Check that the local tag resolves
to the pinned digest before grading; creating a tag must not overwrite an
unrelated existing image. A network-disabled inspection container verifies the
entire tracked tree equals the task base and has no modifications.
The subsequent official test run uses the official harness configuration and
is **not claimed to be network-isolated**. Evaluation may consume substantial
disk, CPU and memory.

```bash
python -m scripts.evaluate_software_repair \
  --prepared-dir .local/repair-prepared --mode baseline \
  --run-id repair-baseline-001 --output-dir .local/repair-evaluation/baseline \
  --allow-local-execution

python -m scripts.evaluate_software_repair \
  --prepared-dir .local/repair-prepared --mode reference \
  --run-id repair-reference-001 --output-dir .local/repair-evaluation/reference \
  --allow-local-execution

python -m scripts.evaluate_software_repair \
  --prepared-dir .local/repair-prepared --mode candidate \
  --candidate .local/repair-run/downloads/repair.json \
  --candidate-sha256 "<repair.json hash from the original result.json>" \
  --run-id repair-candidate-001 --output-dir .local/repair-evaluation/candidate \
  --allow-local-execution
```

Use distinct run IDs/output directories. The negative control adds only an
inert marker, leaving production source unchanged: its issue test should fail
and its existing tests should pass. The reference fix must resolve the issue.
Missing reports, unapplied patches, wrong test accounting and failed controls
are errors, not ordinary unresolved candidates. Official reports/logs and exact
patch/image fingerprints are preserved.

## Observed results and verification stages

The original research implementation was live-checked on October 19, 2026
(October 20 UTC). A new agent with rebuilt Skills delivered a fresh candidate
whose official evaluation passed one issue test and 15 regression tests.
Fresh negative and reference controls behaved as expected.
The issue description and single-case nature limit what this demonstrates.

That result is **historical research evidence**, not a new invocation from
this public layout. It is not pooled with earlier candidate reevaluations.

This example provides current Microsoft authoring and separate configuration,
publication, invocation and audit commands. Offline fixture tests cover
answer-separated preparation, exact package publication, model/Skill binding,
artifact delivery, patch application, failure preservation and read-only audit.
The source-only suite was also checked in an isolated copy without downloaded
data or saved runs.

Actual pinned-input rebuilding, Skill publication/deployment, a new invocation,
read-only service audit and official Docker grading are separate verification
stages. The fresh run below completed all of those stages.

### Observed results: November 16, 2026

The sample was verified from a clean Git-only export of source commit
`07b031d2016e1891c903e9560fa56f527bc5f85d`, without importing old prepared
inputs or candidate outputs. The offline suite ran 258 tests with two unrelated
optional skips using Python 3.12 and PyArrow 21.0.0.

The pinned dataset and repository were downloaded and rebuilt. The complete
570-file repository and neutral task passed input verification. Both generated
Skills were newly published and downloaded for exact byte comparison:

| Skill | Published version | Package files | Uncompressed bytes |
|---|---:|---:|---:|
| software-repair | 4 | 307 | 2,843,008 |
| software-repair-repository-data | 3 | 271 | 1,973,301 |

These are versions returned in the verification project, not values to copy
into another project. Always use your own publication record.

A distinct Microsoft-harness agent used the existing `gpt-5.6-sol` deployment.
One fresh invocation loaded both Skills, ran the standalone verifier and
delivered `repair.json` and `report.md`. The candidate changed only
`src/_pytest/logging.py` and applied cleanly to the pristine repository.
A separate read-only audit re-fetched the response and both files and
reconciled their contents with the original download fingerprints.

Fresh official Docker evaluations used the pinned image and verified its
complete tracked source tree before running:

| Evaluation | Issue test | Existing tests | Resolved |
|---|---|---|---|
| Source-unchanged negative control | 1 failed | 15 passed | No, as expected |
| Benchmark reference fix | 1 passed | 15 passed | Yes |
| Newly downloaded agent candidate | 1 passed | 15 passed | Yes |

The candidate was graded from its original recorded JSON hash:
`fe72f7209afe9ed0ac425a541cd2ebdcead71d114809f56fec1c877f2d675bcc`.
Its decoded patch hash differs from the reference fix's hash; the patches
are not byte-identical. Both passed this task's required tests, which does not
establish that their behavior is equivalent for every input.

No candidate was reconstructed from a report or substituted with an older
artifact. Controls and candidate used separate fresh run/log directories;
the official reports, summaries, image identities and patch provenance were
cross-checked after execution. Raw data and execution records remain ignored.

This is one successful real task, not a statistically meaningful repair score,
an unseen-code guarantee or evidence of general model superiority. The
original issue gives substantial diagnostic guidance. Agent-side test claims
remain distinct from these independently observed official results.
