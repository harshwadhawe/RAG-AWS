"""Print the resolved config at the top of every eval run.

Eval numbers are meaningless without knowing which model produced them, and a
stale exported env var silently outranks .env (python-dotenv does not override).
Surfacing the effective values turns that into a one-glance check.
"""


def pytest_report_header(config):
    import os

    from app import LLM_MODEL
    from get_embedding_function import EMBED_DIMENSION, EMBED_MODEL, REGION
    from populate_database import VECTOR_BUCKET, VECTOR_INDEX

    lines = [
        f"region: {REGION}   index: {VECTOR_BUCKET}/{VECTOR_INDEX}",
        f"embed:  {EMBED_MODEL} ({EMBED_DIMENSION}d)",
        f"llm:    {LLM_MODEL}",
    ]
    # A shell export beats the .env file; say so rather than letting it confuse.
    shadowed = [k for k in ("LLM_MODEL", "EMBED_MODEL", "AWS_ACCESS_KEY_ID") if k in os.environ
                and _from_dotenv(k) not in (None, os.environ[k])]
    if shadowed:
        lines.append(f"WARNING: shell env overrides .env for: {', '.join(shadowed)}")
    return lines


def _from_dotenv(key, path=".env"):
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        return None
    return None


# --- Behaviour-test fixtures -------------------------------------------------
# An in-memory stand-in for S3 Vectors and Bedrock so route behaviour can be
# tested in milliseconds, offline, with no AWS spend. These catch contract
# breaks (a changed signature, a route that forgets to scope by session);
# test_rag.py still exercises the real services for retrieval quality.

import pytest


class FakeStore:
    """Mimics the session-scoped vector store."""

    def __init__(self):
        self.vectors = {}   # key -> (session_id, source)
        self.files = {}     # session_id -> [filename]

    def list_sources(self, session_id):
        return sorted({s for sid, s in self.vectors.values() if sid == session_id})

    def search(self, embedding, session_id, k=5):
        hits = [(f"chunk from {src}", key, 0.1)
                for key, (sid, src) in self.vectors.items() if sid == session_id]
        return hits[:k]

    def ingest(self, filepaths, session_id):
        import os
        for path in filepaths:
            name = os.path.basename(path)
            self.vectors[f"{session_id}:{name}:0:0"] = (session_id, name)
            self.files.setdefault(session_id, []).append(name)

    def clear_database(self, session_id):
        doomed = [k for k, (sid, _) in self.vectors.items() if sid == session_id]
        for k in doomed:
            del self.vectors[k]
        return len(doomed)

    def clear_uploads(self, session_id=None):
        removed = len(self.files.pop(session_id, []))
        return removed


@pytest.fixture
def store(monkeypatch):
    """Swap the storage and model layers for fakes, in the app's namespace.

    Patched on `app` rather than `populate_database` because the routes bind the
    names at import time -- patching the source module would not affect them.
    """
    import app as web

    fake = FakeStore()
    monkeypatch.setattr(web, "list_sources", fake.list_sources)
    monkeypatch.setattr(web, "search", fake.search)
    monkeypatch.setattr(web, "process_pdfs_and_populate_database", fake.ingest)
    monkeypatch.setattr(web, "clear_database", fake.clear_database)
    monkeypatch.setattr(web, "clear_uploads", fake.clear_uploads)
    monkeypatch.setattr(web, "embed_query", lambda text: (0.0,) * 8)

    class FakeBedrock:
        def converse_stream(self, **kwargs):
            # Shaped like the real event stream: deltas first, usage only in the
            # trailing metadata event -- which is why the app cannot report
            # tokens or cost until generation finishes.
            return {"stream": [
                {"contentBlockDelta": {"delta": {"text": "a grounded "}}},
                {"contentBlockDelta": {"delta": {"text": "answer"}}},
                {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
            ]}

    monkeypatch.setattr(web, "bedrock", FakeBedrock())
    return fake


@pytest.fixture
def client(store):
    import app as web
    web.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web.app.test_client() as c:
        yield c
