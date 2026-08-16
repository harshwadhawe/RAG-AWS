# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

Provision AWS first — the app has no local fallback for embeddings, generation, or storage:

```bash
cd infra && terraform apply && cd .. && ./deploy/publish.sh
uv venv                                 # first time only
uv pip install -r requirements.txt
uv run python app.py                    # Flask dev server, debug=True
```

Dependencies are managed with `uv` against `requirements.txt` (no pyproject.toml). `uv run` picks up `.venv` on its own — don't activate anything.

The app serves at **http://127.0.0.1:5000** locally, and is also deployed behind a Lambda Function URL (link in the README, written by `deploy/publish.sh`).

There is no local model server and no local vector store; embeddings, generation, and storage are all AWS calls. Installed size is **62 MB / 52 packages** (down from 305 MB / 116 before chromadb, langchain, and the Anthropic SDK came out) — that budget is what makes a Lambda deploy viable, so weigh new dependencies against it.

Ingestion happens through the web upload route; `populate_database.py`'s `__main__` is a no-op `pass`.

## AWS infrastructure (`infra/`)

Terraform, not CDK — CDK's S3 Vectors support is L1 (`CfnVectorBucket`/`CfnIndex`, raw CloudFormation) so its ergonomic advantage doesn't apply here, and `cdk bootstrap` leaves a permanent CDKToolkit stack behind that survives teardown.

```bash
cd infra
terraform init
terraform apply                             # email comes from terraform.tfvars (gitignored)
cd .. && ./deploy/publish.sh                # .env, README link, CI variables
terraform destroy                           # full teardown, no leftovers
```

⚠️ **Never `source .env` in your shell** — it causes two distinct failures, both silent.

`python-dotenv` does **not** override variables already present in the environment, so anything exported from a previous `source` shadows the regenerated file indefinitely:

| Exported var | Symptom |
|---|---|
| `AWS_PROFILE` | Terraform runs as the least-privilege *dev* role instead of your admin profile → `AccessDenied` on `iam:GetUser`, `s3vectors:ListTagsForResource`, `budgets:ViewBudget` |
| `LLM_MODEL` | `terraform output > .env` appears to work, but the app keeps calling the *old* model — the file changed, the exported value didn't |

The app never needs the shell sourced; `python-dotenv` loads `.env` in-process. To recover a polluted shell:

```bash
unset LLM_MODEL AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
# or, per command:
env -u AWS_PROFILE terraform apply
```

`conftest.py` prints the resolved region, index, embedding model, and LLM in the pytest header, and warns when a shell export is shadowing `.env` — check it before trusting any eval result.

Two identities by design: your **admin** profile provisions infrastructure; the least-privilege **`llama-rag` profile** (assuming the dev role Terraform creates) runs the application. Neither uses a static access key — `.env` holds configuration only.

**IAM is eventually consistent.** Running the evals immediately after an apply that changed the policy can produce a one-off `AccessDenied` on an ARN the policy demonstrably grants. Give it ~30s and re-run before debugging. To tell a transient apart from a real misconfiguration, read the live policy rather than the Terraform source — a real gap shows up here, a transient does not:

```bash
aws iam get-role-policy --role-name llama-rag-lambda --policy-name llama-rag-lambda
```

Do not add retry-on-AccessDenied to the app; it would mask genuine permission bugs for a condition that self-resolves in seconds.

Requires aws provider **>= 6.0** (`aws_s3vectors_*` resources; validated against 6.60.0).

- `force_destroy = true` on the vector bucket is load-bearing — without it `terraform destroy` fails while the index still holds vectors. It must be applied *before* the destroy is attempted.
- Every `aws_s3vectors_index` argument forces replacement. Changing `dimension`, `distance_metric`, or `non_filterable_metadata_keys` silently destroys and rebuilds the index; all vectors are lost and must be re-ingested.
- `terraform.tfstate` no longer contains any static credential (the IAM user and access key were replaced by assumable roles), but it still describes the whole deployment. `infra/.gitignore` covers it; use an encrypted backend if you ever move it remote.
- **Bedrock model access cannot be provisioned by Terraform.** It's a console action (Bedrock → Model access). Meta and Amazon Titan need no request, which is why the current stack works from a bare `terraform apply`. Anthropic models require a one-time use-case form and return `403 ... is not available for this account` until granted — verified on this account with both app *and* admin credentials, so it is account-level, never IAM.

## Evals — run these before and after any retrieval change

```bash
uv run pytest test_rag.py -v                # full suite, 11 tests, ~6s
uv run pytest test_rag.py -k retrieval      # retrieval only: no LLM calls, no generation cost
```

Current state: **11/11 passing** (5 retrieval + 5 answer + 1 refusal).

Five factual questions whose answers were verified to exist in `data/monopoly.pdf` and `data/ticket_to_ride.pdf`, plus one out-of-corpus question that must be *declined* rather than answered. Each factual case runs twice:

- `test_retrieval_surfaces_fact` — asserts the fact appears in the top-5 retrieved chunks. No LLM call.
- `test_answer_contains_fact` — asserts the fact appears in the generated answer.

That split is the diagnosis: retrieval green + answer red means the chunk was found and the model failed to use it; both red means embeddings/chunking/`k`, not generation.

This is the regression gate for changing the embedding model, chunk size, `k`, the prompt, or the vector store. Record the pass count before the change and compare after. Answers are normalized (`$1,500` == `1500`), and multi-value expectations are alternatives, not conjunctions.

## Architecture

Three modules, one flow: PDF → chunks → Titan embeddings → S3 Vectors → nearest-neighbour search → Llama 4 Scout on Bedrock.

