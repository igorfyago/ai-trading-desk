"""Agent evals: run a dataset through an agent, grade every answer,
publish the experiment to LangSmith.

    python evals/run_evals.py all          # or: brief | sql | repo

Grading philosophy, in order of preference:
1. EXECUTE ground truth (sql): run the reference SQL on the same
   deterministic DB and demand the numbers appear in the answer.
2. CHECK structure (brief, repo): typed fields match; required files cited.
3. LLM-AS-JUDGE (repo): only where mechanical checks can't reach —
   semantic correctness vs a reference answer, with a structured verdict.

Each run appears in LangSmith → Datasets & Experiments, with per-example
scores, latencies, token counts, and full traces. Re-run after any prompt
change and diff the experiments side by side: that's the regression story.
"""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from langsmith import evaluate
from pydantic import BaseModel, Field

from common.db import get_connection
from common.llm import get_model

load_dotenv()


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _numbers(text: str) -> set[str]:
    """Normalized numeric tokens in a string ('1,036' -> '1036', '22.70' -> '22.7')."""
    out = set()
    for tok in re.findall(r"-?\d[\d,]*\.?\d*", text.replace(",", "")):
        try:
            f = float(tok)
        except ValueError:
            continue
        out.add(f"{f:g}")
    return out


# ------------------------------------------------------------------ brief ----

def eval_brief():
    a1 = _load("a1", "agents/01_market_brief/main.py")

    def target(inputs: dict) -> dict:
        r = a1.run(inputs["question"])
        return {"tickers": r.tickers, "intent": r.intent, "answer": r.answer,
                "confidence": r.confidence}

    def tickers_exact(outputs: dict, reference_outputs: dict) -> dict:
        got = {t.upper() for t in outputs["tickers"]}
        want = set(reference_outputs["tickers"])
        return {"key": "tickers_exact", "score": float(got == want)}

    def intent_match(outputs: dict, reference_outputs: dict) -> dict:
        return {"key": "intent_match",
                "score": float(outputs["intent"] == reference_outputs["intent"])}

    def answer_substantive(outputs: dict) -> dict:
        return {"key": "answer_substantive", "score": float(len(outputs["answer"]) > 150)}

    return evaluate(target, data="desk-brief",
                    evaluators=[tickers_exact, intent_match, answer_substantive],
                    experiment_prefix="brief", max_concurrency=2)


# -------------------------------------------------------------------- sql ----

def eval_sql():
    a2 = _load("a2", "agents/02_text_to_sql/main.py")
    agent = a2.build_agent()

    def target(inputs: dict) -> dict:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": inputs["question"]}]},
            config={"recursion_limit": 25, "run_name": "eval:sql"})
        n_tool_calls = sum(len(getattr(m, "tool_calls", []) or []) for m in result["messages"])
        return {"answer": result["messages"][-1].content, "tool_calls": n_tool_calls}

    def ground_truth_in_answer(outputs: dict, reference_outputs: dict) -> dict:
        conn = get_connection()
        rows = conn.execute(reference_outputs["reference_sql"]).fetchall()
        conn.close()
        answer_numbers = _numbers(outputs["answer"])
        answer_upper = outputs["answer"].upper()
        ok = True
        for row in rows:
            for value in row:
                if isinstance(value, (int, float)):
                    ok &= _numbers(str(value)) <= answer_numbers
                else:
                    ok &= str(value).upper() in answer_upper
        return {"key": "ground_truth_in_answer", "score": float(ok)}

    def shows_verifiable_sql(outputs: dict) -> dict:
        return {"key": "shows_verifiable_sql", "score": float("select" in outputs["answer"].lower())}

    def efficient_tool_use(outputs: dict) -> dict:
        return {"key": "efficient_tool_use", "score": float(outputs["tool_calls"] <= 5)}

    return evaluate(target, data="desk-sql",
                    evaluators=[ground_truth_in_answer, shows_verifiable_sql, efficient_tool_use],
                    experiment_prefix="sql", max_concurrency=2)


# ------------------------------------------------------------------- repo ----

class JudgeVerdict(BaseModel):
    correct: bool = Field(description="Does the answer state the same mechanism as the reference?")
    reasoning: str


def eval_repo():
    a3 = _load("a3", "agents/03_repo_interpreter/main.py")
    agent = a3.build_agent()
    judge = get_model().with_structured_output(JudgeVerdict)

    def target(inputs: dict) -> dict:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": inputs["question"]}]},
            config={"recursion_limit": 15, "run_name": "eval:repo"})
        return {"answer": result["messages"][-1].content}

    def cites_required_files(outputs: dict, reference_outputs: dict) -> dict:
        answer = outputs["answer"].lower()
        hits = sum(1 for f in reference_outputs["must_cite"] if f.lower() in answer)
        return {"key": "cites_required_files",
                "score": hits / len(reference_outputs["must_cite"])}

    def judged_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
        verdict = judge.invoke([
            {"role": "system", "content":
                "You are grading a codebase-QA answer against a reference written by the "
                "repo author. Grade ONLY whether the mechanism described matches the "
                "reference — extra correct detail is fine, contradiction or invention is not."},
            {"role": "user", "content":
                f"Question: {inputs['question']}\n\nReference: {reference_outputs['reference']}"
                f"\n\nAnswer to grade:\n{outputs['answer']}"},
        ])
        return {"key": "judged_correct", "score": float(verdict.correct),
                "comment": verdict.reasoning}

    return evaluate(target, data="desk-repo",
                    evaluators=[cites_required_files, judged_correct],
                    experiment_prefix="repo", max_concurrency=2)


SUITES = {"brief": eval_brief, "sql": eval_sql, "repo": eval_repo}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    from evals.datasets import upload
    upload()
    for name, fn in SUITES.items():
        if which in (name, "all"):
            print(f"\n=== eval: {name} ===")
            fn()
