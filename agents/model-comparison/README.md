# Compare models behind the same harness agent

Run the same tasks with different model bindings to see how model choice
affects usable results, tool behavior, latency and reported usage.

The runner deploys one new comparison agent with a version for each model.
The versions have the same instructions and enabled tools; only the declared
model binding changes. Every task starts in a fresh conversation.

Use the results to evaluate a configuration for your application. A correct
calculation, a correctly formatted answer and a delivered code file are different
parts of a completed task.

The [verified nine-model results](RESULTS.md) show task-output scores, actual
artifact checks, timing boundaries and the tested deployment configurations.

## What is in the battery?

Each model first receives a readiness check: verify a supplied file, execute
its Python script and upload the actual result file. The result must contain
the expected value, verification marker and Python version.

Ready models then receive six tasks, three times each:

| Task | Required result | Check |
|------|-----------------|-------|
| Exact computation | JSON with prime count, multiplication and modular power | Exact integers and schema |
| Constraints | JSON with a minimum-cost job/depot assignment | Independent enumeration of the feasible optimum |
| CSV transformation | JSON with Decimal totals and exclusions | Duplicate preservation, invalid-row handling and exact totals |
| Implementation | Uploaded interval-normalization Python module | 43 tests, including malformed inputs, overlap/adjacency and mutation |
| Debugging | Uploaded signed-account reconciliation repair | 44 tests, including duplicates, conflicts, signed values and mutation |
| Business evidence | JSON with source cells and supported/contradicted/unknown verdicts | Exact values/citations and bounded evidence claims |

The [battery](battery.json) contains the exact prompts. The
[fixtures](fixtures/) contain only task inputs; evaluator answers and hidden
tests are not supplied to the agent.

The Python starter fixtures deliberately leave work for the agent:
the implementation starter raises `NotImplementedError`, and the debugging
starter contains logic errors to repair. They are challenge inputs, not
solutions claimed to have passed. Each coding request asks the deployed agent
to write and upload its completed module. The caller downloads that actual
file, then the independent Docker grader runs the 43 implementation or 44
debugging checks. Those local checks are not additional model invocations.

The neutral instructions let the model choose a method. The tool profile enables
filesystem read/write and shell execution, with other Copilot toolsets disabled
by default. The task boundary prohibits network access, package installation
and delegation.

New authoring uses `microsoft_copilot_preview` and
`microsoft_copilot_toolset_preview`, with camelCase `defaultConfig` in the
manifest. Persisted REST definitions use `default_config`; the checker
normalizes that field without changing enabled capabilities. Existing legacy
harness/toolset names remain readable. The client sends the current Microsoft
preview header. These changes do not alter the battery, neutral instructions,
fixtures, grader or expected answers.

## Choose the model deployments

The [roster template](models.example.json) lists nine model labels:
Sol6.1, Astra, Kimi K3, GLM 5.3, DeepSeek Flash, Grok 4.7, Terra5.6,
Luna5.6 and Terra6.1. GPT OSS and Sol5.6 are not in this roster.

The labels identify result rows. For each one, supply the name of an actual
model deployment in your existing Foundry account. This workflow does not
deploy, resize or delete models.

```bash
mkdir -p .local
cp agents/model-comparison/models.example.json .local/comparison-models.json
```

Edit `.local/comparison-models.json` and replace every deployment placeholder.
You may select one to ten models by removing entries, but IDs and deployment
names must be distinct. All selected deployments must already be in the
`Succeeded` state. Resolve quota or model-availability gaps before preparation;
the runner does not silently substitute another model or region.

The maximum number of requests is:

```text
model count x (1 readiness + 6 tasks x 3 repetitions)
```

That is **171 requests for the nine-model template**, or 38 for two. The runner
also supports ten models (190 requests). Readiness is excluded
from scored task quality.

The [Terra6.1 deployment request](model-deployments/terra61.json) records one
explicit model/version/type/capacity configuration for creating that model
separately. It is not applied by the agent runner. Check current availability
and quota, and choose a new deployment name before using a model-creation API.

