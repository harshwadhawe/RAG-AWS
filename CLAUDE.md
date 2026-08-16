# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

Ollama must be running in a separate terminal before the app starts — every embedding and every answer goes through it:

```bash
ollama pull nomic-embed-text            # terminal 1, once
ollama run llama3.2                     # terminal 1
uv venv                                 # terminal 2 (first time only)
uv pip install -r requirements.txt
uv run python app.py                    # Flask dev server, debug=True
```

Dependencies are managed with `uv` against `requirements.txt` (no pyproject.toml). `uv run` picks up `.venv` on its own — don't activate anything.

Ingestion happens through the web upload route; `populate_database.py`'s `__main__` is a no-op `pass`.

## Evals — run these before and after any retrieval change

```bash
uv run pytest test_rag.py -v      # needs ollama running; ~15s, real LLM calls
```

`test_rag.py` is a 6-case golden set: five factual questions whose answers were verified to exist in `data/monopoly.pdf` and `data/ticket_to_ride.pdf`, plus one out-of-corpus question that must be *declined* rather than answered. It exercises the whole pipeline through two seams — `process_pdfs_and_populate_database()` and `query_rag()` — so it survives backend swaps.

This is the regression gate for changing the embedding model, chunk size, `k`, the prompt, or the vector store. Record the pass count before the change and compare after. Answers are normalized (`$1,500` == `1500`), and multi-value expectations are alternatives, not conjunctions.

## Architecture

Three modules, one flow: PDF → chunks → embed → Chroma → nearest-neighbour search → LLM prompt.

- `app.py` — Flask app, all routes, and `query_rag()` (retrieval + prompt + generation).
- `populate_database.py` — owns the vector store: `get_collection()`, `process_pdfs_and_populate_database(filepaths)` (ingestion entry point, called synchronously from `/upload`), `search(query_embedding, k)`, `clear_database()`.
- `get_embedding_function.py` — owns embeddings: `embed_texts()` for ingestion, `embed_query()` (LRU-cached) for queries.

**No LangChain.** The pipeline calls `pypdf`, `chromadb`, and `ollama` directly; the only piece kept is `langchain-text-splitters` for `RecursiveCharacterTextSplitter`, which is the one component with real logic. It costs 2.9 MB (pulling `langchain-core`), measured as ~1% of site-packages — worth it. Do not reintroduce `langchain`, `langchain-community`, `langchain-chroma`, or `langchain-ollama`; the wrappers they provide are one-liners here and they were a live source of breakage across the 1.x migration.

Two different Ollama models: `nomic-embed-text` for embeddings and `llama3.2` for generation (`LLM_MODEL` in `app.py`). They are not interchangeable — chat models have no embedding head, and asking Ollama to embed with `llama3.2` fails with `501 This server does not support embeddings`. Changing the embedding model invalidates the existing vector store (dimension mismatch); delete `chroma/` or hit `/reset_rag` afterward, then re-run the evals.

### Chunk IDs and de-duplication

`chunk_documents()` stamps each chunk with `{source_path}:{page}:{chunk_index}` (page numbers 0-indexed). Ingestion queries Chroma for those exact IDs and only adds the ones missing, so re-uploading the same PDF is a no-op. Because the ID embeds the full filepath, the same PDF uploaded under a different path re-ingests as duplicates.

Pages with no extractable text are skipped at load time, so a scanned/image-only PDF silently contributes nothing — there is no OCR step.

### Chroma access

`get_collection()` is `lru_cache`d, so the `PersistentClient` is built once per process; `clear_database()` must call `get_collection.cache_clear()` before deleting the directory or the stale handle survives. The collection is named `docs` and is created with `embedding_function=None` — embeddings are always passed in explicitly, which stops chroma from downloading and running its own default ONNX model.

### State lives in the Flask session, not the database

The home page branches on `session['embeddings_created']`. A populated `chroma/` directory with a fresh session still renders the upload form — the app has no way to notice existing embeddings. Anything that changes ingestion or reset behavior must keep that session flag in sync (`session.modified = True` is required; the code sets it explicitly).

`/reset_rag` deletes `chroma/`, empties `uploads/`, and clears the session — the only path that removes data.

### Routes

`/` (GET question form, POST full-page answer) · `/upload` (GET form, POST ingest → redirect) · `/upload_page` (GET form only) · `/ask_question` (POST, JSON — this is what the home page's AJAX actually calls) · `/reset_rag` (POST).

`/` POST and `/ask_question` both call `query_rag`; the form-based path on `/` is effectively dead since `home.html` intercepts submit and fetches `/ask_question`.

Retrieval is `k=5` similarity search; the prompt (`PROMPT_TEMPLATE` in `app.py`) instructs answering *only* from context, so retrieval failures surface as refusals rather than hallucinations.

## Templates

`base.html` defines a global `showLoader()`; `home.html` shadows it with its own `showLoader`/`hideLoader` pair inside a `DOMContentLoaded` handler. CSRF tokens come from the `inject_csrf_token` context processor and must be included as a hidden input in every form and as the `X-CSRFToken` header on fetch calls.

## Known rough edges

- `app.secret_key = 'your_secret_key'` is hardcoded in `app.py` — sessions are forgeable as-is.
- `/upload` runs ingestion synchronously inside the request; large PDFs block the worker until embeddings finish.
- Uploads aren't extension-checked; `pypdf` will just throw on a non-PDF and 500 the request.
- `chroma/`, `uploads/`, and `.venv/` are gitignored.
- Installed size is ~305 MB, almost all of it chromadb's tree (onnxruntime 70 MB, chromadb_rust_bindings 49 MB, kubernetes 41 MB, grpc 39 MB). This is the thing to fix before any serverless deploy — not the application code.
