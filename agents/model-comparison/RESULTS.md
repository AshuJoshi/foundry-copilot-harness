# Verified results: nine model configurations

**Testing date: October 27, 2026 (Pacific Daylight Time; October 28 UTC).**
This is a point-in-time snapshot of the model versions and deployment
configurations listed below.

The battery used the earlier GitHub-named preview contract. The source now
authors new agents with the Microsoft-named public-preview contract. A quick
compatibility check is not a rerun of this nine-model battery; these scores
and measurements remain the original point-in-time results.

Sol6.1, Astra, Terra5.6 and Terra6.1 met all 18 task-output contracts in this
battery. Other configurations exposed differences in structured delivery,
calculation and whether a requested file was delivered.

These are results for small, fixed tasks in one Foundry project, not a universal
model ranking or an availability guarantee. Use the
[runner guide](README.md) to compare deployments available to your project.

## What was checked

- Nine execution-and-file readiness requests.
- Six tasks, three repetitions per model: **162 distinct scored requests**.
- **171 model requests in 171 distinct conversations**, with no resubmissions.
- Returned agent versions, model bindings, exact prompts and supplied inputs.
- Actual uploaded files, independently re-fetched from Foundry.
- Generated Python modules tested in restricted Docker containers.
- Business-evidence values, source cells, verdicts and uncertainty boundaries.

The [battery](battery.json), [fixtures](fixtures/) and
[grader](../../model_comparison_eval.py) define the task contracts.
Agent instructions and enabled tools stayed fixed; the declared model binding
changed. No model-specific rescue prompt, Web IQ connection or Skill was used.

## Task-output results

Each row contains 18 evaluated tasks. A pass requires the requested values or
code, delivery format and actual artifact where requested.

| Model label | Passed | Observed task-output misses |
|-------------|--------|-----------------------------|
| Sol6.1 | 18/18 | None |
| Astra | 18/18 | None |
| Kimi K3 | 13/18 | Four Markdown/prose-wrapped JSON answers; one prose-only evidence answer |
| GLM 5.3 | 11/18 | Six Markdown-wrapped JSON answers; one completed debugging response with no delivered file |
| DeepSeek Flash | 13/18 | Five prose/Markdown-wrapped JSON answers |
| Grok 4.7 | 17/18 | One Markdown-wrapped JSON answer |
| Terra5.6 | 18/18 | None |
| Luna5.6 | 17/18 | One incorrect CSV transformation total |
| Terra6.1 | 18/18 | None |

That is **143 passing task outputs out of 162**.

### Content and delivery are different checks

There were 17 JSON-delivery failures. Sixteen contained the expected structured
content inside Markdown or surrounding prose. A separate content-only check
confirmed that content, without replacing the original failed delivery score.

Kimi's remaining evidence answer gave totals and verdicts in prose, but did not
deliver the required JSON, copied evidence rows or limitations. That is more
than a removable code fence.

Luna's unsuccessful transformation returned net `20.60` instead of `18.50`.
Its JSON was valid, but its category totals were wrong. Valid syntax alone
does not establish correct arithmetic.

### Code must actually arrive

The battery contained **54 coding tasks**. The agents delivered **53 actual
Python modules**, and all 53 passed their independent tests: 43 checks for
implementation or 44 for debugging.

One GLM debugging response was marked completed by the service but contained
no final answer or uploaded module. No code correctness credit was assigned.
This identifies a missing task deliverable, not the internal cause of that
empty response.

The evidence review also matters. All 26 available structured evidence answers
preserved the distinction between transaction arithmetic and unsupported
profit or shipment-cause claims. The remaining evidence attempt lacked its
required structured output. Schema and numeric checks do not certify every
explanatory sentence.

## Keep infrastructure out of model-quality scores

The schedule finished across explicitly recorded phases after client
connectivity interruptions. Previously submitted requests were not repeated.
Two already-completed remote results were retrieved read-only; their actual
code passed the same independent checks.

