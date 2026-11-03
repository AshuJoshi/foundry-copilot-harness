# Does an instruction preference change tool use?

Compare two versions of the same Copilot harness agent. Keep the model, tools
and task prompts fixed; add one instruction preference to the second version.

The question is not just whether an answer is right. It is whether an agent
with execution capabilities chooses to use them when the user requests an
outcome rather than a method.

## The fixed experiment

The [experiment definition](experiment.json) contains three prompts:

- How many prime numbers are there below one million?
- What is 48271 multiplied by 79613?
- What is 7^77 mod 1,000,003?

Each prompt runs twice under each instruction profile: **12 requests**, each
in a fresh conversation. The schedule alternates profile order between the
two repetitions. These are simple behavioral probes, not a statistical or
general reasoning benchmark.

The neutral instructions allow the agent to choose its approach. The
tool-preferring version appends exactly:

> When the user asks for computation, data transformation, debugging, or code
> execution, prefer using the tools instead of answering from memory.

The prompts do not change. The agent has no published Skill, external
connection or declared tool block. Managed built-ins remain available:
absence of `tools` does not make it tool-free. This experiment changes a
preference, not tool permissions or an enforced execution requirement.

## Source guide

| Source | Purpose |
|--------|---------|
| [azure.yaml](azure.yaml) | Parameterized neutral Microsoft-harness definition |
| [experiment.json](experiment.json) | Exact prompts, reference integers, instruction suffix and repetition count |
| [method_selection.py](../../scripts/method_selection.py) | Local rendering/preparation, pinned-version checks, bounded schedule, report and read-only audit |
| [tests](../../tests/test_method_selection.py) | Independent arithmetic references, instruction-only differences, scheduling, recording and no-retry controls |
| [shared client](../../harness_client.py) | Current public-preview Responses transport with explicit conversations |

Reference answers belong to the controller/evaluator, not the agent prompt.
The entire experiment definition is not uploaded to the agent.

The driver reuses current recording helpers instead of importing the old
exploration workspace. It records source fingerprints but executes no Git
commands. Owner-created committed-source export validation is a separate step.

## 1. Offline checks

Run from the repository root with Python 3.12+:

```bash
python3 -m unittest discover -s tests -p 'test_method_selection.py' -v
```

The tests independently verify prime counting, multiplication and modular
power. They use scripted service responses; no Azure authentication,
deployment or model usage is involved.

## 2. Render both instruction profiles

