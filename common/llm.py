"""Shared model factory: every agent gets its model the same way.

Change DESK_MODEL in .env to swap providers (e.g. "anthropic:claude-sonnet-4-5")
without touching agent code — all agents go through init_chat_model.
"""

import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv()

DEFAULT_MODEL = "openai:gpt-4.1"


def get_model(temperature: float = 0.0):
    """Chat model used by all text agents. Configured via DESK_MODEL env var."""
    return init_chat_model(os.getenv("DESK_MODEL", DEFAULT_MODEL), temperature=temperature)


def get_embeddings():
    """Embedding model for the RAG agent."""
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(model="text-embedding-3-small")
