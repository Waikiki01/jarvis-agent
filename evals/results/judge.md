# Faithfulness spot check (LLM judge: `qwen2.5:7b-instruct`)

14/15 answers judged faithful. Report only - not a gate; review each FLAG by hand before trusting it.

| Case | Verdict | Claims | Unsupported claims |
|---|---|---|---|
| rag-printer-ip | ✅ | 1 |  |
| rag-nas-backup | ✅ | 1 |  |
| rag-ha-port | ✅ | 1 |  |
| rag-guest-wifi | ✅ | 1 |  |
| rag-flight | ✅ | 6 |  |
| rag-teamlab | ✅ | 2 |  |
| rag-budget | ✅ | 1 |  |
| rag-osaka-hotel | ✅ | 1 |  |
| rag-next-service | ✅ | 3 |  |
| rag-insurance-date | ⚠️ | 3 | The car is a Honda City 2021 |
| rag-tyres | ✅ | 3 |  |
| rag-30k-cost | ✅ | 1 |  |
| conv-1-printer | ✅ | 1 |  |
| conv-2-service | ✅ | 3 |  |
| conv-3-birthday | ✅ | 0 |  |