Use existing approved project and model values, following the
[CLI installation guide](../company-research/README.md#install-and-check-the-tools).
Select a distinct new remote name for this experiment. Neither rendering
command makes an Azure call.

```bash
export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export AZURE_SUBSCRIPTION_ID="<subscription-id>"
export MODEL_DEPLOYMENT="<existing-model-deployment>"
export INSTRUCTION_AGENT_NAME="<new-distinct-agent-name>"
export AZD_ALPHA_ENABLE_AGENTS_MANAGED_HARNESS=true

python3 -m scripts.method_selection render \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --agent-name "$INSTRUCTION_AGENT_NAME" \
  --model-deployment "$MODEL_DEPLOYMENT" \
  --output .local/instruction-selection/neutral/azure.yaml
```

```bash
python3 -m scripts.method_selection prepare \
  --manifest .local/instruction-selection/neutral/azure.yaml \
  --output .local/instruction-selection/tool-preferring/azure.yaml
```

Review both manifests. Only instructions should differ. New definitions use
`microsoft_copilot_preview`; the current client can also read unchanged legacy
harness agents. Do not switch the harness of an existing agent.

## 3. Deploy only with approval

Use the [existing-project deployment procedure](../company-research/README.md#5-deploy-the-agent)
for each rendered folder, replacing that guide's service key with
`outcome-exploration-agent`. Set the selected project's ARM ID, endpoint and
resource group explicitly. Do not create or resize a model or run provisioning
when only agent deployment was approved.

Deploy the neutral definition first and record its active numeric version.
Deploy the tool-preferring definition under the **same new agent name**, creating
a second immutable version. Verify that the earlier neutral version remains
active and available.

This intentional version creation differs from casually overwriting an old
agent. Both versions must have identical model/harness/capabilities and differ
only in instructions. No Skill publication or separate output-file grant is
required by these text-only tasks.

## 4. Run the twelve-request comparison

This command authorizes the registered **12 paid requests**, not a model-specific
rescue loop. Use the actual versions returned by deployment:

```bash
python3 -u -m scripts.method_selection run \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --agent-name "$INSTRUCTION_AGENT_NAME" \
  --neutral-version "<active-neutral-version>" \
  --tool-version "<active-tool-preferring-version>" \
  --output-dir .local/instruction-selection/run \
  --allow-paid-invocations
```

The runner checks both pinned definitions before any request, records the exact
schedule and source fingerprints, and saves each request before submission.
Every trial starts a fresh conversation. Failed requests stop the remaining
schedule without automatic retries. Output directories cannot be reused.

The 300-second default controls HTTP/polling, not a hard total wall-clock
deadline, remote cancellation or billing cap. Keep the client awake; inspect
the original conversation after an uncertain transport result.

The report retains failed/missing coverage rather than counting it as a
completed answer. Presence of the expected integer is a screening check,
**not automatic correctness credit**: a response may also contain a
contradiction or an incorrect explanation.

## 5. Audit and interpret

Reconstruct the local report without a new invocation:

```bash
python3 -m scripts.method_selection compare \
  --output-dir .local/instruction-selection/run
```

Re-fetch recorded completed responses and check their original user turns:

```bash
python3 -m scripts.method_selection audit \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --run-dir .local/instruction-selection/run \
  --output-dir .local/instruction-selection/audit
```

Audit is read-only. Partial coverage remains partial. The report checks the
registered schedule, immutable versions, model, original request/conversation,
source fingerprints and actual response-based summary before counting trials.

Review actual commands, exit codes, stdout and the entire final answer:

- A shell call alone does not prove the task calculation succeeded.
- No parent tool calls does not prove recall or expose private reasoning.
- Parent-visible activity may omit nested subagent calls.
- Service duration is not private thinking time or end-to-end checked delivery.
- Token totals/cache subsets are not a verified dollar-cost calculation.
- Same prompt/configuration does not guarantee the same execution path.

## Historical result and migration status

The earlier research experiment completed twelve calls on October 19, 2026.
Successful visible Python computation appeared in **2/6 neutral** and
**6/6 tool-preferring** runs. All twelve final answers matched independent
references. That was a small, separate experiment, not part of the later
nine-model battery or a comparison against another harness.

This migration preserves the exact prompts, reference values, schedule and
single instruction suffix. Historical raw runs were not copied into public
source. The original 2/6 versus 6/6 outcome is not promised for future runs.

### New public-preview verification: November 3, 2026

The owner committed the migrated source as
[22a5eae](https://github.com/AshuJoshi/foundry-copilot-harness/commit/22a5eae989e402f301f3a33ba3fa7bfd15967ae4).
Its clean Git-only export matched all 80 reviewed source files and passed
**214 offline tests without skips**, including existing Docker and PyArrow
controls. The exact original experiment file, neutral instructions and
schedule were checked for preservation.

With explicit approval, a distinct new Microsoft-harness agent was deployed
in the existing Canada Central project, using the existing model deployment.
Neutral and tool-preferring profiles were pinned as two immutable versions
of that same new agent. Their persisted definitions differed only in the
single instruction preference; both versions remained active and accessible.

The registered twelve requests completed, each in a distinct conversation,
with no client resubmissions. Read-only retrieval independently matched all
twelve responses and their single original user turns. Manual inspection
checked the full answers, actual commands, exit codes and stdout:

| Task | Neutral successful Python trials | Tool-preferring successful Python trials | Correct final answers |
|------|---------------------------------:|-----------------------------------------:|----------------------|
| Prime count | 0/2 | 2/2 | 4/4 |
| Multiplication | 0/2 | 2/2 | 4/4 |
| Modular power | 2/2 | 2/2 | 4/4 |
| **Total** | **2/6** | **6/6** | **12/12** |

All eight visible Python calculations exited zero with the independently
expected result. The four remaining neutral responses had no visible parent
tool calls. No private reasoning or recall mechanism was inferred.

This new trial reproduced the earlier behavioral observation, but the two
rounds were not pooled. Two repetitions per task/profile are still a small
probe, not evidence of statistical significance, a general tool-use guarantee
or superiority over another harness. No sampling/reasoning-effort override,
Skill or external connection was introduced.

All 23 pre-existing agents and five model deployments remained unchanged.
One new agent with two pinned versions was added. The initial preflight
detected a changed shell-default subscription and stopped before any write;
subsequent commands used the approved subscription explicitly without changing
the global default. That preflight stop was not a failed model trial.

All concrete manifests, responses, reports and logs belong under ignored
`.local/`. Do not commit actual run outputs. The deployed experiment agent
remains until explicitly approved cleanup; do not delete a shared project/model.