Client-network interruptions and a readiness-checker false negative are not
model-quality failures. Original diagnostic records remain preserved
separately. Interrupted timings are excluded, and pre-model DNS failures do
not count as model invocations. These results therefore describe verified task
outputs, not uninterrupted end-to-end availability.

This nine-model battery is separate from earlier exploratory comparison rounds.
Their attempts and scores were not pooled into this table.

## Timing: distinguish the clocks

The table below includes all non-interrupted completed scored requests,
including unsuccessful task outputs. It measures completion time, not time
to a correct answer. Readiness is excluded.

| Model label | Timing observations | Mean client wall seconds | Mean reported service seconds |
|-------------|--------------------:|-------------------------:|------------------------------:|
| Sol6.1 | 17 | 25.15 | 19.59 |
| Astra | 18 | 24.98 | 18.61 |
| Kimi K3 | 17 | 34.74 | 26.12 |
| GLM 5.3 | 18 | 53.05 | 46.17 |
| DeepSeek Flash | 18 | 28.69 | 20.72 |
| Grok 4.7 | 18 | 45.50 | 38.89 |
| Terra5.6 | 18 | 16.83 | 10.67 |
| Luna5.6 | 18 | 15.07 | 9.17 |
| Terra6.1 | 18 | 13.91 | 9.00 |

Client wall time includes local overhead, file handling and grading. Service
duration is a different boundary, not a measurement of private thinking time.
The two excluded observations were the interrupted client requests whose
artifacts were subsequently retrieved.

For a matched task example, Terra6.1 had lower mean client wall time than
Terra5.6 on all six tasks, with three observations per task. It did not have
lower reported service time on every task: constraints averaged 6.29 client
seconds / 2.67 service seconds for Terra6.1, versus 7.05 / 1.67 for Terra5.6.

Do not treat the aggregate means as a latency ranking. Task success, provider
configuration, allocation, caching and execution paths differ, and continuation
timings came from later phases. Cold/warm lifecycle and complete internal
model-call counts were not available.

## Tested model bindings

| Label | Catalog model | Version | Deployment type | Capacity units |
|-------|---------------|---------|-----------------|---------------:|
| Sol6.1 | gpt-6.1-sol | 2026-09-29 | DataZoneStandard | 100 |
| Astra | gpt-6-astra | 2026-09-03 | DataZoneStandard | 100 |
| Kimi K3 | FW-Kimi-K3 | 1 | GlobalStandard | 200 |
| GLM 5.3 | FW-GLM-5.3 | 2 | GlobalStandard | 200 |
| DeepSeek Flash | FW-DeepSeek-V4.1-Flash | 1 | GlobalStandard | 100 |
| Grok 4.7 | grok-4.7 | 1 | DataZoneStandard | 80 |
| Terra5.6 | gpt-5.6-terra | 2026-07-09 | GlobalStandard | 50 |
| Luna5.6 | gpt-5.6-luna | 2026-07-09 | GlobalStandard | 50 |
| Terra6.1 | gpt-6.1-terra | 2026-10-26 | DataZoneStandard | 100 |

The project was in West US. Capacity units are recorded configuration values,
not interchangeable throughput across model families. The intended comparison
changed the agent's model binding; it did not hold provider infrastructure
identical. This roster is not a list of models available in every tenant or region.

Reported usage was retained, including cache and reasoning subsets where
provided. Pricing, actual billed dollars, backend-call counts and internal-loop
counts were not verified. Missing metrics are unknown, not zero.

## Reproduce the workflow

Follow the [deployment, invocation and audit guide](README.md). Offline tests,
model deployment, agent deployment, invocation and independent artifact checks
are separate stages. A passing offline suite does not establish a successful
remote invocation.

This verification used an isolated file-only source copy, not a Git-only
export of a committed source revision. Clean Git-export verification remains
a separate reproducibility check. Fresh installation of the latest tooling
bundle was not part of this run; the
[tool guide](../company-research/README.md#install-and-check-the-tools) identifies
the tested versions and installation limitation.

This page is a curated summary of actual, independently checked agent runs
and is intended for source control. Raw responses, candidate code, generated
run reports and logs remain under ignored `.local/` and are not intended for
source control.
