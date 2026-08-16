import os
from functools import lru_cache

import boto3
from dotenv import load_dotenv
from langsmith import traceable
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from get_embedding_function import REGION, embed_texts

load_dotenv()

# No fallback for the bucket: a wrong-but-plausible default sends reads and
# writes to an index that may not exist, or worse, to someone else's. Terraform
# writes the real value into .env (local) and the Lambda environment (deployed).
VECTOR_BUCKET = os.environ.get("VECTOR_BUCKET") or ""
VECTOR_INDEX = os.environ.get("VECTOR_INDEX", "docs")


def _require_bucket():
    if not VECTOR_BUCKET:
        raise RuntimeError(
            "VECTOR_BUCKET is not set — the app has not been pointed at any "
            "infrastructure yet. Run ./deploy/deploy.sh (which writes .env), or "
            "set VECTOR_BUCKET in the environment."
        )

# S3 Vectors caps a single PutVectors request; batch well under it.
PUT_BATCH = 100

# Explicit opt-out of session scoping. Only the scheduled cleanup and a full
# index wipe should use it; the name is meant to be conspicuous in review.
ALL_SESSIONS = None


@lru_cache(maxsize=1)
def _client():
    _require_bucket()
    return boto3.client("s3vectors", region_name=REGION)


def _index_args():
    return {"vectorBucketName": VECTOR_BUCKET, "indexName": VECTOR_INDEX}


def delete_keys(keys, batch=200):
    """Delete vectors by key. Returns the number removed."""
    client = _client()
    keys = list(keys)
    for i in range(0, len(keys), batch):
        client.delete_vectors(**_index_args(), keys=keys[i:i + batch])
    return len(keys)


def clear_database(session_id, max_rounds=200):
    """Delete this session's vectors, or every vector when session_id is None.

    Returns the number of vectors deleted. `session_id` is required; pass
    ALL_SESSIONS to wipe the whole index.

    Bounded rather than `while True`: list-after-delete is eventually consistent,
    so a key that was just removed can still come back in the next listing. An
    unbounded loop would spin on that until the Lambda timeout.
    """
    deleted = 0
    for _ in range(max_rounds):
        keys = [key for _, key in _scan(session_id)]
        if not keys:
            return deleted
        deleted += delete_keys(keys)
    raise RuntimeError(
        f"index still returning vectors after {max_rounds} delete rounds "
        f"({deleted} deleted); aborting rather than looping"
    )


def load_pages(filepath):
    """Yield (page_number, text) for each page with extractable text."""
    for page_number, page in enumerate(PdfReader(filepath).pages):
        text = page.extract_text()
        if text and text.strip():
            yield page_number, text


