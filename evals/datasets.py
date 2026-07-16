"""Eval datasets for the desk's agents, uploaded to LangSmith.

Each dataset is a set of (input, reference) pairs. References are chosen so
correctness can be checked MECHANICALLY where possible:
  - brief:  expected structured fields (tickers, intent)
  - sql:    a reference SQL query — the evaluator executes it against the
            same deterministic demo DB and checks the agent's answer
            contains the ground-truth numbers
  - repo:   files the answer must cite + a short reference answer for an
            LLM judge

Run `python evals/datasets.py` to (re)create them in LangSmith.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

BRIEF = [
    {"inputs": {"question": "Is SPY pinned by dealers into Friday opex?"},
     "outputs": {"tickers": ["SPY"], "intent": "positioning"}},
    {"inputs": {"question": "Should I sell iron condors on QQQ this week given the gamma regime?"},
     "outputs": {"tickers": ["QQQ"], "intent": "positioning"}},
    {"inputs": {"question": "What does a negative gamma flip crossing mean for IWM risk today?"},
     "outputs": {"tickers": ["IWM"], "intent": "risk"}},
    {"inputs": {"question": "Explain what charm flow does to SPX delta hedging into expiry"},
     "outputs": {"tickers": ["SPX"], "intent": "education"}},
    {"inputs": {"question": "Price a 1-week ATM straddle on SPY given 20 vol"},
     "outputs": {"tickers": ["SPY"], "intent": "pricing"}},
]

SQL = [
    {"inputs": {"question": "How many snapshots do we have in total?"},
     "outputs": {"reference_sql": "SELECT COUNT(*) FROM snapshots"}},
    {"inputs": {"question": "Which ticker had the most negative-gamma snapshots?"},
     "outputs": {"reference_sql":
                 "SELECT ticker FROM snapshots WHERE regime='negative_gamma' "
                 "GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 1"}},
    {"inputs": {"question": "What is the average VIX across all SPY snapshots? One decimal is fine."},
     "outputs": {"reference_sql": "SELECT ROUND(AVG(vix),1) FROM snapshots WHERE ticker='SPY'"}},
    {"inputs": {"question": "What's the single strongest put wall ever recorded, and on which ticker?"},
     "outputs": {"reference_sql":
                 "SELECT s.ticker, w.strike FROM walls w JOIN snapshots s ON s.id=w.snapshot_id "
                 "WHERE w.kind='put' ORDER BY w.strength DESC LIMIT 1"}},
    {"inputs": {"question": "How many snapshots had a red traffic light for QQQ?"},
     "outputs": {"reference_sql":
                 "SELECT COUNT(*) FROM snapshots WHERE ticker='QQQ' AND traffic_light='red'"}},
    {"inputs": {"question": "What was the highest spot price we ever recorded for IWM?"},
     "outputs": {"reference_sql": "SELECT MAX(spot) FROM snapshots WHERE ticker='IWM'"}},
]

REPO = [
    {"inputs": {"question": "How is the gamma flip level computed?"},
     "outputs": {"must_cite": ["analytics.rs"],
                 "reference": "The flip is found by scanning strikes from the lowest upward, "
                              "accumulating net GEX per strike; where the cumulative sum crosses "
                              "zero, the level is linearly interpolated between the two strikes "
                              "(find_flip in collector/src/analytics.rs)."}},
    {"inputs": {"question": "Where does the market data come from and how does the synthetic provider fit in?"},
     "outputs": {"must_cite": ["provider", "collector"],
                 "reference": "A MarketDataProvider trait abstracts the feed; the default CBOE "
                              "provider fetches free delayed chains, and a synthetic provider "
                              "generates offline data. PROVIDER env var selects between them."}},
    {"inputs": {"question": "How do snapshots get from the collector into the dashboard?"},
     "outputs": {"must_cite": ["schema.sql", "api"],
                 "reference": "The Rust collector writes timestamped rows into the Postgres "
                              "gex_dex_snapshots table (schema.sql); the Node/Express API reads "
                              "them with SQL and serves JSON to the web dashboard."}},
    {"inputs": {"question": "What retention/pruning mechanism exists for old snapshots?"},
     "outputs": {"must_cite": ["schema.sql"],
                 "reference": "Snapshots older than RETENTION_DAYS are deleted; schema.sql "
                              "documents the DELETE ... WHERE timestamp < NOW() - INTERVAL "
                              "pattern driven by the retention setting."}},
]

DATASETS = {
    "desk-brief": (BRIEF, "Structured extraction: tickers + intent from trader questions"),
    "desk-sql": (SQL, "Text-to-SQL over the deterministic demo DB, graded by executing reference SQL"),
    "desk-repo": (REPO, "RAG over options-flow-analytics, graded on citations + LLM judge"),
}


def upload() -> None:
    client = Client()
    for name, (examples, description) in DATASETS.items():
        if client.has_dataset(dataset_name=name):
            ds = client.read_dataset(dataset_name=name)
            existing = list(client.list_examples(dataset_id=ds.id))
            if len(existing) == len(examples):
                print(f"{name}: exists with {len(existing)} examples, skipping")
                continue
            client.delete_dataset(dataset_id=ds.id)
        ds = client.create_dataset(dataset_name=name, description=description)
        client.create_examples(dataset_id=ds.id, examples=examples)
        print(f"{name}: created with {len(examples)} examples")


if __name__ == "__main__":
    upload()
