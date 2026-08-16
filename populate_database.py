import os
import shutil
from functools import lru_cache

import chromadb
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from get_embedding_function import embed_texts

CHROMA_PATH = "chroma"
COLLECTION = "docs"


@lru_cache(maxsize=1)
def get_collection():
    os.makedirs(CHROMA_PATH, exist_ok=True)
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    # embedding_function=None: we always pass embeddings in explicitly, which
    # stops chroma from downloading and running its own default model.
    return client.get_or_create_collection(COLLECTION, embedding_function=None)


def clear_database():
    get_collection.cache_clear()
    if os.path.exists(CHROMA_PATH):
        shutil.rmtree(CHROMA_PATH)
    os.makedirs(CHROMA_PATH, exist_ok=True)


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
        for page_number, text in load_pages(filepath):
            for index, chunk in enumerate(splitter.split_text(text)):
                yield {
                    "id": f"{filepath}:{page_number}:{index}",
                    "text": chunk,
                    "metadata": {"source": filepath, "page": page_number},
                }


def process_pdfs_and_populate_database(filepaths):
    collection = get_collection()
    chunks = list(chunk_documents(filepaths))
    if not chunks:
        print("No extractable text found.")
        return

    existing = set(collection.get(ids=[c["id"] for c in chunks], include=[])["ids"])
    new = [c for c in chunks if c["id"] not in existing]
    if not new:
        print("No new chunks to add.")
        return

    print(f"Adding {len(new)} new chunks to the database.")
    collection.add(
        ids=[c["id"] for c in new],
        documents=[c["text"] for c in new],
        embeddings=embed_texts([c["text"] for c in new]),
        metadatas=[c["metadata"] for c in new],
    )


def search(query_embedding, k=5):
    """Return [(text, id, distance)] for the k nearest chunks."""
    result = get_collection().query(
        query_embeddings=[list(query_embedding)],
        n_results=k,
        include=["documents", "distances"],
    )
    return list(zip(result["documents"][0], result["ids"][0], result["distances"][0]))
