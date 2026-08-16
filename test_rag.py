"""Golden-set eval for the RAG pipeline.

Run: uv run pytest test_rag.py -v
Needs AWS credentials in .env (see infra/) and Bedrock model access granted.
Ingestion is idempotent, so the fixture below is safe to re-run.

Each case is a real LLM call, so the suite takes a couple of minutes.
"""

import re

import pytest

from app import query_rag
from get_embedding_function import embed_query
from populate_database import process_pdfs_and_populate_database, search

CORPUS = ["data/monopoly.pdf", "data/ticket_to_ride.pdf"]

# (question, substrings that must all appear in a correct answer)
# Every expected value was verified to exist in the source PDFs.
CASES = [
    ("How much money does each player start with in Monopoly? Answer with the number only.", ["1500"]),
    ("How much salary do you collect for passing GO in Monopoly? Answer with the number only.", ["200"]),
    ("How many points is the longest continuous path bonus worth in Ticket to Ride? Answer with the number only.", ["10"]),
    ("How many train cars of each color are included in Ticket to Ride? Answer with the number only.", ["45"]),
    ("Name one way to get out of jail in Monopoly.", ["doubles", "card", "pay", "$50", "50"]),
]

# The corpus says nothing about this. A grounded system should decline rather
# than invent an answer -- the prompt instructs answering only from context.
OUT_OF_SCOPE = "What is the capital city of Australia?"
DECLINE_MARKERS = [
    "does not", "doesn't", "no information", "not provide", "no mention",
    "not mention", "cannot", "can't", "unable", "not contain", "no context",
    "not specified", "not found", "not include", "unfortunately",
]


def normalize(text):
    """Lowercase and strip currency/thousands formatting so $1,500 == 1500."""
    return re.sub(r"[$,]", "", text or "").lower()


@pytest.fixture(scope="session", autouse=True)
def corpus():
    process_pdfs_and_populate_database(CORPUS)


@pytest.mark.parametrize("question,expected", CASES, ids=lambda v: v[:40] if isinstance(v, str) else "")
def test_retrieval_surfaces_fact(question, expected):
    """Retrieval only -- no LLM call.

    Splits the failure mode: if this passes and test_answer_contains_fact fails,
    the chunk was retrieved and the model failed to use it. If both fail, the
    problem is embeddings/chunking/k, not generation.
    """
    context = normalize(" ".join(text for text, _, _ in search(embed_query(question), k=5)))
    assert any(normalize(e) in context for e in expected), (
        f"none of {expected} in the top-5 retrieved chunks"
    )


@pytest.mark.parametrize("question,expected", CASES, ids=lambda v: v[:40] if isinstance(v, str) else "")
def test_answer_contains_fact(question, expected):
    answer, sources = query_rag(question)
    got = normalize(answer)
    assert sources, "retrieval returned no sources"
    # Multi-value expectations are alternatives (any one is a correct answer);
    # single-value expectations are required.
    assert any(normalize(e) in got for e in expected), (
        f"none of {expected} in answer: {answer!r}"
    )


def test_declines_when_answer_not_in_corpus():
    answer, _ = query_rag(OUT_OF_SCOPE)
    got = normalize(answer)
    assert "canberra" not in got, f"hallucinated an out-of-corpus fact: {answer!r}"
    assert any(m in got for m in DECLINE_MARKERS), (
        f"did not decline an unanswerable question: {answer!r}"
    )
