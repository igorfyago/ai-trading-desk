"""Agent 3 — GEX Repo Interpreter (RAG over a real codebase).

Level: retrieval-augmented agent. Indexes the options-flow-analytics repo
(Rust collector, SQL schema, Node API, docs) into a local Chroma vector
store, then answers questions about HOW the system works — with file
citations — by retrieving the relevant source chunks on demand.

Retrieval is exposed to the agent AS A TOOL, so the model decides when and
what to search (and can search multiple times for multi-part questions),
instead of a fixed retrieve-then-answer chain.

Setup: set GEX_REPO_PATH in .env to a checkout of options-flow-analytics.
Run:   python agents/03_repo_interpreter/main.py --index          # (re)build the index
       python agents/03_repo_interpreter/main.py "How is the gamma flip level computed?"
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.tools import tool
from langchain_text_splitters import RecursiveCharacterTextSplitter
from rich.console import Console

from common.llm import get_embeddings, get_model

load_dotenv()
console = Console()

CHROMA_DIR = str(Path(__file__).resolve().parents[2] / "data" / "chroma")
COLLECTION = "gex-repo"
SOURCE_EXTS = {".rs", ".sql", ".js", ".ts", ".md", ".yml", ".yaml", ".toml"}
SKIP_DIRS = {"target", "node_modules", ".git", ".idea", "dist"}


def repo_path() -> Path:
    p = Path(os.getenv("GEX_REPO_PATH", "../options-flow-analytics")).expanduser()
    if not p.exists():
        sys.exit(f"GEX repo not found at '{p}'. Set GEX_REPO_PATH in .env")
    return p.resolve()


def load_repo_documents(root: Path) -> list[Document]:
    docs = []
    for path in root.rglob("*"):
        if path.is_dir() or path.suffix.lower() not in SOURCE_EXTS:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if text.strip():
            docs.append(Document(page_content=text, metadata={"file": str(path.relative_to(root))}))
    return docs


def build_index() -> None:
    root = repo_path()
    docs = load_repo_documents(root)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1200, chunk_overlap=150, separators=["\n\n", "\nfn ", "\n", " ", ""]
    )
    splits = splitter.split_documents(docs)
    console.print(f"Indexing {len(docs)} files → {len(splits)} chunks from {root} ...")
    Chroma.from_documents(
        splits, get_embeddings(), persist_directory=CHROMA_DIR, collection_name=COLLECTION
    )
    console.print("[green]Index built.[/green]")


def get_retriever():
    store = Chroma(
        persist_directory=CHROMA_DIR,
        embedding_function=get_embeddings(),
        collection_name=COLLECTION,
    )
    return store.as_retriever(search_type="mmr", search_kwargs={"k": 6, "fetch_k": 24})


@tool
def search_codebase(query: str) -> str:
    """Semantically search the options-flow-analytics source code and docs.

    Returns the most relevant code/doc chunks, each tagged with its file path.
    Call multiple times with different phrasings for multi-part questions.

    Args:
        query: What to look for, e.g. "gamma flip computation" or "wall detection"
    """
    docs = get_retriever().invoke(query)
    if not docs:
        return "No matches. Try different terminology."
    return "\n\n".join(f"--- {d.metadata['file']} ---\n{d.page_content}" for d in docs)


SYSTEM = """You are the maintainer of the options-flow-analytics codebase (a Rust
collector computing dealer GEX/DEX from option chains, PostgreSQL storage, and a
Node.js API + dashboard). Answer questions about how the system works.

Rules:
- ALWAYS ground answers in retrieved code: call search_codebase before answering,
  and again with new phrasings if the first results don't cover the question.
- Cite file paths for every claim, like (collector/src/gex.rs).
- Quote the key lines of code when explaining a computation.
- If the code genuinely doesn't answer the question, say so — never invent code."""


def build_agent(checkpointer=None):
    return create_agent(model=get_model(), tools=[search_codebase],
                        system_prompt=SYSTEM, checkpointer=checkpointer)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--index":
        build_index()
        sys.exit(0)
    question = " ".join(args) or "How is the gamma flip level computed, end to end?"
    agent = build_agent()
    result = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": 15, "run_name": "cli:repo-interpreter", "tags": ["cli", "repo"]},
    )
    console.print(result["messages"][-1].content)
