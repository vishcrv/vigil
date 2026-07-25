"""FastAPI entrypoint. Run from `backend/`: uvicorn main:app --reload

Startup fails fast if the configured provider's API key is missing (spec.md's
Environment Variables section: better a clear error here than on first query).
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent.providers import check_api_key
from api.routes.agent import router as agent_router
from db import get_connection, init_db

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_api_key(os.getenv("LLM_PROVIDER", "anthropic"))
    with get_connection() as conn:
        init_db(conn)
    yield


app = FastAPI(title="vigil", lifespan=lifespan)

# Local Vite dev server only; nothing is deployed (spec.md: Deploy = none).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent_router, prefix="/api/v1")

# ponytail: the enriched Parquet is NOT loaded here. The ML tools own their own
# lru_cached DuckDB connection (ml/data.py), so a startup load would just be a
# second copy. Revisit only if per-query Parquet open time measurably hurts.
