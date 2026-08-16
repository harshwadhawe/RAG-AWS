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

No test suite exists (`pytest` is in requirements.txt but there are no test files). `populate_database.py`'s `__main__` is a no-op `pass`, and the `query_data.py` script older docs mention was deleted — ingestion only happens through the web upload route.

`requirements.txt` is unpinned, so a fresh install resolves to langchain 1.x. The legacy `langchain.document_loaders` / `langchain.text_splitter` / `langchain.prompts` shims are gone there; imports now use `langchain_community`, `langchain_text_splitters`, and `langchain_core` directly. Keep new imports off the `langchain.*` top-level namespace.

## Architecture

Four files, one flow: PDF → chunks → Chroma → similarity search → LLM prompt.

- `app.py` — Flask app, all routes, and `query_rag()` (retrieval + prompt + generation).
- `populate_database.py` — `process_pdfs_and_populate_database(filepaths)` is the ingestion entry point, called synchronously from the `/upload` route. Also `clear_database()`.
- `get_embedding_function.py` — single place the embedding model is chosen. Bedrock is commented out; Ollama `llama3.2` is live.

Two different Ollama models: `nomic-embed-text` for embeddings (`get_embedding_function.py`) and `llama3.2` for generation (`OllamaLLM` in `app.py:query_rag`). They are not interchangeable — chat models have no embedding head, and asking Ollama to embed with `llama3.2` fails with `501 This server does not support embeddings`. Changing the embedding model invalidates the existing vector store (dimension mismatch); delete `chroma/` or hit `/reset_rag` afterward.

### Chunk IDs and de-duplication

`calculate_chunk_ids()` stamps each chunk with `{source_path}:{page}:{chunk_index}`. Ingestion reads all existing IDs from Chroma and only adds chunks whose ID isn't already there, so re-uploading the same PDF is a no-op. Because the ID embeds the full filepath, the same PDF uploaded under a different path re-ingests as duplicates.

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
- Uploads aren't extension-checked; `PyPDFLoader` will just throw on a non-PDF and 500 the request.
- `chroma/`, `uploads/`, and `testenv/` are gitignored.
