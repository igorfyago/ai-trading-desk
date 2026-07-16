# ai-trading-desk web app: all six agents + voice behind one FastAPI process.
FROM python:3.12-slim

WORKDIR /app

# Layer-cache dependencies separately from source
COPY pyproject.toml ./
COPY common ./common
RUN pip install --no-cache-dir -e ".[web,postgres]"

COPY agents ./agents
COPY web ./web
COPY evals ./evals

# Seed the demo DB at build time so the container is self-sufficient;
# set DATABASE_URL at runtime to use the live options-flow Postgres instead.
RUN python -m common.db

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import httpx; httpx.get('http://localhost:8000/agents', timeout=2)"

CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8000"]
