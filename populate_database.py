import os
from functools import lru_cache

import boto3
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from get_embedding_function import REGION, embed_texts

load_dotenv()

VECTOR_BUCKET = os.environ.get("VECTOR_BUCKET", "llama-rag-vectors")
VECTOR_INDEX = os.environ.get("VECTOR_INDEX", "docs")

# S3 Vectors caps a single PutVectors request; batch well under it.
PUT_BATCH = 100


@lru_cache(maxsize=1)
def _client():
    return boto3.client("s3vectors", region_name=REGION)


def _index_args():
    return {"vectorBucketName": VECTOR_BUCKET, "indexName": VECTOR_INDEX}


def clear_database():
    """Delete every vector in the index. The index itself is managed by Terraform."""
    client = _client()
    while True:
        page = client.list_vectors(**_index_args(), maxResults=500)
        keys = [v["key"] for v in page.get("vectors", [])]
        if not keys:
            return
        client.delete_vectors(**_index_args(), keys=keys)


def load_pages(filepath):
    """Yield (page_number, text) for each page with extractable text."""
    for page_number, page in enumerate(PdfReader(filepath).pages):
        text = page.extract_text()
        if text and text.strip():
            yield page_number, text


def chunk_documents(filepaths):
    """Split PDFs into chunks tagged with a stable `source:page:index` id."""
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
                    "id": f"{source}:{page_number}:{index}",
                    "text": chunk,
                    "metadata": {"source": source, "page": page_number},
                }


def _existing_keys(keys):
    """Which of these vector keys are already stored."""
    found = set()
    client = _client()
    for i in range(0, len(keys), PUT_BATCH):
        response = client.get_vectors(**_index_args(), keys=keys[i:i + PUT_BATCH])
        found.update(v["key"] for v in response.get("vectors", []))
    return found


def process_pdfs_and_populate_database(filepaths):
    chunks = list(chunk_documents(filepaths))
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


def list_sources():
    """Distinct source documents currently in the index, sorted.

    This is what makes the app stateless: "has the corpus been populated, and
    with what" is answered by the index itself rather than by a per-browser
    session flag or a local uploads directory, so any instance can serve any
    request.

    ponytail: full scan of vector metadata. Fine for hundreds of documents;
    if the index grows, keep a document manifest instead (a DynamoDB table or
    a single S3 object) and read that.
    """
    client, sources, start = _client(), set(), None
    while True:
        kwargs = {**_index_args(), "maxResults": 500, "returnMetadata": True}
        if start:
            kwargs["nextToken"] = start
        page = client.list_vectors(**kwargs)
        for vector in page.get("vectors", []):
            source = vector.get("metadata", {}).get("source")
            if source:
                sources.add(source)
        start = page.get("nextToken")
        if not start:
            return sorted(sources)


# S3 Vectors is an approximate index and routinely returns FEWER than topK --
# measured at roughly half on this corpus (topK=5 -> 2 results, topK=20 -> 10).
# AWS documents over-fetching then post-processing as the mitigation. Asking for
# k directly silently gives you k/2 chunks of context and quietly degrades
# answers, which is exactly the failure the golden set caught.
OVERFETCH = 4


def search(query_embedding, k=5):
    """Return [(text, key, distance)] for the k nearest chunks, best first."""
    response = _client().query_vectors(
        **_index_args(),
        queryVector={"float32": list(query_embedding)},
        topK=k * OVERFETCH,
        returnDistance=True,
        # Requires s3vectors:GetVectors in addition to QueryVectors.
        returnMetadata=True,
    )
    results = [
        (v["metadata"]["source_text"], v["key"], v.get("distance"))
        for v in response["vectors"]
    ]
    return sorted(results, key=lambda r: r[2] if r[2] is not None else 0)[:k]
