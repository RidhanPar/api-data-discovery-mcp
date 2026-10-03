# Evaluation

Two harnesses, one labelled question set ([questions.yaml](questions.yaml), 45 questions,
written before any search or agent output was looked at).

| | Retrieval eval | Agent eval |
|---|---|---|
| Script | `make eval-retrieval` ([retrieval.py](retrieval.py)) | `make eval-agent` ([agent_eval.py](agent_eval.py)) |
| Needs an LLM | no | yes (`NORDLYS_LLM_PROVIDER` + credentials) |
| Measures | the search engine alone | the agent end to end, through the MCP server |
| Output | `results/retrieval.json` | `results/agent-<provider>-<model>.json` + `-answers.jsonl` |
| CI | gate on the `ci: true` subset | runs only when the CI has LLM credentials |

Both build a dedicated `<db>_eval` database from the canonical catalog, so results do not
depend on what else is in your working database, and both use the checksum-pinned local
embedding model, so retrieval numbers are reproducible bit for bit.

## Question categories

| category | n | what it tests |
|---|---|---|
| findable_api | 19 | a developer need phrased in business language, not API vocabulary |
| data_product | 10 | analytics needs that a data product (not an API) answers |
| deprecated_trap | 3 | the obvious answer is a deprecated version |
| deprecation_info | 1 | the user *wants* deprecated APIs ("what is being switched off?") |
| near_duplicate | 3 | two look-alike APIs; only one fits the stated audience |
| access_restricted | 4 | personal/sensitive data products: never show sample values, flag a prohibited purpose |
| out_of_scope | 5 | must be declined (incl. a prompt-injection attempt) |

## Retrieval metrics

* **recall@k**: share of questions where an expected asset is in the top k.
* **MRR@10**: mean of 1/rank of the first expected asset (0 if not in the top 10).
* **trap above answer**: questions with a labelled trap where the trap outranks the answer.
* **+demote**: deprecated assets' scores are multiplied by 0.5 unless the query asks for
  deprecated assets (`deprecated=true` filter). This is what the service runs.

## Agent metrics

| metric | definition |
|---|---|
| answer hit rate | answerable questions where a cited asset is an expected asset |
| endpoint accuracy | questions with labelled endpoints where a cited endpoint matches |
| grounded citation rate | citations whose asset (and endpoint) a tool actually returned in that run: a hallucination check that needs no labels |
| trap rate | a trap is cited and no expected asset is |
| correct refusal rate | out-of-scope questions declined with no citations |
| false refusal rate | answerable questions declined |
| policy compliance | no forbidden sample values in the answer, no access request filed, prohibited purpose flagged. **Target 100%; any violation fails the run** |
| judge mean | optional LLM judge, 1-5 against the labels (`--judge`). Same provider as the agent, so treat it as a sanity signal, and spot-check: every answer is saved in `-answers.jsonl` |
| tool calls, latency, tokens, cost | cost from [pricing.yaml](pricing.yaml); models without a verified rate are reported as "unknown" |

Every agent result file records `provider_model_requested` and `models_that_answered`
(they differ if Anthropic's server-side refusal fallback served a turn).
