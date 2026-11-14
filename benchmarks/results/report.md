# MaraBrain graph evaluation

Crystalline 0.32.2, MaraBrain over MCP stdio (managed mode), 2026-11-14T23:13:46+0700, on Darwin arm64.
Corpus write time 3.0 s; embedding wait 0.0 s (141/141 chunks embedded); search modes {'hybrid': 324}.

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
| 1 | create_memory | 48 | 0 | 15.1 | 16.2 | 16.5 | 0.73 | 65.6 | unresolved links after: 0 |
| 4 | create_memory | 48 | 0 | 19.1 | 41.3 | 52.3 | 0.3 | 160.26 | unresolved links after: 0 |
| 8 | create_memory | 48 | 0 | 29.7 | 96.3 | 105.4 | 0.3 | 162.65 | unresolved links after: 0 |

## S2 mixed read load

| Workers | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | Wall s | OK/s | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 4 | build_memory_context | 50 | 0 | 24.6 | 45.2 | 50.0 | 0.94 | 158.99 |  |
| 4 | read_memory | 50 | 0 | 16.7 | 37.3 | 58.1 | 0.94 | 158.99 |  |
| 4 | search_memories | 50 | 0 | 28.6 | 54.8 | 62.4 | 0.94 | 158.99 |  |
| 8 | build_memory_context | 50 | 0 | 31.1 | 91.0 | 100.2 | 0.8 | 186.47 |  |
| 8 | read_memory | 50 | 0 | 28.4 | 108.0 | 150.6 | 0.8 | 186.47 |  |
| 8 | search_memories | 50 | 0 | 44.0 | 108.4 | 128.5 | 0.8 | 186.47 |  |
| 16 | build_memory_context | 50 | 0 | 55.9 | 218.9 | 245.8 | 0.82 | 182.99 |  |
| 16 | read_memory | 50 | 0 | 69.7 | 207.1 | 219.0 | 0.82 | 182.99 |  |
| 16 | search_memories | 50 | 0 | 35.0 | 202.3 | 239.3 | 0.82 | 182.99 |  |