| File | Owns |
|---|---|
| `app.py` | Flask routes + `query_rag()` (retrieval → prompt → generation) |
| `populate_database.py` | The vector store: `process_pdfs_and_populate_database()`, `search()`, `clear_database()`, `chunk_documents()` |
| `get_embedding_function.py` | Embeddings and the shared `REGION`: `embed_texts()` (thread-pooled — Titan takes one input per call), `embed_query()` (LRU-cached) |
| `test_rag.py` | The golden set |
| `conftest.py` | Prints resolved config in the pytest header; warns on `.env` shadowing |
| `infra/` | Terraform: `main.tf`, `variables.tf`, `outputs.tf`, gitignored `terraform.tfvars` |

All AWS config comes from `.env`, written by `deploy/publish.sh` from Terraform outputs. It holds no credentials — those come from the `AWS_PROFILE` role assumption. `VECTOR_BUCKET` has no fallback and raises if unset, rather than silently pointing at the wrong index.

**No LangChain, one AWS SDK.** The pipeline calls `pypdf` and `boto3` directly — boto3 alone covers embeddings, vector storage, and generation. The only third-party piece kept is `langchain-text-splitters` for `RecursiveCharacterTextSplitter`, the one component with real logic. Do not reintroduce `langchain`, `langchain-community`, `langchain-chroma`, or `langchain-ollama` — the wrappers are one-liners here and were a live source of breakage across the 1.x migration. The `anthropic` SDK is also gone; Converse reaches Anthropic models too, when access allows.

Two models, both on `bedrock-runtime`:
- **Embeddings**: `amazon.titan-embed-text-v2:0` via `invoke_model`. Emits 1024 dims and **must match the index dimension exactly** — the index dimension is immutable.
- **Generation**: `us.meta.llama4-scout-17b-instruct-v1:0` via the **Converse** API, at `temperature=0`.

Converse is provider-agnostic — the same call shape serves Meta, Amazon, Mistral, and Anthropic — so changing `LLM_MODEL` in `.env` is the only edit needed to swap models. `invoke_model` would mean hand-building each provider's chat template.

### Model IDs and the `us.` prefix

Llama 3.1 and newer (including all Llama 4) are **`INFERENCE_PROFILE`-only** — they do not support `ON_DEMAND`, so a bare `meta.llama...` id fails and the `us.` cross-region profile prefix is required. Llama 3 (`meta.llama3-70b-instruct-v1:0`) is the exception and works bare. Check before switching:

```bash
aws bedrock list-foundation-models --by-provider meta \
  --query 'modelSummaries[].{id:modelId,inf:inferenceTypesSupported}'
```

A cross-region profile needs `bedrock:InvokeModel` on **both** the profile ARN *and* the underlying foundation model with a **wildcard region** (`arn:aws:bedrock:*::foundation-model/...`), because the profile routes across regions. `infra/main.tf` derives both from `var.llm_model_id`.

**Meta models need no access request; Anthropic models require a console opt-in** and return `403 ... is not available for this account` until granted. That is why this project defaults to Meta.

### Vector storage

Chunk id (`source:page:index`) is the S3 Vectors **key**, so de-duplication is a `get_vectors` lookup on those keys rather than a scan. Metadata splits deliberately: `source_text` holds the chunk body and is declared **non-filterable** (filterable metadata is capped at 2 KB/vector); `source` and `page` stay filterable for future per-document queries.

`search()` sets `returnMetadata=True`, which requires **`s3vectors:GetVectors` in addition to `QueryVectors`** — without it you get an AccessDenied that reads like a query bug.

Page numbers are 0-indexed. Because the id embeds the full filepath, the same PDF uploaded under a different path re-ingests as duplicates.

Pages with no extractable text are skipped at load time, so a scanned/image-only PDF silently contributes nothing — there is no OCR step.

### State lives in the Flask session, not the index

The home page branches on `session['embeddings_created']`. A populated index with a fresh session still renders the upload form — the app has no way to notice existing vectors. Anything that changes ingestion or reset behavior must keep that session flag in sync (`session.modified = True` is required; the code sets it explicitly).

`/reset_rag` calls `clear_database()` (which pages through `list_vectors` and deletes every key — the index itself is Terraform-managed and survives), empties `uploads/`, and clears the session.

### Routes

`/` (GET question form, POST full-page answer) · `/upload` (GET form, POST ingest → redirect) · `/upload_page` (GET form only) · `/ask_question` (POST, JSON — this is what the home page's AJAX actually calls) · `/reset_rag` (POST).

`/` POST and `/ask_question` both call `query_rag`; the form-based path on `/` is effectively dead since `home.html` intercepts submit and fetches `/ask_question`.

Retrieval is `k=5` similarity search; the prompt (`PROMPT_TEMPLATE` in `app.py`) instructs answering *only* from context, so retrieval failures surface as refusals rather than hallucinations.

## Templates

`base.html` defines a global `showLoader()`; `home.html` shadows it with its own `showLoader`/`hideLoader` pair inside a `DOMContentLoaded` handler. CSRF tokens come from the `inject_csrf_token` context processor and must be included as a hidden input in every form and as the `X-CSRFToken` header on fetch calls.

## Known rough edges

- `app.secret_key` falls back to a hardcoded dev value when `FLASK_SECRET_KEY` is unset — sessions are forgeable in that state.
- `/upload` runs ingestion synchronously inside the request; large PDFs block the worker until every Titan call returns.
- Uploads aren't extension-checked; `pypdf` will just throw on a non-PDF and 500 the request.
- `/upload` and `/ask_question` make paid AWS calls on every request, with no rate limiting or auth in front of them.
- `.env` (AWS credentials), `uploads/`, `.venv/`, and `infra/*.tfstate*` are gitignored.
