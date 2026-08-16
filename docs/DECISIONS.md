# Design decisions

Why this project is built the way it is. Each entry records what was decided, the evidence behind it, and what it costs — including the ones that were wrong first.

The project started as a local Flask + LangChain + Chroma + Ollama demo and moved to AWS. That history matters mainly because it explains what was *removed* and why.

---

## 1. Evals before refactoring

**Decision.** Build the golden set first, against the *original* stack, and record a baseline before changing anything.

**Why.** The starting app answered "How much money does each player start with in Monopoly?" with **$3,500**. The correct answer, present in the source PDF, is $1,500 — the PDF's OCR is garbled (`$100~`, `$50~`), so retrieval fed the model mangled denominations. Nothing in the app noticed. Every subsequent change (embedding model, vector store, LLM, framework removal) was going to alter answer quality, and without a baseline there would be no way to tell a regression from noise.

**Evidence.** Every expected value was verified to exist in the corpus before being asserted on — an eval that asserts absent facts tests nothing. Baseline on the original stack: **6/6**. After removing LangChain: **6/6**. After migrating to AWS: **11/11**. Today, with the offline behaviour suite alongside it: **21/21**.

**Consequence.** Every later decision could be settled by measurement instead of argument — most visibly the model choice (#7).

---

## 2. Remove LangChain, keep only the text splitter

**Decision.** Call `pypdf`, `boto3`, and the vector store directly. Retain `langchain-text-splitters` alone.

**Why.** Of six LangChain components in use, exactly one had non-trivial logic:

| Usage | What it provided | Replacement |
|---|---|---|
| `PyPDFLoader` | wrapper | `pypdf`, ~6 lines |
| `RecursiveCharacterTextSplitter` | **real logic** | kept |
| `Chroma` | wrapper | native client |
| `ChatPromptTemplate` | wrapper | f-string |
| `OllamaLLM` / `OllamaEmbeddings` | wrapper | native client |

The framework had already cost real time: a fresh install resolved to langchain 1.x, where the legacy `langchain.document_loaders` / `text_splitter` / `prompts` shims are gone, breaking every import in the repo.

**Evidence.** 13 langchain-family packages were installed for a ~150-line app, including all four `langgraph` packages and `langsmith` — none of which the code touched. The replacement diff was **+119/−108 lines**: essentially flat, confirming the wrappers weren't buying anything.

**The splitter was kept on measurement, not preference.** It drags in `langchain-core`, but that is 2.9 MB against 305 MB of site-packages — ~1%. Hand-rolling a recursive splitter to save 1% would be the wrong trade.

---

## 3. S3 Vectors over OpenSearch Serverless

**Decision.** Store embeddings in Amazon S3 Vectors.

**Why.** It is the cost-optimised tier, explicitly not the low-latency tier: AWS quotes ~100 ms for frequently-queried indexes and sub-second for cold ones, against single-digit ms for an in-memory HNSW index, in exchange for roughly 90% lower cost. Published comparisons put a 10M-vector corpus at ~$11/month on S3 Vectors versus ~$350 on OpenSearch Serverless.

**The stated requirement was "minimal latency", which is in tension with this choice.** It was taken anyway because the vector lookup is not the dominant latency term here:

| Step | Warm |
|---|---|
| Titan query embedding | ~50–100 ms |
| S3 Vectors query | ~100 ms |
| LLM generation | ~500 ms |
| Lambda cold start (once deployed) | 1–3 s |

Generation and cold start dominate. Paying 50× for the vector tier would shave a fraction of the total.

**When to revisit.** Sub-20 ms retrieval, high QPS per shard, or deep retrieval with reranking. The standard escalation is tiered: keep the cold corpus in S3 Vectors and import the hot subset into OpenSearch Serverless when a document set crosses a query-volume threshold.

---

## 4. Terraform over AWS CDK

**Decision.** Terraform, in `infra/`.

**Why.** Three reasons, in order of weight:

1. **CDK's S3 Vectors support is L1 only** (`CfnVectorBucket`, `CfnIndex` — raw CloudFormation escape hatches). CDK's entire advantage is L2/L3 constructs, none of which exist for the primary resource.
2. **`cdk bootstrap` leaves permanent residue** — a CDKToolkit stack, S3 bucket, ECR repo, and IAM roles that survive teardown. That defeats the "quick build and teardown" requirement outright.
3. Terraform appears more often in job listings.

**Evidence.** Validated against `hashicorp/aws` v6.60.0, which provides `aws_s3vectors_vector_bucket`, `aws_s3vectors_index`, and `aws_s3vectors_vector_bucket_policy`. Config passes `terraform validate` and `terraform fmt`.

**What Terraform cannot do.** Bedrock model access is a console action with no API. Any guide claiming fully automated Bedrock setup is wrong.

---

## 5. Bedrock Converse API over provider SDKs

**Decision.** Generate via `bedrock-runtime.converse()` rather than a provider-specific SDK or `invoke_model`.

**Why.** Converse is provider-agnostic — the same call shape serves Meta, Amazon, Mistral, and Anthropic. Swapping models is one `.env` edit, which is what makes eval-driven model selection (#7) practical. `invoke_model` would mean hand-building each provider's chat template; Llama's differs from Claude's, which differs from Titan's.

**Consequence.** Dropping the `anthropic` SDK removed a dependency while *keeping* Anthropic reachable — Converse serves those models too, if access is granted.

---

## 6. Meta models over Anthropic on Bedrock

**Decision.** Default to Llama on Bedrock.

**Why.** Anthropic models on Bedrock require a one-time console opt-in and return `403 ... is not available for this account` until granted. Meta models need no request and worked immediately.

**Evidence that this was account-level, not IAM:** the same call was tested with *admin* credentials, which bypass IAM entirely, and returned the identical 403 across `claude-sonnet-5`, `claude-opus-4-8`, and `claude-haiku-4-5`. `list-foundation-models` showed all of them `ACTIVE` in-region — offered, but not granted.

**Trade-off.** The Anthropic models are generally stronger. Converse (#5) means switching back is one env var once access is granted.

### 6a. The `us.` inference-profile prefix

Llama 3.1 and newer — including all Llama 4 — are **`INFERENCE_PROFILE`-only** and reject a bare `meta.llama...` id. Llama 3 is the exception. Check before switching:

```bash
aws bedrock list-foundation-models --by-provider meta \
  --query 'modelSummaries[].{id:modelId,inf:inferenceTypesSupported}'
```

The IAM consequence is non-obvious: a cross-region profile needs `bedrock:InvokeModel` on **both** the profile ARN *and* the underlying foundation model with a **wildcard region**, because the profile routes traffic across regions.

---

## 7. Model chosen by the golden set

**Decision.** `us.meta.llama4-scout-17b-instruct-v1:0`.

**Evidence.** Four candidates scored against the same golden set:

| Model | Score | Avg latency |
|---|---|---|
| **llama4-scout-17b** | 6/6 | **0.53 s** |
| llama4-maverick-17b | 6/6 | 0.53 s |
| llama3-70b | 6/6 | 0.69 s |
| llama3-3-70b | 6/6 | 0.82 s |

**Stated honestly: this eval did not discriminate on quality.** All four saturate a 6-case set, so the choice was made on latency and cost — Scout is the cheaper of the two fastest. Justifying a larger model would require harder cases: multi-hop questions, or ones needing synthesis across chunks.

---

## 8. Titan embeddings at 1024 dimensions

**Decision.** Keep Titan Text Embeddings V2 at its default 1024 dimensions.

**512 was proposed first and rejected.** The rationale was halving storage cost — but this corpus is ~20 MB, so S3 Vectors storage runs about **$0.001/month** at either setting. Optimising it saves nothing measurable, while **index dimension is immutable**: changing it destroys and rebuilds the index, dropping every vector. Reduced recall for no saving, locked in permanently.

**Generalisation.** Index name, dimension, distance metric, and non-filterable metadata keys are all immutable. Get them right at creation; there is no migration path short of a rebuild and full re-ingest.

---

## 9. Metadata split: filterable vs non-filterable

**Decision.** `source_text` (the chunk body) is declared **non-filterable**; `source` and `page` stay filterable.

**Why.** Filterable metadata is capped at **2 KB per vector**, and the body text is never filtered on — only returned. An 800-character chunk fits today, but raising `chunk_size` would silently breach the cap.

**Gotcha this creates.** `search()` sets `returnMetadata=True` to get chunk text back, which requires **`s3vectors:GetVectors` in addition to `QueryVectors`**. `QueryVectors` alone returns only keys and distances, and the resulting `AccessDenied` reads like a query bug.

---

## 10a. No long-lived credentials anywhere

**Decision.** Every consumer of the app's permissions gets short-lived credentials from a role. There are no static access keys.

| Consumer | Credential |
|---|---|
| Lambda (web + ingest) | Execution role |
| GitHub Actions | OIDC → assumed role, per job |
| Local development | Named AWS profile → assumed role via STS |

**What this replaced.** Terraform originally created an IAM *user* with an access key and wrote it to `.env`. That put a durable credential in two places it shouldn't be: a file on disk, and `terraform.tfstate` in plaintext. It was also the *only* static credential left — Lambda and CI were already role-based — so local dev was the odd one out rather than the norm.

`.env` now carries only non-secret configuration (which bucket, which index, which models). It cannot leak anything durable.

**Cost.** One extra setup step: appending the profile block from `terraform output -raw aws_profile` to `~/.aws/config`. `deploy/publish.sh` prints it when the profile is missing.

**One policy document, three consumers** — `data.aws_iam_policy_document.app` is attached to the Lambda execution role, the CI role, and the dev role, so the three cannot drift apart.

---

## 10. Two credentials by design

**Decision.** An **admin** profile provisions infrastructure; a least-privilege **app** user created by Terraform runs the application.

**Why.** The app needs embeddings, generation, and vector read/write — nothing else. It has no business reading IAM users or budgets.

**The failure mode this creates.** Sourcing `.env` into a shell exports the app credentials, which then outrank the admin profile in the AWS credential chain. Terraform — which must manage IAM — fails with `AccessDenied` on `iam:GetUser`, `s3vectors:ListTagsForResource`, and `budgets:ViewBudget`. It looks like three separate permission bugs; it is one wrong identity, and the least-privilege policy was working exactly as intended.

`python-dotenv` does not override existing environment variables, so the same pollution silently pins `LLM_MODEL` to a stale value even after `terraform output` rewrites the file. **Never `source .env`** — the app loads it in-process.

**Guard.** `conftest.py` prints the resolved region, index, embedding model, and LLM in the pytest header and warns when a shell export shadows `.env`. Eval numbers are meaningless without knowing which model produced them.

---

## 11. No retry on AccessDenied

**Decision.** Let `AccessDenied` fail loudly.

**Why.** IAM is eventually consistent. Running evals seconds after an apply that changed the policy produced a single `AccessDenied` on an ARN the policy demonstrably granted — while 5 of 6 calls to that same ARN succeeded in the same run. A genuine policy gap fails every call, not one; that ratio is the tell.

Adding retry logic would mask real permission bugs to paper over a condition that self-resolves in seconds. To distinguish the two, read the live policy rather than the Terraform source:

```bash
aws iam get-role-policy --role-name llama-rag-lambda --policy-name llama-rag-lambda
```

---

## 12. Stateless by construction

**Decision.** No server-side state at all. "Which documents exist" is answered by querying the index; uploads are staged in a temp directory and discarded.

**Why.** The app previously held three pieces of state, each of which breaks under a scale-to-zero, many-instance runtime:

| State | Problem |
|---|---|
| `session['embeddings_created']` | Per-browser, not per-system. The index is shared, so a new visitor saw "upload files" for a populated corpus — wrong even on a single machine. |
| `session['processing']` | Set and cleared inside one request; never observable. Dead code. |
| `get_uploaded_files()` reading `uploads/` | Local disk is per-instance and ephemeral. Files uploaded to instance A are invisible to instance B, and Lambda's filesystem is read-only outside `/tmp`. |

**Note on Flask sessions.** They are signed *client-side cookies*, so they are not inherently stateful and CSRF continues to work across instances — provided `FLASK_SECRET_KEY` is stable. A per-instance random key would invalidate every CSRF token on cold start, so Terraform generates one and passes it as an environment variable.

**Consequence — a chunk-id change.** Ids keyed on the full filepath (`data/monopoly.pdf:0:0`). With uploads staged in a per-request temp directory, that would make every re-upload a fresh set of duplicates instead of a no-op. Ids now key on the document *name* (`monopoly.pdf:0:0`). This required a one-time wipe and re-ingest; two documents with the same filename now collide by design.

---

## 13. Over-fetch from S3 Vectors

**Decision.** Request `k * 4` candidates and slice to `k` after sorting by distance.

**Why.** S3 Vectors is an approximate index and routinely returns **fewer results than `topK`** — measured on this corpus:

| topK | returned |
|---|---|
| 5 | 2 |
| 10 | 5 |
| 20 | 10 |
| 40 | 19 |
| 100 | 40 (whole index) |

Roughly half. So `k=5` was silently delivering **2 chunks of context**, degrading every answer, with nothing in the app aware of it. AWS documents over-fetching and post-processing as the mitigation for exactly this.

**How it surfaced.** The golden set. The statelessness refactor changed the index contents, which shifted which chunks landed in the top 2, and one factual case started failing on *retrieval*. The eval didn't catch a refactor bug — it exposed a latent one that had been quietly costing answer quality the whole time. This is the concrete payoff of decision #1.

---

## 14. Zip + Lambda layer over a container image

**Decision.** Package as a zip with the Lambda Web Adapter layer, on arm64.

**Why.** A container image would require an ECR repository, a docker build, and an image push on every deploy. The LWA layer needs none of that, and `uv --python-platform aarch64-manylinux2014` cross-compiles Linux wheels **on macOS without Docker** — verified by inspecting the resulting `.so` files as ARM aarch64 ELF.

Package size: **29 MB zipped / 81 MB unzipped**, against limits of 50 MB and 250 MB. This is only viable because of decision #2 — at the original 305 MB it would have forced a container.

arm64 is ~20% cheaper than x86_64 and cross-compiles just as cleanly.

**Trade-off.** Container images allow up to 10 GB and run identically locally. If dependencies ever outgrow 250 MB unzipped, that's the escape hatch.

---

## 15. Reserved concurrency on a public endpoint

**Decision.** `authorization_type = "NONE"` (genuinely public), with `reserved_concurrent_executions = 5`.

**Why.** Every request spends Bedrock tokens. An unauthenticated public URL with unbounded concurrency is an open invitation to spend real money. Reserved concurrency caps how many requests can be in flight, which bounds the worst case, and pairs with the existing budget alarm.

Setting it to `0` disables the function without destroying the infrastructure — a kill switch that survives `terraform apply`.

**Not solved.** This bounds throughput, not total spend, and there is still no authentication or per-IP rate limiting. For anything beyond a portfolio demo, put CloudFront with WAF rate rules in front, or require a shared secret.

---

## 16. Presigned S3 upload instead of uploading through Lambda

**Decision.** The browser requests a presigned POST and sends the file **directly to S3**. An S3 event then triggers a separate ingestion Lambda.

```
before:  browser ──6 MB cap──► Lambda ──synchronous embed──► S3 Vectors
after:   browser ──presigned POST──► S3 ──event──► ingest Lambda ──► S3 Vectors
```

**Why.** A 6.8 MB upload failed in production with `payload is too large for the RequestResponse invocation type (limit 6291456 bytes)`. Lambda rejects oversized invocations *before* the application runs, so the user sees raw AWS JSON and the app cannot render a useful error.

Capping uploads at ~4.5 MB was the first response. That was treating the symptom: it accepted a platform constraint as a product constraint, and left synchronous ingestion — the other half of the problem — untouched.

Routing uploads around Lambda removes both at once:

| Problem | Resolution |
|---|---|
| 6 MB invocation limit | Never applied to S3. Limit is now 64 MB, a product choice (`max_upload_mb`). |
| Ingestion blocking the request | Ingest Lambda gets a 900 s timeout; closing the browser no longer cancels it. |

**Presigned POST rather than PUT.** POST supports a `content-length-range` condition, so **S3 itself** enforces the size limit. With PUT, anyone hitting the public unauthenticated endpoint could request a URL and upload objects of arbitrary size.

**SigV4 forced explicitly.** boto3 still signs presigned POSTs with SigV2 in `us-east-1`; regions created after 2014 reject it. Without `signature_version="s3v4"` this works in the current region and breaks silently on any move.

**One package, two functions.** The ingest Lambda ships the same `deploy/app.zip` with a different handler (`ingest.handler`, no adapter layer) — one build, no drift.

**Chunk ids still key on basename**, so the `incoming/` prefix does not leak into them and re-uploads stay idempotent.

**Raw PDFs expire after 7 days.** The vectors are the durable artifact; the source is reproducible input. Bounds storage cost and data retention.

**Consequence for the UI.** "Upload returned" no longer means "ready to query", so the upload page polls `/documents` until the file appears in the index.

**Verified in production:** a 7.3 MB PDF — above the old ceiling — uploaded (204 in 4.6 s) and appeared in the index ~40 s later.

---

## 17. Session isolation by metadata filter, not separate indexes

**Decision.** One shared index; every vector carries a `session_id` and every query filters on it.

**Why.** The endpoint is public and unauthenticated, so without scoping any visitor could read — and delete — any other visitor's documents. The alternative, an index per session, is a stronger boundary but wrong here: S3 Vectors caps at 10,000 indexes per bucket, index creation adds latency to a visitor's first upload, and cleanup becomes a lifecycle problem per index.

Metadata filtering was nearly free because of an earlier decision: only `source_text` was declared non-filterable at index creation (#9), and filterable keys need no declaration — so `session_id` required **no index rebuild**.

**Verified on the live index:** with two sessions present, a Monopoly question asked in the session holding only Ticket to Ride returned exclusively Ticket to Ride chunks. Filtered recall was *better* than unfiltered, because the filter narrows the candidate set before the ×4 over-fetch.

**The hole this exposed.** `search(embedding, k=5, session_id=None)` defaulted to unfiltered — one forgotten keyword and a visitor's question is answered from everyone's documents, silently. `session_id` is now a **required positional argument**; opting out requires passing `ALL_SESSIONS` explicitly. The unsafe path is a `TypeError` rather than a data leak.

**Not solved.** Session equals cookie: copying it grants access, and there is no authentication. Correct for a public demo, wrong for real documents.

---

## 18. Scheduled expiry, because neither lifecycle nor TTL is precise enough

**Decision.** An EventBridge rule fires a cleanup Lambda every 15 minutes; it deletes the raw PDFs and vectors of any session idle longer than 60 minutes.

**Why not the obvious mechanisms:**

| Mechanism | Why it can't do this |
|---|---|
| S3 lifecycle rules | `Expiration` is expressed in **whole days**, minimum 1 |
| DynamoDB TTL | Best-effort — AWS deletes "typically within 48 hours" of expiry, not at the timestamp |

Both are garbage-collection hints, not schedulers. An hour-scale policy needs something that actually runs on a schedule.

Expiry keys on a session's **most recent** upload, so an active visitor isn't swept mid-use. The 7-day lifecycle rule stays as a backstop if the schedule ever stops firing. No DynamoDB: S3 prefix listing plus the vector scan cover it at this scale, and a table would earn its place only when that scan gets slow — the same threshold at which `list_sources()` needs a manifest.

---

## 19. Behaviour tests over a BDD framework

**Decision.** Given/When/Then *naming* in plain pytest (`test_behaviour.py`), against in-memory fakes in `conftest.py`. No `pytest-bdd`, no `behave`.

**Why.** Gherkin's payoff is a shared vocabulary with non-technical stakeholders who read and write specs. Without those readers you pay for feature files and a step registry and get nothing back. BDD is a practice, not a tool — the naming carries the clarity on its own.

The fakes are patched onto the **`app` module namespace**, because routes bind those names at import time; patching `populate_database` would have no effect.

**Evidence they work.** Each test maps to a bug this project shipped, and each was verified to fail when its bug is reintroduced:

| Reintroduced bug | Result |
|---|---|
| Reset skips deleting S3 files | ❌ that test only — 9 others still pass |
| Upload page posts multipart to Lambda | ❌ that test only — 9 others still pass |

A suite that stays green on a broken app is worse than no suite, so "does this fail for the right reason" is part of writing the test.

**Cost:** 0.3 s, no AWS, no credentials — so it runs on every push even while the stack is torn down.

---

## 20. A build stamp, because tests cannot catch a stale deploy

**Decision.** `build.sh` writes the git sha into the package; `/health` returns it; `deploy.sh` polls after applying and exits non-zero if the live version differs from HEAD.

**Why.** Terraform tracks the Lambda package by `filebase64sha256`. If the zip wasn't rebuilt, the hash is unchanged, Terraform reports "no changes", and **the old code keeps serving with no error anywhere**. This shipped: an apply succeeded while the site served pre-rename templates, and the symptom surfaced as an unrelated 6 MB upload failure.

No unit test can catch this — the code under test is correct; the code *deployed* is not. `deploy.sh` now runs build, apply, and publish in one command so the build cannot be skipped, and verifies the result rather than trusting it.

---

## Known open issues

- **No auth or per-IP rate limiting** in front of endpoints that make paid AWS calls; reserved concurrency bounds throughput only (#15). Sessions isolate *data*, not *spend*.
- **`list_sources()` scans all vector metadata** to answer "which documents exist". Fine for hundreds of documents; beyond that, keep a manifest in DynamoDB or a single S3 object.
- **No per-document scoping in the UI.** Questions search everything in *your session*; `source` is already filterable metadata, the UI just doesn't expose a picker.
- **The UI does not stream.** The infrastructure supports it (Function URL `RESPONSE_STREAM`), but `/ask_question` still returns a single JSON blob, so time-to-first-token is time-to-full-answer.
- **No OCR.** Pages with no extractable text are skipped silently, so scanned PDFs contribute nothing.
- **Root access keys.** The admin profile currently uses account root credentials, which AWS recommends deleting in favour of an IAM admin user.
