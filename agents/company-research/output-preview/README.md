# What the output files look like

These are documentation previews, not files downloaded from an agent run.
They show the three deliverables before you deploy the agent.

| Preview | What to look for |
|---------|------------------|
| [report.md](report.md) | Findings, source citations, unknowns, scenario table and follow-up questions |
| [evidence.json](evidence.json) | Source IDs connecting the brief to its source ledger |
| [scenarios.json](scenarios.json) | The exact output of the bundled calculator and assumptions |

**ExampleCo is fictional.** All research statements, source titles and
timestamps were authored to demonstrate the format. The `example.com`,
`example.org` and `example.net` URLs are reserved documentation addresses.
They were not retrieved through Web IQ and are not research evidence.

The scenario values are different: they are calculated from the checked-in
illustrative assumptions. The offline tests compare the entire scenario
preview with the calculator's output. They are still not company financials.

## What changes in a real run?

The company, sources, research date, findings and report length depend on your
request and the available evidence. A successful run returns actual retrieved
source URLs. The sample calculator results remain the same while its
assumptions are unchanged.

The runner saves responses, logs and downloaded files beneath `.local/`, which
is excluded by the repository's ignore file. Keep run data there. These authored
previews are maintained separately; do not copy a run directory here or attach
these files to an agent as research input.

## Reproduce the scenario values without Azure

From the repository root:

```bash
mkdir -p .local
python3 skills/company-research/calculate_scenarios.py > .local/scenarios-preview.json
```

Compare that local file with [scenarios.json](scenarios.json), or run the offline
tests to check JSON equality. The command does not research ExampleCo or invoke
any service.
