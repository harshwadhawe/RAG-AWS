# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

Provision AWS first — the app has no local fallback for embeddings, generation, or storage:

```bash
./deploy/deploy.sh                      # build -> apply -> publish -> verify version
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
terraform init
cd .. && ./deploy/deploy.sh                  # the only deploy command you need
cd infra && terraform destroy && cd ..       # teardown
./deploy/publish.sh --down                   # mark the README as not deployed
./deploy/verify_teardown.sh                  # confirm nothing survived
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

### Teardown and rebuild

`terraform destroy` removes everything it manages. Three things deliberately sit **outside** Terraform so a destroy/rebuild cycle does not lose them:

| Survives | Why it is outside Terraform |
|---|---|
| SSM `/llama-rag/langsmith-api-key` | A Terraform-managed secret lands in `terraform.tfstate` in plaintext. Kept out of state, so `destroy` cannot delete it and tracing works immediately after a rebuild. `deploy.sh` restores it from `.env.local` if it ever goes missing. |
| `.env.local` | Local settings and the LangSmith key. `publish.sh` rewrites `.env` wholesale but never touches this. |
| The `llama-rag` AWS profile in `~/.aws/config` | References the dev role by ARN. Role names are stable, so the same ARN reappears on rebuild and the profile keeps working. |

**What changes on rebuild:** the Lambda Function URL gets a new random id. `publish.sh` rewrites the README link and the GitHub repo variables, so run `deploy.sh` (which calls it) rather than a bare `terraform apply`.

**What stays valid:** the CI role ARN (name-stable, so the `AWS_CI_ROLE_ARN` secret needs no update) and the vector index name -- though the index itself is recreated empty, so re-ingest and re-run the evals.

Verify with `./deploy/verify_teardown.sh`. It discovers resources by the `Project` tag that `default_tags` stamps on everything, so it does not drift as resources are added -- the previous hardcoded version checked 7 while Terraform managed 37, and reported success with three Lambdas still running.

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
uv run pytest                          # everything: 21 tests
uv run pytest test_behaviour.py        # 10 offline behaviour tests, ~0.3s, no AWS
uv run pytest test_rag.py              # 11 golden-set tests against live AWS
```

Current state: **21/21 passing**.

`test_behaviour.py` uses in-memory fakes (fixtures in `conftest.py`) patched onto the **`app` module namespace** -- the routes bind those names at import time, so patching `populate_database` would not affect them. Each test maps to a bug this project shipped; when adding one, verify it *fails* with the bug reintroduced before trusting it.

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
| `ingest.py` | Two Lambda handlers: `handler` (S3 event -> ingest), `cleanup_handler` (scheduled session expiry) |
| `test_rag.py` | Golden set against live AWS |
| `test_behaviour.py` | Offline route/behaviour tests |
| `conftest.py` | Pytest header with resolved config, `.env`-shadowing warning, and the in-memory fakes |
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

### Tracing

Four `@traceable` decorators produce a nested run tree: `query_rag` (chain) → `embed_query` (embedding), `search` (retriever), `_generate` (llm). `langsmith` is declared in requirements.txt even though `langchain-core` pulls it in -- we depend on it directly, and a transitive dependency upstream drops is a silent breakage.

- **Valid `run_type` values are `{llm, prompt, parser, tool, chain, embedding, retriever}`.** An invalid one (e.g. `embedder`) only *warns* at runtime, so a mistyped span on an unexercised path ships silently.
- `name=` takes a string, not a callable.
- Enabled by `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY`; a no-op otherwise (~12 us per call).
- **Flushing is mandatory on Lambda, and must target the right client.** The exporter batches on a background thread the freeze kills, so `teardown_request` calls `get_cached_client().flush()`. Two traps here, both hit in practice:
  - `wait_for_all_tracers()` does **not** exist in langsmith >= 0.11 (it was the LangChain-era name).
  - `Client()` constructs a **new** client and flushes its empty queue. `@traceable` buffers into `run_trees.get_cached_client()`; flushing anything else leaves runs stuck **"pending"** in the UI -- start delivered, end never sent. A pending run is a flush bug, not an instrumentation bug.
- The key comes from an SSM SecureString read at cold start (`LANGSMITH_API_KEY_PARAM`), so it never enters terraform.tfstate. A failed fetch disables tracing and logs; a failed export logs and continues. Neither ever fails a request.
- Do not decorate hot inner loops (per-chunk embedding); the per-call cost is only negligible at per-request frequency.

For an always-on audit independent of tracing, enable Bedrock model invocation logging in Terraform -- it captures every prompt and completion account-wide with no code, but only sees *model* calls, so retrieval never appears there.

### Session isolation

The endpoint is public and unauthenticated, so every read and write is scoped to a `sid` held in the signed Flask cookie.

