# MaraBrain live HTTP stress test

Crystalline 0.33.0, `python -m marabrain` over streamable HTTP (stateless, bearer auth), managed engines, 2026-11-18T23:20:55+0700, Darwin arm64. Each load worker is an independent fastmcp client; overload, hostile, and abort phases use raw HTTP so status codes stay visible.

## Startup and seeding (default limits)

- launch_to_health_s: `1.85`
- cold_activation_ws-a_s: `0.31`
- cold_activation_ws-b_s: `0.24`
- cold_activation_ws-c_s: `0.27`
- memories_per_workspace: `58`
- embedding_wait_s: `12.2`

## Protocol overhead: tools/list and health

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| workers_1 | health | 195 | 0 | 24.3 | 43.8 | 56.7 | 92.9 | 59.7 |  |
| workers_1 | tools/list | 205 | 0 | 6.3 | 11.0 | 13.6 | 23.7 | 59.7 |  |
| workers_1 | *totals* | | | | | | | | wall_s=6.7, ok=400, errors=0, ok_per_s=59.7 |
| workers_16 | health | 185 | 0 | 245.5 | 313.0 | 343.7 | 346.9 | 101.0 |  |
| workers_16 | tools/list | 215 | 0 | 75.7 | 128.5 | 145.6 | 153.6 | 101.0 |  |
| workers_16 | *totals* | | | | | | | | wall_s=3.96, ok=400, errors=0, ok_per_s=101.0 |
| workers_64 | health | 203 | 0 | 1005.8 | 1197.0 | 1272.8 | 1273.8 | 93.9 |  |
| workers_64 | tools/list | 197 | 0 | 298.8 | 588.3 | 608.8 | 608.8 | 93.9 |  |
| workers_64 | *totals* | | | | | | | | wall_s=4.26, ok=400, errors=0, ok_per_s=93.9 |

## Concurrent writes (linked creates, then read-back)

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| workers_1 | create_memory | 60 | 0 | 25.0 | 29.7 | 103.9 | 103.9 | 37.4 |  |
| workers_1 | *totals* | | | | | | | | wall_s=1.6, ok=60, errors=0, ok_per_s=37.4, receipts=60, unique_permalinks=60, readable_after=60 |
| workers_8 | create_memory | 240 | 0 | 123.6 | 171.1 | 237.4 | 259.8 | 63.6 |  |
| workers_8 | *totals* | | | | | | | | wall_s=3.77, ok=240, errors=0, ok_per_s=63.6, receipts=240, unique_permalinks=240, readable_after=240 |
| workers_32 | create_memory | 480 | 0 | 535.9 | 843.5 | 890.7 | 1452.0 | 61.7 |  |
| workers_32 | *totals* | | | | | | | | wall_s=7.78, ok=480, errors=0, ok_per_s=61.7, receipts=480, unique_permalinks=480, readable_after=480 |

## Mixed agent workload on two workspaces

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| workers_8 | build_memory_context | 204 | 0 | 78.5 | 160.4 | 195.6 | 205.3 | 89.7 |  |
| workers_8 | create_memory | 159 | 0 | 81.6 | 164.1 | 216.9 | 228.1 | 89.7 |  |
| workers_8 | edit_memory | 92 | 0 | 75.3 | 135.8 | 162.9 | 162.9 | 89.7 |  |
| workers_8 | read_memory | 375 | 0 | 72.9 | 148.6 | 179.0 | 205.6 | 89.7 |  |
| workers_8 | review_memories | 41 | 0 | 82.6 | 140.0 | 192.8 | 192.8 | 89.7 |  |
| workers_8 | search_memories | 929 | 0 | 83.5 | 163.4 | 201.7 | 290.5 | 89.7 |  |
| workers_8 | *totals* | | | | | | | | wall_s=20.07, ok=1800, errors=0, ok_per_s=89.7, server_cpu_cores=1.09, daemons_cpu_cores=0.7, server_rss_peak_mb=134.5, daemons_rss_peak_mb=261.9, daemons_peak=3, transient_cli_peak=0 |
| workers_32 | build_memory_context | 222 | 0 | 292.5 | 1055.0 | 1679.9 | 1681.3 | 88.0 |  |
| workers_32 | create_memory | 176 | 0 | 309.9 | 1234.6 | 1637.5 | 1665.6 | 88.0 |  |
| workers_32 | edit_memory | 84 | 0 | 256.5 | 1111.8 | 1694.8 | 1694.8 | 88.0 |  |
| workers_32 | read_memory | 362 | 0 | 269.6 | 905.2 | 1460.4 | 1691.2 | 88.0 |  |
| workers_32 | review_memories | 62 | 0 | 312.4 | 965.2 | 1679.9 | 1679.9 | 88.0 |  |
| workers_32 | search_memories | 883 | 0 | 281.0 | 1041.2 | 1541.9 | 2779.1 | 88.0 |  |
| workers_32 | *totals* | | | | | | | | wall_s=20.34, ok=1789, errors=0, ok_per_s=88.0, server_cpu_cores=1.1, daemons_cpu_cores=0.7, server_rss_peak_mb=135.1, daemons_rss_peak_mb=285.5, daemons_peak=3, transient_cli_peak=0 |
| workers_64 | build_memory_context | 202 | 0 | 635.9 | 2024.1 | 2775.4 | 4620.9 | 83.6 |  |
| workers_64 | create_memory | 142 | 0 | 687.4 | 1691.8 | 2134.1 | 2326.9 | 83.6 |  |
| workers_64 | edit_memory | 71 | 0 | 478.3 | 1657.8 | 3815.4 | 3815.4 | 83.6 |  |
| workers_64 | read_memory | 366 | 0 | 616.6 | 2058.8 | 3778.3 | 5427.5 | 83.6 |  |
| workers_64 | review_memories | 53 | 0 | 500.0 | 2371.0 | 3953.1 | 3953.1 | 83.6 |  |
| workers_64 | search_memories | 894 | 0 | 623.8 | 2356.9 | 4663.8 | 6180.9 | 83.6 |  |
| workers_64 | *totals* | | | | | | | | wall_s=20.67, ok=1728, errors=0, ok_per_s=83.6, server_cpu_cores=1.1, daemons_cpu_cores=0.67, server_rss_peak_mb=140.0, daemons_rss_peak_mb=294.0, daemons_peak=3, transient_cli_peak=0 |

