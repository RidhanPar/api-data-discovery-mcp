# ADR 0002: Hybrid search (BM25 + vectors, fused with RRF) with deprecation demotion

**Status:** accepted, with a measured caveat

## Context

Discovery questions mix business language ("pay a repair shop for a motor claim") with
exact identifiers (`CPR`, `AvtaleGiro`, `claimId`). The catalog contains deliberate traps:
deprecated versions that read like the right answer and look-alike APIs for different
audiences. We wanted the ranking decision to be made on numbers, not taste.

## Decision

* Two retrievers over the same chunks: BM25 computed in SQL from `tsvector` term
  frequencies (real BM25, not `ts_rank`), and cosine similarity over pgvector HNSW with a
  local, checksum-pinned embedding model (`bge-small-en-v1.5`, ONNX int8).
* Fuse with reciprocal rank fusion (k = 60): no score calibration between retrievers.
* Demote deprecated assets (score × 0.5) unless the query asks for deprecated assets.

## Evidence

`make eval-retrieval` on 40 labelled questions with an expected asset
(`eval/results/retrieval.json`, local ONNX embeddings):

| configuration | recall@1 | recall@3 | MRR@10 | trap ranked above answer |
|---|---|---|---|---|
| lexical (BM25) | 0.70 | 0.825 | 0.79 | 6/9 |
| vector | 0.80 | 0.95 | 0.883 | 2/9 |
| hybrid | 0.775 | 0.95 | 0.87 | 5/9 |
| vector + demote | 0.80 | 0.95 | 0.873 | 1/9 |
| **hybrid + demote (shipped)** | **0.825** | 0.925 | **0.883** | **1/9** |

What the numbers say, without spin:

* Vector search alone is as good as hybrid on recall@3 on this question set; BM25 alone
  is clearly worse. Hybrid's measured advantage is narrow: it fixes all three
  near-duplicate questions (vector alone: 2/3) and has the best recall@1 once
  deprecated assets are demoted.
* Fusion re-introduces deprecated traps that vector search had ranked lower (5/9 vs 2/9),
  because BM25 matches the deprecated spec's vocabulary. Demotion is what fixes this
  (1/9), and it costs the one question that *wants* deprecated APIs (q33), which the
  `list_deprecations` tool and the `deprecated=true` filter serve instead.
* The differences are one or two questions out of 40. They are not statistically
  significant; the set is small.

We keep hybrid because identifier-style queries (product codes, field names, national
ID schemes) are where BM25 is known to help, and the question set under-represents them.
That is an assumption this set does not test; a larger, identifier-heavy set is the
first follow-up. If it does not show a gain, vector + demote is simpler and faster
(p50 about 20 ms vs 36 ms here) and should replace hybrid.

## Consequences

+ Ranking changes are regression-tested in CI on a fixed subset (`make eval-retrieval-ci`).
- Two indexes to maintain; latency roughly doubles versus vector-only.