## Prerequisites

- Python 3.12+ on Linux or WSL.
- Azure CLI, Azure Developer CLI and the Foundry extensions. See
  [tool installation](../company-research/README.md#install-and-check-the-tools).
- An existing Foundry project and the model deployments in your roster.
- Caller access to read project/model state and create agents.
- Permission for any required project-scoped agent-instance file-access grant.
- Docker for isolated generated-code grading.

No Web IQ connection or published Skill is needed. The caller, grader and
controller use Python's standard library. The grader uses Linux resource limits;
run it inside WSL rather than native Windows Python.

### Prepare the grading image

The code grader requires an official Python image pinned by digest. The image
must already be present before preparation. This is the image used for the
grader-control checks:

```bash
export PYTHON_GRADING_IMAGE="python@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"
docker image inspect "$PYTHON_GRADING_IMAGE"
```

If it is missing, download it deliberately before continuing:

```bash
docker pull "$PYTHON_GRADING_IMAGE"
```

The grader never downloads an image automatically. It runs each candidate in
a network-disabled, read-only container as an unprivileged user, with resource
limits and a 30-second execution bound. Only the candidate, test inputs and
dispatch code are mounted. Expected answers, credentials, the repository and
Docker socket are not mounted.

Do not run generated model code directly on your workstation.

## 1. Run the offline controls

```bash
MODEL_STUDY_TEST_IMAGE="$PYTHON_GRADING_IMAGE" \
  python3 -m unittest discover -s tests -p 'test_model_comparison*.py' -v
```

The actual container controls require correct implementations to pass and the
supplied buggy implementation to fail. Without `MODEL_STUDY_TEST_IMAGE`, that
control is explicitly skipped; a skipped test does not establish a working
container grader.

The six tasks and inputs retain the original battery's fingerprints. Changes
to the tasks or grading logic should be treated as a new comparison, not silently
mixed into earlier results.

## 2. Prepare a new comparison

Use your existing project values. Sign in with `az login` and `azd auth login`
for the intended tenant before running the preflight.

```bash
export AZURE_SUBSCRIPTION_ID="<subscription-id>"
export AZURE_RESOURCE_GROUP="<existing-resource-group>"
export AZURE_AI_ACCOUNT="<foundry-account>"
export AZURE_AI_PROJECT="<foundry-project>"
export AZURE_LOCATION="westus"
export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export COMPARISON_AGENT_NAME="<new-distinct-agent-name>"
export MAX_INVOCATIONS="171"
export LAUNCH_CUTOFF="<future-ISO-timestamp-with-timezone>"

python3 -m scripts.compare_models prepare \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --account "$AZURE_AI_ACCOUNT" \
  --project "$AZURE_AI_PROJECT" \
  --region "$AZURE_LOCATION" \
  --agent-name "$COMPARISON_AGENT_NAME" \
  --models .local/comparison-models.json \
  --python-image "$PYTHON_GRADING_IMAGE" \
  --max-invocations "$MAX_INVOCATIONS" \
  --stop-starting-at "$LAUNCH_CUTOFF" \
  --output-dir .local/comparison-study
```

Set `MAX_INVOCATIONS` to 19 times the number of selected models.
The cutoff must be an absolute future timestamp with a timezone, not just
an hour of the day. No new request starts at or after it; an accepted remote
request may still be running.

Preparation makes read-only Azure calls and writes local manifests/state. It
checks the account/project/endpoint identity, region, deployed models, image
and new-agent name. It saves the complete schedule, source/input hashes, model
versions and capacities before any agent deployment or paid request.

It refuses an existing output directory or an agent-name collision. Keep
concrete model bindings, prepared configuration and results under ignored
`.local/`.

## 3. Deploy the model-only versions

This command creates agent versions and, if missing, project-scoped Foundry
User assignments for their instance identities:

```bash
export AZD_ALPHA_ENABLE_AGENTS_MANAGED_HARNESS=true

python3 -m scripts.compare_models deploy \
  --study-dir .local/comparison-study \
  --allow-azure-writes \
  --allow-project-role-grants
```

Only the selected new agent is deployed. Existing agents and model deployments
are not changed. The runner verifies every active version against its source
definition, checks that earlier pinned versions remain available and waits
once for permission propagation.

Different versions may share an instance identity. The runner checks each
distinct principal only once; it does not duplicate its project grant.
It does not grant subscription-wide access.

If deployment fails, inspect the saved records rather than rerunning it into
the same study directory. A successful deployment is not a readiness result.

### Optional two-request compatibility check

For a quick deployment check rather than a full comparison, prepare a
**one-model** roster with the same `prepare` command, using
`--max-invocations 19` for its registered full schedule, and deploy a distinct
new agent name. Do not run the full `run` command. Instead:

```bash
python3 -u -m scripts.compare_models smoke \
  --study-dir .local/comparison-study \
  --max-invocations 2 \
  --allow-paid-invocations
```

```bash
python3 -m scripts.compare_models audit \
  --study-dir .local/comparison-study \
  --run-kind smoke \
  --output-dir .local/comparison-smoke-audit
```

This submits at most two requests: the existing file-execution readiness task
and the existing implementation task, with the same independent 43-check
Docker grader. It stops on a failure and never retries a request. Results stay
in the separate `smoke/` directory, not the full-battery report. It neither
reruns the nine-model battery nor changes the historical result scores.

The [public-preview verification summary](../../README.md#public-preview-verification)
records a successful two-request check with Sol6.1 and the upgraded CLI,
including the independent file audit and 43/43 implementation checks.

## 4. Run the full battery

This command authorizes paid requests up to the exact prepared maximum:

```bash
python3 -u -m scripts.compare_models run \
  --study-dir .local/comparison-study \
  --max-invocations "$MAX_INVOCATIONS" \
  --allow-paid-invocations
```

The schedule rotates model order across task groups and repetitions. Each
worker records its request before submission, checks the returned model and
agent version, verifies supplied-file consumption and downloads required files.
The independent grader runs after a completed result is delivered.

The safety rules are:

- A failed readiness check pauses that model.
- If the roster includes `sol61`, failure of that known baseline's readiness
  stops the batch for inspection.
- A later operational failure pauses that model's remaining tasks.
- A completed wrong answer is scored as wrong and does not pause the model.
- A local grading-infrastructure failure stops the whole batch.
- No request is automatically resubmitted.

Each agent worker has a 420-second local wall-clock limit, with progress
messages while it runs. The separate code grader has its own 30-second limit.
Terminating a local worker does not prove remote cancellation or cap billing.
Preserve the run and inspect service history after an uncertain failure.

The output directory cannot be reused. There is no automatic resume or
failed-task retry command. Missing coverage remains untested, not an incorrect
completed answer.

## 5. Audit and generate the report

```bash
python3 -m scripts.compare_models audit \
  --study-dir .local/comparison-study \
  --output-dir .local/comparison-audit

python3 -m scripts.compare_models report \
  --study-dir .local/comparison-study \
  --output-dir .local/comparison-report
```

Audit makes read-only response/file requests. It checks the original prompt,
model/version binding and delivered file bytes without invoking the model.

The report contains:

| File | Contents |
|------|----------|
| `report.md` | Task description, per-model outcomes and per-task coverage |
| `runs.csv` | Per-attempt correctness, failure, timing, token and visible-tool fields |
| `results.json` | Structured results, source fingerprints and missing-metric policy |
| `prompts.json` | Exact user prompts, common instructions and input hashes |

All run responses, generated code, logs, downloads and reports remain under
`.local/`. They are not documentation previews and should not be copied into
the repository's source.

### Continue only verified unsubmitted tasks

Do not retry a model request to repair an evaluator mistake. First preserve the
original source snapshot, requests, outcomes and audit. Recheck the existing
response and actual file against the requested contract.

The optional [completion tool](../../scripts/complete_comparison.py) can handle
an older readiness checker rejecting a permitted relative-path execution of
the supplied script. It accepts only a checker-function correction and the
explicit addition of non-retrying HTTP transport-error handling;
the remaining controller, tasks, fixtures and grader must match the original
source fingerprints and syntax. It waits for the original schedule to finish
and requires its read-only audit and report.

```bash
python3 -u -m scripts.complete_comparison \
  --study-dir .local/comparison-study \
  --original-source "<preserved-original-source-folder>" \
  --original-report .local/comparison-report \
  --original-audit .local/comparison-audit/audit.json \
  --model "<original-model-id>" \
  --max-invocations 18 \
  --output-dir .local/comparison-completion \
  --allow-paid-invocations
```

Set the count to the exact number of never-submitted tasks for that model.
In single-model mode, every existing job is excluded, even if its remote outcome
is unknown. A one-time claim beside the original study prevents a second continuation.
Do not delete that claim to bypass the replay guard.

If a continuation itself is interrupted, first audit and report its saved
records using its preserved source. A later phase must explicitly provide
every earlier `--previous-continuation` directory and matching
`--previous-source` folder. The tool validates that complete history against
the claims and original protocol, then excludes every prior submitted job.
It refuses an omitted, active or unaudited earlier phase. Each phase has a
new output directory and its own exact remaining-request count.

The tool makes no new readiness request and changes no agent definition.
It preserves the original readiness failure, audits the new outputs and writes
`combined-results.json`, `combined-runs.csv` and `combined-report.md` with
explicit original/continuation phases. The continuation runs later, not in the
original rotating order. Failed or uncertain submitted model requests are never
eligible for a retry.

After an explicitly approved client-network interruption, use
`--all-unsubmitted` instead of `--model` to finish remaining tasks across the
original roster. The exact count still needs confirmation. This mode also
admits a recorded DNS failure during conversation creation, but only when
there is no recorded model request, response, conversation ID or Responses
POST event. Such preflight events stay in a separate diagnostic section,
not the model-quality denominator. All selected models must pass a read-only
recheck of their original readiness responses and files.

This mode can continue tasks skipped after an earlier completed file-delivery
failure; that original failure remains a failed task output. A new completed
response that omits its required upload is also a delivery failure, not a
network failure. Genuine transport, protocol and grading-infrastructure errors
still stop the continuation without retries.

If a timed-out request completed remotely, `--timeout-reconciliation` can point
to a retained JSON inspection record with `original_directory`, `response_id`,
`artifact_sha256`, `paid_invocations: 0` and `resubmitted: false`. The tool
re-fetches that original response and actual code file, checks their binding
and grades the code in Docker. It reports verified correctness separately
from the preserved client failure and excludes interrupted timing. It never
submits a replacement request.
The option can be repeated for separately recovered requests, including an
incomplete HTTP response body. Original/continuation phases and interrupted
timings remain identifiable in the combined report.

## Read the results

Compare content and delivery separately. Extra Markdown can violate a JSON-only
contract even when the answer inside it is correct. A passing code module is
evidence for the supplied tests, not every possible application input.
Review business-evidence limitations manually as well as checking their schema.

Client wall time includes local overhead and grading. Service timestamps
measure a different boundary. Reported tokens, caching, providers and allocated
capacities can differ; token totals alone do not establish dollar cost.
Internal model-loop counts, complete backend-call counts, cold/warm lifecycle
and actual billed cost remain unknown.

Keep comparison rounds distinguishable when task policy, model versions or
service conditions change. Use these results to choose configurations for a
larger representative workload, rather than a universal model ranking.

## Cleanup

The comparison leaves its new agent/versions and any required project grants
in place. Review those resources before removing them; do not delete existing
models or other agents as part of routine cleanup. Excluding a model from a
roster does not delete its deployment or release another model's quota.
