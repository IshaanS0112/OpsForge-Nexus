# Chaos drill records

Written by [`scripts/run_drills.py`](../../scripts/run_drills.py); one JSON file per drill,
with the experiment, the incidents it opened, the cascade tree and both RCA rankings.
Latencies are measured from injection on the control loop's own signals. A drill
passes when it is detected within 90 s and, if it targets a canary, rolled back
within 90 s. The last column is RCA's top cause with the drill withheld.

| Recorded (UTC) | Cluster | Fault | Service (target) | Verdict | Detected | Mechanisms | Rolled back | Recovered | Cascaded to | Blind RCA #1 |
|---|---|---|---|---|---|---|---|---|---|---|
| [2026-09-19 21:28](20260919T212828-latency.json) | kind | LATENCY_INJECTION +400 ms | ledger-svc (all) | PASS | 5.5 s | fast burn at 5.5s, signal at 15.7s, consensus at 15.7s | — | 66.6 s | checkout-api, payments-svc, notification-svc | Latency regression in ledger-svc |
| [2026-09-19 21:35](20260919T213531-errors.json) | kind | ERROR_BURST 50% | inventory-svc (all) | PASS | 13.2 s | signal at 13.2s, consensus at 13.2s | — | 69.2 s | notification-svc, checkout-api | Error-rate regression in inventory-svc |
| [2026-09-19 21:45](20260919T214511-pod-kill.json) | kind | POD_KILL 100% | payments-svc (all) | FAIL | — | — | — | 61.4 s | — | — |
| [2026-09-19 21:46](20260919T214645-canary.json) | kind | ERROR_BURST 50% | catalog-svc (canary) | PASS | 13.7 s | gate at 13.7s, signal at 34.3s | 13.7 s | 64.9 s | checkout-api, inventory-svc, notification-svc | Release v1.9.1789854311 (CANARY) failed its rollout gates |
| [2026-09-19 21:57](20260919T215726-pod-kill.json) | kind | POD_KILL 100% | payments-svc (all) | PASS | 11.5 s | fast burn at 11.5s, signal at 16.5s, consensus at 16.5s | — | 62.5 s | notification-svc, checkout-api | Latency regression in payments-svc |
