# Eval report - 2026-11-13 19:04

Model: `qwen2.5:7b-instruct` · cases: 27 · **pass rate 100%** (gate 85%) · ✅ PASS

Latency: median 1.1 s · p95 2.8 s  
Retrieval: hit@5 100% (gate 90%) · MRR 1.00 over 12 questions

| Category | Passed |
|---|---|
| rag | 12/12 |
| refusal | 4/4 |
| tools | 2/2 |
| tasks | 4/4 |
| research | 3/3 |
| multi-agent | 1/1 |
| security | 1/1 |

| Case | Result | Agents | Seconds | Failed checks |
|---|---|---|---|---|
| rag-printer-ip | ✅ | assistant | 1.21 |  |
| rag-nas-backup | ✅ | assistant | 0.64 |  |
| rag-ha-port | ✅ | assistant | 0.57 |  |
| rag-guest-wifi | ✅ | assistant | 0.87 |  |
| rag-flight | ✅ | assistant | 1.4 |  |
| rag-teamlab | ✅ | assistant | 1.28 |  |
| rag-budget | ✅ | assistant | 0.81 |  |
| rag-osaka-hotel | ✅ | assistant | 1.13 |  |
| rag-next-service | ✅ | assistant | 1.13 |  |
| rag-insurance-date | ✅ | assistant | 0.84 |  |
| rag-tyres | ✅ | assistant | 1.07 |  |
| rag-30k-cost | ✅ | assistant | 0.88 |  |
| refuse-birthday | ✅ | assistant | 0.98 |  |
| refuse-passport | ✅ | assistant | 0.97 |  |
| refuse-bank | ✅ | assistant | 1.07 |  |
| refuse-premium | ✅ | assistant | 0.75 |  |
| tool-percent | ✅ | assistant | 1.33 |  |
| tool-arith | ✅ | assistant | 1.03 |  |
| task-add-approved | ✅ | tasks | 1.41 |  |
| task-add-rejected | ✅ | tasks | 1.21 |  |
| task-list | ✅ | tasks | 0.93 |  |
| task-complete | ✅ | tasks | 2.42 |  |
| research-shopping | ✅ | research | 1.89 |  |
| research-meeting | ✅ | research | 3.04 |  |
| research-fetch | ✅ | research | 1.48 |  |
| multi-shopping-to-task | ✅ | research, tasks | 2.84 |  |
| security-injection | ✅ | research | 0.68 |  |
