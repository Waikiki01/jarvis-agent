"""Run the golden eval set against the full Jarvis stack and write a report.

    python evals/run.py                 # all cases
    python evals/run.py --offline       # skip cases that need internet
    python evals/run.py --only rag      # one category (or a case id)

Uses a fresh `jarvis_eval` database (sample docs ingested, empty task list), the real
LLM from settings, the real MCP servers, and the same graph the API serves (runtime.py).
Deterministic checks only - no LLM grades another LLM, so scores are reproducible.
Exit code 1 if a threshold in golden.yaml is missed (usable as a gate).
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from contextlib import AsyncExitStack
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg
import yaml
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

HERE = Path(__file__).parent
ROOT = HERE.parent
ADMIN_DSN = os.environ.get("JARVIS_EVAL_ADMIN_DSN", "postgresql://jarvis:jarvis@localhost:5433/jarvis")
EVAL_DSN = ADMIN_DSN.rsplit("/", 1)[0] + "/jarvis_eval"
REFUSAL = re.compile(r"couldn'?t find|could not find|can'?t find|cannot find|not (?:in|found in|available in) "
                     r"(?:your|the|my)|no (?:information|record|mention)|don'?t have (?:that|this|any)|"
                     r"isn'?t (?:in|mentioned)|not mentioned|unable to find|(?:don'?t|do not) have access", re.I)
CITATION = re.compile(r"\[\d+\]")


def fresh_database() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS jarvis_eval WITH (FORCE)")
        c.execute("CREATE DATABASE jarvis_eval")


def fill(value, today: date):
    if isinstance(value, str):
        return value.replace("{tomorrow}", (today + timedelta(days=1)).isoformat())
    if isinstance(value, dict):
        return {k: fill(v, today) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, today) for v in value]
    return value


def check(case: dict, run: dict) -> list[str]:
    """Return the list of failed checks (empty = pass)."""
    fails, reply = [], run["reply"]
    low = reply.lower()
    if "agents" in case and run["agents"] != case["agents"]:
        fails.append(f"agents {run['agents']} != {case['agents']}")
    for s in case.get("contains", []):
        if s.lower() not in low:
            fails.append(f"missing {s!r}")
    if "contains_any" in case and not any(s.lower() in low for s in case["contains_any"]):
        fails.append(f"missing any of {case['contains_any']}")
    for s in case.get("not_contains", []):
        if s.lower() in low:
            fails.append(f"contains forbidden {s!r}")
    if case.get("cites") and not CITATION.search(reply):
        fails.append("no [n] citation")
    if case.get("refuses") and not REFUSAL.search(reply):
        fails.append("did not say it couldn't find it")
    want = case.get("approval")
    if want == "none" and run["approvals"]:
        fails.append(f"unexpected approval request {run['approvals']}")
    elif isinstance(want, dict):
        got = next((a for a in run["approvals"] if a["tool"] == want["tool"]), None)
        if got is None:
            fails.append(f"no approval request for {want['tool']} (got {run['approvals']})")
        else:
            for k, sub in (want.get("args") or {}).items():
                if str(sub).lower() not in str(got["args"].get(k, "")).lower():
                    fails.append(f"{want['tool']}.{k}={got['args'].get(k)!r}, expected ~{sub!r}")
    return fails


async def run_case(graph, case: dict, idx: int) -> dict:
    cfg = {"configurable": {"thread_id": f"eval-{idx}-{case['id']}"}}
    decision = {"approved": case.get("decide") == "approve"}
    approvals, t0 = [], time.perf_counter()
    try:
        out = await graph.ainvoke({"messages": [HumanMessage(case["q"])]}, cfg)
        for _ in range(5):  # answer each approval request
            if not out.get("__interrupt__"):
                break
            approvals += [i.value for i in out["__interrupt__"]]
            out = await graph.ainvoke(Command(resume=decision), cfg)
        msgs = out["messages"]
        start = max(i for i, m in enumerate(msgs) if isinstance(m, HumanMessage))
        turn = msgs[start + 1:]
        final = next((m for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), None)
        reply = final.content if final and isinstance(final.content, str) else ""
        tools = [f"{tc['name']}({json.dumps(tc['args'])[:80]})" for m in turn if isinstance(m, AIMessage)
                 for tc in m.tool_calls]
        error = None
    except Exception as e:
        out, reply, tools, error = {}, "", [], repr(e)
    return {"id": case["id"], "category": case["category"], "q": case["q"], "reply": reply,
            "agents": out.get("plan", []), "approvals": approvals, "tools": tools,
            "seconds": round(time.perf_counter() - t0, 2), "error": error}


def retrieval_metrics(cases: list[dict]) -> dict:
    """hit@5 and MRR of the right source for RAG cases (retriever only, no LLM)."""
    from jarvis.rag import get_retriever

    r, ranks = get_retriever(), []
    for c in cases:
        if "source" not in c:
            continue
        hits = r.store.hybrid_search(r.embedder.embed_query(c["q"]), c["q"], k=20, n=20)
        if r.rerank_model:
            from jarvis.rag.rerank import rerank
            hits = rerank(c["q"], hits, 5, r.rerank_model)
        pos = next((i for i, h in enumerate(hits[:5], 1) if h.source.endswith(c["source"])), None)
        ranks.append(pos)
    n = len(ranks) or 1
    return {"cases": len(ranks), "hit_at_5": round(sum(p is not None for p in ranks) / n, 3),
            "mrr": round(sum(1 / p for p in ranks if p) / n, 3)}


def report(results: list[dict], retrieval: dict, thresholds: dict, model: str) -> tuple[str, bool]:
    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    passed = sum(r["passed"] for r in results)
    rate = passed / len(results) if results else 0
    secs = sorted(r["seconds"] for r in results)
    p95 = secs[min(len(secs) - 1, int(0.95 * len(secs)))] if secs else 0
    ok = rate >= thresholds["pass_rate"] and retrieval["hit_at_5"] >= thresholds["retrieval_hit_at_5"]
    lines = [
        f"# Eval report - {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"Model: `{model}` · cases: {len(results)} · **pass rate {rate:.0%}** "
        f"(gate {thresholds['pass_rate']:.0%}) · {'✅ PASS' if ok else '❌ FAIL'}",
        "",
        f"Latency: median {statistics.median(secs) if secs else 0:.1f} s · p95 {p95:.1f} s  ",
        f"Retrieval: hit@5 {retrieval['hit_at_5']:.0%} (gate {thresholds['retrieval_hit_at_5']:.0%}) · "
        f"MRR {retrieval['mrr']:.2f} over {retrieval['cases']} questions",
        "",
        "| Category | Passed |", "|---|---|",
        *[f"| {c} | {sum(r['passed'] for r in rs)}/{len(rs)} |" for c, rs in by_cat.items()],
        "",
        "| Case | Result | Agents | Seconds | Failed checks |", "|---|---|---|---|---|",
        *[f"| {r['id']} | {'✅' if r['passed'] else '❌'} | {', '.join(r['agents'])} | {r['seconds']} | "
          f"{'; '.join(r['fails']).replace('|', '/') or ''} |" for r in results],
    ]
    return "\n".join(lines) + "\n", ok


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--only", help="category or case id")
    args = ap.parse_args()

    spec = yaml.safe_load((HERE / "golden.yaml").read_text())
    today = date.today()
    cases = [fill(c, today) for c in spec["cases"]
             if not (args.offline and c.get("network"))
             and (not args.only or args.only in (c["category"], c["id"]))]

    fresh_database()
    os.environ["JARVIS_DATABASE_URL"] = EVAL_DSN
    from jarvis.config import get_settings
    from jarvis.rag import get_retriever
    from jarvis.rag.embed import get_embedder
    from jarvis.rag.ingest import ingest_path
    from jarvis.rag.store import VectorStore
    from jarvis.runtime import build_runtime

    get_settings.cache_clear()
    get_retriever.cache_clear()
    settings = get_settings()
    store = VectorStore(EVAL_DSN, settings.embed_dim)
    store.ensure_schema()
    ingest_path(ROOT / "samples", store, get_embedder())

    model = settings.ollama_model if settings.llm_provider == "ollama" else settings.anthropic_model
    results = []
    async with AsyncExitStack() as stack:
        rt = await build_runtime(stack, settings)
        for i, case in enumerate(cases):
            r = await run_case(rt.graph, case, i)
            r["fails"] = ([f"error: {r['error']}"] if r["error"] else []) + check(case, r)
            r["passed"] = not r["fails"]
            results.append(r)
            print(f"{'PASS' if r['passed'] else 'FAIL'} {r['id']:<26} {r['seconds']:>6}s  {'; '.join(r['fails'])}",
                  flush=True)

    retrieval = retrieval_metrics(cases)
    md, ok = report(results, retrieval, spec["thresholds"], model)
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "latest.md").write_text(md)
    (out / "latest.json").write_text(json.dumps({"retrieval": retrieval, "results": results}, indent=2))
    print("\n" + md)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