- Chunk key: `{sid}:{source}:{page}:{index}` -- the sid prefix is what the cleanup sweep groups on
- `session_id` is **filterable** metadata; only `source_text` was declared non-filterable at index creation, so this needed no index rebuild
- `search()` passes `filter={"session_id": sid}`, applied server-side by S3 Vectors
- Uploads land at `incoming/{sid}/{file}`; the presigned POST pins that exact key, so a client cannot write into another session
- `ingest.py` recovers the sid from the S3 key -- it is triggered by S3 and never sees the cookie

**`session_id` is a required positional argument** on `search()`, `list_sources()`, `clear_database()`, and `_scan()`. Pass `ALL_SESSIONS` (which is `None`) to opt out deliberately; only `cleanup_handler` does. This is why: a keyword with a `None` default meant one forgotten argument silently served every visitor's documents.

The eval corpus lives in session `evals`, so visitor uploads cannot perturb golden-set results.

### Session expiry

`ingest.cleanup_handler` runs on an EventBridge schedule (every 15 min) and deletes the raw PDFs *and* vectors of any session whose most recent upload is older than `session_ttl_minutes` (default 60). Expiring by most-recent-upload avoids sweeping an active visitor mid-use.

S3 lifecycle rules are day-granular and DynamoDB TTL is best-effort within ~48 h, so neither can express an hour-scale policy -- a scheduled sweep is the only mechanism with that precision. The 7-day lifecycle rule remains as a backstop if the schedule ever stops firing.

### Vector storage

Chunk id (`source:page:index`) is the S3 Vectors **key**, so de-duplication is a `get_vectors` lookup on those keys rather than a scan. Metadata splits deliberately: `source_text` holds the chunk body and is declared **non-filterable** (filterable metadata is capped at 2 KB/vector); `source` and `page` stay filterable for future per-document queries.

`search()` sets `returnMetadata=True`, which requires **`s3vectors:GetVectors` in addition to `QueryVectors`** — without it you get an AccessDenied that reads like a query bug.

Page numbers are 0-indexed. Because the id embeds the full filepath, the same PDF uploaded under a different path re-ingests as duplicates.

Pages with no extractable text are skipped at load time, so a scanned/image-only PDF silently contributes nothing — there is no OCR step.

### No server-side state

The only thing in the Flask cookie is `sid` (plus the CSRF token). "Which documents exist" is answered by querying the index for that session, never by a flag — so any Lambda instance can serve any request, and a populated index is visible immediately in any browser that owns the session.

`/reset_rag` deletes **both** this session's vectors and its raw PDFs in S3. Deleting only the vectors leaves a confusing half-state: documents vanish from search but still occupy the bucket, and nothing re-ingests them because S3 events fire only on new objects.

### Routes

| Route | Purpose |
|---|---|
| `/` | Chat UI, or the empty state when the session has no documents |
| `/ask_question` | POST, JSON — what the UI actually calls; returns answer, sources, metrics |
| `/upload_page` | The presigned-upload UI |
| `/upload_url` | POST — issues a presigned S3 POST scoped to `incoming/{sid}/` |
| `/documents` | JSON list of this session's indexed documents; polled during ingestion |
| `/reset_rag` | POST — deletes this session's vectors and raw files |
| `/health` | Build sha + model; used by `deploy.sh` to detect a stale deploy |
| `/upload` | Legacy direct-to-Flask upload; only reachable when `UPLOAD_BUCKET` is unset (local dev) |

Retrieval is `k=5` similarity search; the prompt (`PROMPT_TEMPLATE` in `app.py`) instructs answering *only* from context, so retrieval failures surface as refusals rather than hallucinations.

## Templates

Product naming comes from `APP_NAME` / `APP_TAGLINE` in `app.py` (overridable by env), injected via a context processor — changing the name is one edit, not a search-and-replace.

`upload.html` drives the presigned flow entirely in JS: request a policy, POST the file to S3, then poll `/documents` until the document is indexed. **It must never contain a `multipart/form-data` form posting to the app** — that path hits Lambda's 6 MB invocation limit, and `test_behaviour.py` asserts against it.

CSRF tokens come from the `inject_csrf_token` context processor and must be included as a hidden input in every form and as the `X-CSRFToken` header on fetch calls.

## Known rough edges

- `app.secret_key` falls back to a hardcoded dev value when `FLASK_SECRET_KEY` is unset — sessions are forgeable in that state.
- `/upload` runs ingestion synchronously inside the request; large PDFs block the worker until every Titan call returns.
- Uploads aren't extension-checked; `pypdf` will just throw on a non-PDF and 500 the request.
- `/upload` and `/ask_question` make paid AWS calls on every request, with no rate limiting or auth in front of them.
- `.env` (AWS credentials), `uploads/`, `.venv/`, and `infra/*.tfstate*` are gitignored.
