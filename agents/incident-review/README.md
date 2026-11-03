# Review an incident diagnosis with another agent

An investigator analyzes the recorded telemetry. A challenger forms its own
assessment before seeing the investigator's answer, then checks concrete claims.
The investigator receives the validated critique and revises once.

This workflow reuses the [incident investigator's prepared data and Skills](../incident-investigation/README.md).
It adds three role definitions and a local controller, not a monitoring
connection, A2A integration or remediation system.

## Three modes, different questions

| Mode | Roles and calls | What it tests |
|------|-----------------|---------------|
| `natural` | Investigator initial, challenger independent, challenger critique, investigator revision: four calls | Independent-first assessment and actual peer file handoffs |
| `faulty-proposal-control` | Challenger independent and critique: two calls | Whether critique detects the deliberately flawed human-authored proposal |
| `self-review-control` | Initial, self-check, self-critique, final revision: four calls in one conversation | Whether one agent can improve its answer without an independent peer |

No reviewer must invent an objection. `agree`, `revise` and
`insufficient_evidence` are valid evidence-dependent outcomes. Passing hashes
and schemas does not establish causal correctness or that two agents are better.

The [observed results](#observed-results) include completed peer/control
workflows and a self-review attempt that stopped on an output-contract error.
Use that distinction when evaluating the baseline.

## Source and artifact boundaries

| Source | Purpose |
|--------|---------|
| [investigator.azure.yaml](investigator.azure.yaml) | Initial investigation and bounded revision role |
| [challenger.azure.yaml](challenger.azure.yaml) | Independent assessment and later critique role |
| [self-review.azure.yaml](self-review.azure.yaml) | Single-agent baseline, explicitly not independent |
| [schemas.json](schemas.json) | Restricted contract vocabulary, not a general JSON Schema implementation |
| [faulty-diagnosis.json](faulty-diagnosis.json) | Evaluator-only fixture and rubric; never a natural model output |
| [incident_review.py](../../incident_review.py) | Frozen artifact hashes, exact source cells, quotations and JSON-pointer checks |
| [run_incident_review.py](../../scripts/run_incident_review.py) | Explicit role rendering, bounded stage order and read-only audit |
| [contract tests](../../tests/test_incident_review.py) | Positive and negative artifact/evidence controls |
| [controller tests](../../tests/test_incident_review_run.py) | Actual attachment encoding, request recording and stage/history boundaries |

Only the fixture's neutral `diagnosis` object goes to the challenger in the
planted-error mode. The fixture wrapper, expected objections and rubric never
become model inputs. The errors deliberately confuse caller attribution,
span status with HTTP, and baseline metrics with demonstrated recovery.

The controller invokes separately deployed prompt agents. There are no direct
agent-to-agent calls. Each natural stage starts a new conversation and receives
explicit validated attachments. The independent assessment is frozen before
critique. Self-review uses one continuous conversation and keeps initial
attachment filenames stable across turns. Workspace paths are not shared
between agents.

The three empty `tools` lists preserve the original behavior: no external
declared tools, but managed built-ins remain available. Local-only instructions
are not an enforced network-egress boundary. All roles remain advisory.

## 1. Verify the incident foundation

Follow the [incident guide](../incident-investigation/README.md) to prepare
case-001, publish and independently verify its three complete packages, and
obtain `publication.json`. Reuse that verified publication rather than
publishing new Skills merely to add reviewer roles.

Use the same model and exact Skill versions for the investigator and challenger.
This bounded review experiment uses case-001.
Telemetry attribution and download terms are in the
[incident data notice](../incident-investigation/DATA-NOTICE.txt).
Raw telemetry and run outputs are not committed.

Run offline controls from the repository root:

```bash
python3 -m unittest discover -s tests -p 'test_incident_review*.py' -v
```

No model calls, Azure sign-in or deployment occur in these tests.

## 2. Render new role definitions locally

Use the approved project values from the incident guide. Choose distinct new
remote names; do not overwrite earlier agents or switch their harness.

```bash
export INVESTIGATOR_NAME="<new-investigator-name>"
export CHALLENGER_NAME="<new-challenger-name>"

python3 -m scripts.run_incident_review \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --challenger "$CHALLENGER_NAME" \
  --challenger-version 1 \
  --investigator "$INVESTIGATOR_NAME" \
  --prepared-dir .local/incident-prepared/case-001 \
  --publication .local/incident-publication/case-001/publication.json \
  --challenger-manifest .local/incident-review-deploy/challenger/azure.yaml \
  --investigator-manifest .local/incident-review-deploy/investigator/azure.yaml \
  --model-deployment "$MODEL_DEPLOYMENT" \
  --output-dir .local/incident-review-unused \
  --render-only
```

Rendering makes no model request. Its version argument is a required parser
input, not evidence that the agent exists at that version. Use actual returned
versions for invocation. For the self-review role, select `--mode self-review-control`,
use a distinct self-review name in `--challenger`, and render its manifest separately.

## 3. Deploy only with explicit approval

Deploy each rendered manifest using the incident guide's existing-project
azd environment setup and `AZD_ALPHA_ENABLE_AGENTS_MANAGED_HARNESS=true`.
The service keys are `incident-review-investigator`,
`incident-review-challenger` and `incident-review-self-review`.
Use scoped `azd deploy`, not infrastructure provisioning.

Check each active definition against source, its selected model and the same
three immutable Skill versions. Grant project-scoped Foundry User to each
actual instance principal only if needed, with approval; allow propagation.
Do not grant subscription-wide access or create another model/project.

## 4. Run the natural workflow

The following explicitly authorizes up to four paid requests:

```bash
python3 -u -m scripts.run_incident_review \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --investigator "$INVESTIGATOR_NAME" \
  --investigator-version "<active-investigator-version>" \
  --challenger "$CHALLENGER_NAME" \
  --challenger-version "<active-challenger-version>" \
  --prepared-dir .local/incident-prepared/case-001 \
  --publication .local/incident-publication/case-001/publication.json \
  --investigator-manifest .local/incident-review-deploy/investigator/azure.yaml \
  --challenger-manifest .local/incident-review-deploy/challenger/azure.yaml \
  --output-dir .local/incident-review-natural \
  --mode natural \
  --allow-four-paid-invocations
```

For the planted-error control, use the challenger with
`--mode faulty-proposal-control --allow-two-paid-invocations` and a new output
directory. For self-review, use the self-review name/version/manifest in the
challenger arguments and `--mode self-review-control --allow-four-paid-invocations`.
Neither mode requires an investigator invocation.

Stages stop at a failed execution, materialization, upload or validation check.
No request is automatically retried. Preserve failed runs and inspect remote state;
local HTTP timeouts do not cancel remote execution or cap billing.

Outputs include separate stage directories, actual downloaded diagnosis/critique
files, reports, frozen bindings, receiver hashes and request/response records.
All remain beneath ignored `.local/`. Use committed source and preserve the
recorded source fingerprints with each run.

## 5. Audit and judge the result

Re-fetch a complete saved workflow without paid calls:

```bash
python3 -m scripts.run_incident_review \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --challenger "$CHALLENGER_NAME" \
  --challenger-version "<active-challenger-version>" \
  --challenger-manifest .local/incident-review-deploy/challenger/azure.yaml \
  --prepared-dir .local/incident-prepared/case-001 \
  --publication .local/incident-publication/case-001/publication.json \
  --audit-run-dir .local/incident-review-natural \
  --output-dir .local/incident-review-natural-audit
```

Audit retrieves each originally bound agent version's response/files, checks
the original task and attachment hashes, actual receiver verification, Skill
execution and byte-identical output files. Use the unchanged original source
and prepared assets. It does not resume a partial run or certify semantic truth.

Human review should check whether an objection is warranted, whether accepted
claims are supported, whether the independent answer changed after disclosure,
and whether any improvement justifies extra calls/time/usage. Compare with
self-review without pretending different histories are identical experiments.
Planted errors are controls, never natural model mistakes.

## Observed results

**Testing date: November 3, 2026 (Pacific time).** Three roles used the same
Sol5.6 deployment in Canada Central and identical pinned case-001 Skills.
The natural workflow revised two concrete claims; the planted-error control
identified all three intended problem types. The self-review attempt stopped
on an output-contract error rather than producing an unchecked revision.

| Live mode | Actual requests | Outcome | Independent verification |
|-----------|----------------:|---------|--------------------------|
| Natural peer review | 4 | Completed; critique requested revision with two findings and five accepted claims | Four responses and eight output files re-fetched; task, input hashes, exact evidence and file bytes checked |
| Planted-error control | 2 | Completed; six findings covered all three intended problem types; three claims retained | Two responses and four output files re-fetched; fixture wrapper/rubric excluded from model input |
| Self-review | 3 | Stopped after self-critique failed the existing evidence-use contract; final revision not submitted | All three responses and six delivered files re-fetched; execution/receiver checks passed, original critique rejection preserved |

There were **nine actual paid requests and zero resubmissions** against a
planned ten-request schedule. The natural workflow used four distinct
conversations, the control used two, and self-review used three turns in one
conversation. This is not a completed four-call self-review baseline.

### What the critique established

In the natural workflow, the challenger identified an unproven request-level
log/span match and an onset metric described as a baseline. The investigator's
revision explicitly accepted both qualifications. The bounded cartservice
overflow conclusion remained supported; exact upstream input provenance and
request-level linkage remained unknown. Timestamp precision also limits exact
ordering claims.

In the planted-error control, manual review found all three predeclared problem
types: caller symptoms treated as conclusive origin, span status treated as
HTTP, and pre-failure metrics treated as demonstrated restart/recovery.
The six findings overlap; they are not six independent planted bugs.
This is one human-authored control, not a population accuracy estimate.

Self-review's self-check added useful baseline and full-window information.
However, its critique included evidence `C8` without referencing it from any
finding or accepted claim. The unchanged validator rejected that output.
The original failure and files were retained; no validator relaxation, output
editing or replacement model request turned it into a success. Its final
revision was not executed.

These results demonstrate working peer/control handoffs and safe stopping on
one failed self-review contract. They do not establish superiority over a
complete matched-budget self-review baseline, statistical reliability, or
successful remediation. Mechanical checks do not certify every narrative claim.

The committed source passed 200 offline tests without skips, including the
existing Docker and PyArrow controls. Raw responses, reports, telemetry,
audit downloads and failed-run evidence remain under ignored `.local/`;
only this curated summary belongs in source control.