def chunk_documents(filepaths, session_id):
    """Split PDFs into chunks keyed `session:source:page:index`.

    The session prefix keeps one visitor's documents out of another's results on
    a public endpoint, and makes "delete everything this session uploaded" a
    prefix operation rather than a search.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=80,
        length_function=len,
    )
    for filepath in filepaths:
        # The document's *name* is the identity, not where it happened to be
        # staged. Uploads land in a temp dir that differs per request and per
        # Lambda instance; keying on the full path would make every re-upload a
        # fresh set of duplicates instead of a no-op.
        source = os.path.basename(filepath)
        for page_number, text in load_pages(filepath):
            for index, chunk in enumerate(splitter.split_text(text)):
                yield {
                    "id": f"{session_id}:{source}:{page_number}:{index}",
                    "text": chunk,
                    # session_id is *filterable* metadata. Only source_text was
                    # declared non-filterable at index creation, and filterable
                    # keys need no declaration -- so this needs no index rebuild.
                    "metadata": {
                        "source": source,
                        "page": page_number,
                        "session_id": session_id,
                    },
                }


def _existing_keys(keys):
    """Which of these vector keys are already stored."""
    found = set()
    client = _client()
    for i in range(0, len(keys), PUT_BATCH):
        response = client.get_vectors(**_index_args(), keys=keys[i:i + PUT_BATCH])
        found.update(v["key"] for v in response.get("vectors", []))
    return found


def process_pdfs_and_populate_database(filepaths, session_id):
    chunks = list(chunk_documents(filepaths, session_id))
    if not chunks:
        print("No extractable text found.")
        return

    existing = _existing_keys([c["id"] for c in chunks])
    new = [c for c in chunks if c["id"] not in existing]
    if not new:
        print("No new chunks to add.")
        return

    print(f"Adding {len(new)} new chunks to the index.")
    embeddings = embed_texts([c["text"] for c in new])
    vectors = [
        {
            "key": chunk["id"],
            "data": {"float32": embedding},
            # source_text is declared non-filterable on the index: filterable
            # metadata is capped at 2 KB/vector and we never filter on body text.
            "metadata": {"source_text": chunk["text"], **chunk["metadata"]},
        }
        for chunk, embedding in zip(new, embeddings)
    ]
    client = _client()
    for i in range(0, len(vectors), PUT_BATCH):
        client.put_vectors(**_index_args(), vectors=vectors[i:i + PUT_BATCH])


def list_sources(session_id):
    """Distinct source documents in the index, sorted. Scoped to one session.

    This is what makes the app stateless: "has the corpus been populated, and
    with what" is answered by the index itself rather than by a per-browser
    session flag or a local uploads directory, so any instance can serve any
    request.

    ponytail: full scan of vector metadata. Fine for hundreds of documents;
    if the index grows, keep a document manifest instead (a DynamoDB table or
    a single S3 object) and read that.
    """
    return sorted({source for source, _ in _scan(session_id)})


def _scan(session_id):
    """Yield (source, key) for stored vectors, optionally one session's.

    ListVectors has no server-side filter -- only QueryVectors does -- so the
    session match happens client-side. This is why the manifest is the scaling
    ceiling noted above.
    """
    client, start = _client(), None
    while True:
        kwargs = {**_index_args(), "maxResults": 500, "returnMetadata": True}
        if start:
            kwargs["nextToken"] = start
        page = client.list_vectors(**kwargs)
        for vector in page.get("vectors", []):
            meta = vector.get("metadata", {})
            if session_id and meta.get("session_id") != session_id:
                continue
            if meta.get("source"):
                yield meta["source"], vector["key"]
        start = page.get("nextToken")
        if not start:
            return


# S3 Vectors is an approximate index and routinely returns FEWER than topK --
# measured at roughly half on this corpus (topK=5 -> 2 results, topK=20 -> 10).
# AWS documents over-fetching then post-processing as the mitigation. Asking for
# k directly silently gives you k/2 chunks of context and quietly degrades
# answers, which is exactly the failure the golden set caught.
OVERFETCH = 4


@traceable(run_type="retriever", name="s3_vectors_search")
def search(query_embedding, session_id, k=5):
    """Return [(text, key, distance)] for the k nearest chunks, best first.

    `session_id` is REQUIRED and positional. Pass ALL_SESSIONS to search across
    every visitor -- deliberately ugly, because that is the unsafe case. A
    default of None would mean a forgotten keyword silently answers one
    visitor's question from another visitor's documents, which is precisely the
    failure this scoping exists to prevent. Now it is a TypeError instead.
    """
    filters = {"session_id": session_id} if session_id else None
    response = _client().query_vectors(
        **_index_args(),
        queryVector={"float32": list(query_embedding)},
        topK=k * OVERFETCH,
        returnDistance=True,
        # Requires s3vectors:GetVectors in addition to QueryVectors.
        returnMetadata=True,
        **({"filter": filters} if filters else {}),
    )
    results = [
        (v["metadata"]["source_text"], v["key"], v.get("distance"))
        for v in response["vectors"]
    ]
    return sorted(results, key=lambda r: r[2] if r[2] is not None else 0)[:k]
