"""LLM-judge faithfulness spot check for knowledge-base answers. Report only, never a gate.

    python evals/run.py            # first: produces evals/results/latest.json
    python evals/judge.py          # judge the knowledge-base answers in it
    python evals/judge.py --provider anthropic   # use Claude as the judge (paid, stronger)

For each answered RAG/multi-turn case, a judge LLM splits the answer into claims and marks
each one as supported or not by the passages Jarvis was given. Why it is not the gate:
the deterministic checks in run.py are reproducible and free; a judge (especially a 7B
one) is noisy. It is a second opinion that finds things string checks can't, e.g. an
answer that contains the right number but adds an invented detail around it.

Passages come from latest.json when run.py recorded them, else they are retrieved again
from the `jarvis_eval` database (same retriever, so the same passages).
Writes evals/results/judge.md and judge.json.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "src"))
EVAL_DSN = os.environ.get("JARVIS_EVAL_ADMIN_DSN",
                          "postgresql://jarvis:jarvis@localhost:5433/jarvis").rsplit("/", 1)[0] + "/jarvis_eval"
CATEGORIES = {"rag", "multi-turn"}


class Claim(BaseModel):
    claim: str = Field(description="one factual statement from the answer")
    supported: bool = Field(description="true only if the passages state it")


class Verdict(BaseModel):
    claims: list[Claim] = Field(description="every factual claim in the answer")
    faithful: bool = Field(description="true if every claim is supported by the passages")


PROMPT = """You check whether an assistant's answer is faithful to the source passages.

Question: {q}

Passages the assistant was given:
{passages}

Answer: {answer}

List every factual claim in the answer (ignore citation markers like [1] and polite filler).
Mark a claim supported only if the passages state it. A refusal such as "I couldn't find
that" makes no factual claim: return no claims and faithful=true."""


def judge_model(provider: str | None, model: str | None):
    from jarvis.config import get_settings
    from jarvis.llm import get_chat_model

    s = get_settings().model_copy()
    if provider:
        s.llm_provider = provider
    if model:
        if s.llm_provider == "ollama":
            s.ollama_model = model
        else:
            s.anthropic_model = model
    name = s.ollama_model if s.llm_provider == "ollama" else s.anthropic_model
    return get_chat_model(s).with_structured_output(Verdict, method="json_schema"), name


def passages_for(result: dict) -> str:
    if result.get("context"):
        return result["context"]
    os.environ["JARVIS_DATABASE_URL"] = EVAL_DSN
    from jarvis.config import get_settings
    from jarvis.rag import format_hits, get_retriever

    get_settings.cache_clear()
    get_retriever.cache_clear()
    hits = get_retriever().search(result["q"])
    return format_hits(hits) if hits else "(no passages)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["ollama", "anthropic"], help="judge provider (default: from .env)")
    ap.add_argument("--model", help="judge model name (default: from .env)")
    args = ap.parse_args()

    data = json.loads((HERE / "results" / "latest.json").read_text())
    # Only answers written by the knowledge-base agent: an action reply ("I've added ...") is
    # backed by a tool result, not by passages, and would be flagged wrongly.
    cases = [r for r in data["results"]
             if r["category"] in CATEGORIES and r["reply"] and r["agents"][-1:] == ["assistant"]]
    judge, name = judge_model(args.provider, args.model)

    rows = []
    for r in cases:
        passages = passages_for(r)
        try:
            v = judge.invoke(PROMPT.format(q=r["q"], passages=passages, answer=r["reply"]))
            unsupported = [c.claim for c in v.claims if not c.supported]
            faithful = v.faithful and not unsupported
            row = {"id": r["id"], "faithful": faithful, "claims": len(v.claims), "unsupported": unsupported}
        except Exception as e:  # a judge failure is reported, never fatal
            row = {"id": r["id"], "faithful": None, "claims": 0, "unsupported": [], "error": repr(e)}
        rows.append({**row, "reply": r["reply"]})
        mark = {True: "OK  ", False: "FLAG", None: "ERR "}[row["faithful"]]
        print(f"{mark} {r['id']:<26} {'; '.join(row['unsupported']) or row.get('error', '')}", flush=True)

    judged = [x for x in rows if x["faithful"] is not None]
    ok = sum(x["faithful"] for x in judged)
    md = [f"# Faithfulness spot check (LLM judge: `{name}`)", "",
          f"{ok}/{len(judged)} answers judged faithful. Report only - not a gate; "
          "review each FLAG by hand before trusting it.", "",
          "| Case | Verdict | Claims | Unsupported claims |", "|---|---|---|---|",
          *[f"| {x['id']} | {'✅' if x['faithful'] else ('⚠️' if x['faithful'] is False else 'error')} | "
            f"{x['claims']} | {'; '.join(x['unsupported']).replace('|', '/') or x.get('error', '')} |"
            for x in rows]]
    (HERE / "results" / "judge.md").write_text("\n".join(md) + "\n")
    (HERE / "results" / "judge.json").write_text(json.dumps(rows, indent=2))
    print("\n" + "\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
