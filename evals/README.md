# Agent evals

Tests check the *code*; evals check the *agents' judgment* — repeatedly, against graded datasets, with every experiment published to LangSmith (Datasets & Experiments) for side-by-side comparison after any prompt or model change.

```bash
python evals/run_evals.py all     # or: brief | sql | repo
```

## Grading philosophy

1. **Execute ground truth when possible.** The text-to-SQL agent is graded by *running the reference SQL* against the same deterministic demo DB and requiring the true numbers in the agent's answer. No judge, no vibes.
2. **Check structure mechanically.** The brief agent's extracted tickers/intent are exact-matched; the RAG agent must cite the required files.
3. **LLM-as-judge only where mechanics can't reach** — semantic correctness of RAG answers vs a reference written by the repo author, with a structured verdict and reasoning stored as feedback.

Also measured: tool-loop efficiency (≤5 tool calls for SQL) and answer substance.

## Baseline results (gpt-4.1, 2026-07-16)

| Suite | Evaluator | Score |
|---|---|---|
| brief (5 ex) | tickers_exact | **1.00** |
| | intent_match | 0.80 |
| | answer_substantive | 1.00 |
| sql (6 ex) | ground_truth_in_answer | **1.00** |
| | shows_verifiable_sql | 1.00 |
| | efficient_tool_use | 1.00 |
| repo (4 ex) | judged_correct | **1.00** |
| | cites_required_files | 0.62 |

The 0.62 citation score is the suite doing its job: the RAG agent's answers are judged semantically correct but don't always name the source files explicitly — a prompt iteration target, now measurable.
