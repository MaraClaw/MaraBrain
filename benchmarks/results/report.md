# MaraBrain graph evaluation

Crystalline 0.33.0, MaraBrain over MCP stdio (managed mode), 2026-11-18T23:19:56+0700, on Darwin arm64.
Corpus write time 6.6 s; embedding wait 0.1 s (141/141 chunks embedded); search modes {'hybrid': 324}.

## E1 multi-hop recall (budget: at most 5 memories)
Search top-1 is the service the question names in 0.65 of questions.

| Strategy | Recall | Mean tool calls | Mean memories retrieved |
| --- | --- | --- | --- |
| search_top5 | 0.412 | 1.0 | 5.0 |
| search_top1_context | 0.7 | 2.0 | 3.7 |
| search_top1_read_follow | 0.7 | 3.3 | 2.3 |

| Strategy/question | Recall |
| --- | --- |
| search_top1_context/oncall | 0.675 |
| search_top1_context/version | 0.725 |
| search_top1_read_follow/oncall | 0.675 |
| search_top1_read_follow/version | 0.725 |
| search_top5/oncall | 0.35 |
| search_top5/version | 0.475 |

## E2 currentness

| Arm | Top-1 current | Stale in top 3 |
| --- | --- | --- |
| no_lifecycle | 0.6 | 1.0 |
| superseded_ranking_only | 1.0 | 0.0 |
| superseded_status_stable | 1.0 | 0.0 |

## E3 integrity review

| Metric | Value |
| --- | --- |
| injected_each | 10 |
| dangling_link_recall_V102 | 1.0 |
| retired_reference_recall_V101 | 1.0 |
| clean_false_positives_V101_V102 | 0 |
| findings_total | 30 |
| flagged_memories_by_rule | {'V101': 10, 'V102': 10, 'V301': 8} |

## S1 concurrent store

| Workers | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | Wall s | OK/s | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | create_memory | 48 | 0 | 26.7 | 28.7 | 30.5 | 1.29 | 37.19 | unresolved links after: 0 |
| 4 | create_memory | 48 | 0 | 53.1 | 84.2 | 93.8 | 0.69 | 69.55 | unresolved links after: 0 |
| 8 | create_memory | 48 | 0 | 117.2 | 146.1 | 208.2 | 0.75 | 63.66 | unresolved links after: 0 |

## S2 mixed read load

| Workers | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | Wall s | OK/s | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 4 | build_memory_context | 50 | 0 | 46.4 | 68.6 | 76.3 | 1.81 | 82.93 |  |
| 4 | read_memory | 50 | 0 | 44.8 | 62.3 | 71.4 | 1.81 | 82.93 |  |
| 4 | search_memories | 50 | 0 | 56.1 | 82.0 | 92.1 | 1.81 | 82.93 |  |
| 8 | build_memory_context | 50 | 0 | 86.7 | 138.5 | 164.1 | 1.52 | 98.76 |  |
| 8 | read_memory | 50 | 0 | 76.0 | 122.0 | 134.8 | 1.52 | 98.76 |  |
| 8 | search_memories | 50 | 0 | 81.5 | 132.8 | 148.6 | 1.52 | 98.76 |  |
| 16 | build_memory_context | 50 | 0 | 208.5 | 315.7 | 336.0 | 1.86 | 80.48 |  |
| 16 | read_memory | 50 | 0 | 207.6 | 424.5 | 426.1 | 1.86 | 80.48 |  |
| 16 | search_memories | 50 | 0 | 214.1 | 372.2 | 412.4 | 1.86 | 80.48 |  |
