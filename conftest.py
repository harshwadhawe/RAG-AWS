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