## Write contention


## Write contention: concurrent_appends

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| concurrent_appends | edit_memory | 48 | 0 | 394.7 | 763.1 | 816.3 | 816.3 | 32.5 |  |
| concurrent_appends | *totals* | | | | | | | | wall_s=1.48, ok=48, errors=0, ok_per_s=32.5 |
- markers_present_after: `48`
- markers_written: `48`

## Write contention: same_title_creates

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| same_title_creates | create_memory | 1 | 23 | 92.7 | 92.7 | 92.7 | 92.7 | 1.9 | Crystalline command failed with exit code 1. x23 |
| same_title_creates | *totals* | | | | | | | | wall_s=0.53, ok=1, errors=23, ok_per_s=1.9 |
- same_title_distinct_permalinks: `1`

## Large payloads

- 64KiB: `{"create_ok": true, "create_ms": 154.1, "read_ok": true, "read_bytes": 198372, "errors": []}`
- 1024KiB: `{"create_ok": true, "create_ms": 389.0, "read_ok": true, "read_bytes": 3181184, "errors": []}`
- 4096KiB: `{"create_ok": false, "create_ms": 96.2, "read_ok": false, "read_bytes": 0, "errors": ["MCPError: Server returned an error response"]}`

## Connection overload (limit_concurrency=100)

- burst_300_concurrent_connections: `{"requests": 300, "outcomes": {"200 tool_ok": 70, "503": 230}, "wall_s": 2.28, "ok_p50_ms": 1713.2, "ok_p95_ms": 2222.9}`

## Connection overload (limit_concurrency=100): after

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| after | search_memories | 20 | 0 | 520.9 | 714.8 | 714.8 | 714.8 | 1.8 |  |
| after | *totals* | | | | | | | | wall_s=11.23, ok=20, errors=0, ok_per_s=1.8 |
- server_alive: `true`

## Hostile traffic during load


## Hostile traffic during load: good_traffic_16_workers

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| good_traffic_16_workers | build_memory_context | 70 | 0 | 281.1 | 649.5 | 1095.8 | 1095.8 | 38.0 |  |
| good_traffic_16_workers | create_memory | 56 | 0 | 235.2 | 678.9 | 798.5 | 798.5 | 38.0 |  |
| good_traffic_16_workers | edit_memory | 30 | 0 | 255.5 | 539.5 | 847.3 | 847.3 | 38.0 |  |
| good_traffic_16_workers | read_memory | 124 | 0 | 250.3 | 751.6 | 1108.4 | 1246.6 | 38.0 |  |
| good_traffic_16_workers | review_memories | 6 | 0 | 363.6 | 767.7 | 767.7 | 767.7 | 38.0 |  |
| good_traffic_16_workers | search_memories | 313 | 0 | 438.9 | 1002.6 | 1165.3 | 1498.1 | 38.0 |  |
| good_traffic_16_workers | *totals* | | | | | | | | wall_s=15.77, ok=599, errors=0, ok_per_s=38.0 |
- wrong_token_x2000: `{"outcomes": {"401": 2000}, "wall_s": 5.48}`
- wrong_host_x200: `{"outcomes": {"421": 200}, "wall_s": 0.64}`

## Client aborts

- requests_aborted_by_client_timeout_50ms: `{"ReadTimeout": 292, "503": 28}`
- transient_cli_right_after: `0`
- drain_s: `0.11`

## Client aborts: health_after

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| health_after | health | 5 | 0 | 39.3 | 45.6 | 45.6 | 45.6 | 24.0 |  |
| health_after | *totals* | | | | | | | | wall_s=0.21, ok=5, errors=0, ok_per_s=24.0 |

## Soak

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 16 workers | build_memory_context | 1129 | 0 | 129.5 | 479.4 | 618.3 | 1003.2 | 76.3 |  |
| 16 workers | create_memory | 878 | 0 | 144.1 | 491.1 | 750.7 | 970.9 | 76.3 |  |
| 16 workers | edit_memory | 489 | 0 | 136.1 | 481.5 | 673.5 | 756.9 | 76.3 |  |
| 16 workers | read_memory | 1818 | 0 | 124.2 | 459.0 | 634.1 | 1042.9 | 76.3 |  |
| 16 workers | review_memories | 270 | 0 | 149.7 | 510.9 | 721.7 | 1057.8 | 76.3 |  |
| 16 workers | search_memories | 4588 | 0 | 169.5 | 599.8 | 843.3 | 1837.2 | 76.3 |  |
| 16 workers | *totals* | | | | | | | | wall_s=120.17, ok=9172, errors=0, ok_per_s=76.3, server_rss_start_mb=147.3, server_rss_end_mb=133.2, open_files_start=76, open_files_end=76, server_cpu_cores=1.03, daemons_cpu_cores=0.81, server_rss_peak_mb=147.5, daemons_rss_peak_mb=486.5, daemons_peak=3, transient_cli_peak=14 |

| Window s | OK/s | Errors | p50 ms | p95 ms |
| --- | --- | --- | --- | --- |
| 0-15 | 51.4 | 0 | 226.5 | 728.5 |
| 15-30 | 52.9 | 0 | 235.3 | 702.3 |
| 30-45 | 53.1 | 0 | 244.5 | 647.6 |
| 45-60 | 79.1 | 0 | 134.2 | 556.1 |
| 60-75 | 97.7 | 0 | 103.8 | 434.5 |
| 75-90 | 96.1 | 0 | 111.0 | 440.8 |
| 90-105 | 93.1 | 0 | 114.9 | 446.2 |
| 105-120 | 87.1 | 0 | 118.0 | 474.3 |
| 120-135 | 1.1 | 0 | 215.5 | 695.9 |

## SIGTERM under load

- exited_within_grace: `true`
- exit_s: `0.46`
- exit_code: `0`
- descendants_before: `6`
- descendants_surviving_10s: `0`
- client_outcomes: `{"ok": 265, "errors": 154}`

## Restart with capacity limits

- launch_to_health_s: `1.35`
- confirmed_before_restart: `2212`
- readable_after_restart: `2212`
- errors: `{"read_memory": {"ok": 2212, "errors": 0, "error_samples": {}, "p50_ms": 29.2, "p95_ms": 94.5, "p99_ms": 157.0, "max_ms": 317.9}}`

## Capacity-limited overload: 64 workers on three workspaces

| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 64 workers | build_memory_context | 191 | 110 | 712.5 | 3325.6 | 4885.2 | 5000.1 | 71.8 | Resident workspace capacity is busy. x110 |
| 64 workers | create_memory | 109 | 105 | 663.1 | 2840.2 | 3048.1 | 3297.9 | 71.8 | Resident workspace capacity is busy. x105 |
| 64 workers | edit_memory | 68 | 0 | 551.1 | 2442.4 | 3469.8 | 3469.8 | 71.8 |  |
| 64 workers | read_memory | 315 | 167 | 573.5 | 2559.4 | 4229.0 | 6752.1 | 71.8 | Resident workspace capacity is busy. x167 |
| 64 workers | review_memories | 46 | 24 | 641.4 | 2575.2 | 2800.3 | 2800.3 | 71.8 | Resident workspace capacity is busy. x24 |
| 64 workers | search_memories | 753 | 381 | 624.9 | 2779.5 | 3812.4 | 6774.9 | 71.8 | Resident workspace capacity is busy. x381 |
| 64 workers | *totals* | | | | | | | | wall_s=20.63, ok=1482, errors=787, ok_per_s=71.8, refusals=787, refusal_p95_ms=23.0, server_cpu_cores=1.11, daemons_cpu_cores=0.66, server_rss_peak_mb=156.4, daemons_rss_peak_mb=668.8, daemons_peak=2, transient_cli_peak=0 |

## SIGTERM idle

- exited_within_grace: `true`
- exit_s: `0.36`
- exit_code: `0`
- descendants_before: `2`
- descendants_surviving_10s: `0`

## Server logs

- first_run.log: `{"tracebacks": 0, "error_lines": 26}`
- second_run.log: `{"tracebacks": 0, "error_lines": 0}`
