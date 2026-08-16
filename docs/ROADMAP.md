# Gap analysis and roadmap

An honest assessment of what this project demonstrates, what it doesn't, and what to build next — measured against what 2026 AI-engineering roles actually ask for.

Kept deliberately blunt. The point is to prioritise work, not to market the project.

---

## Where it stands

Hiring guidance for 2026 consistently names the same signals: production instincts (error handling, evaluation, deployment, structured thinking), and specialisation in retrieval, evaluation, agents, and observability. RAG is simultaneously the **most in-demand skill** and the **most saturated portfolio category** — every bootcamp ships a "chat with your PDF".

That produces a split verdict for this project:

- **Engineering rigour: strong.** Measured decisions, recorded baselines, real debugging evidence, deployed infrastructure with teardown.
- **Concept novelty: low.** The artifact is the single most common GenAI portfolio project in existence.

The engineering narrative is the differentiator, not the application. Most portfolios have the opposite problem, which is the harder one to fix.

## Assessment by dimension

| Dimension | Standing | Evidence / gap |
|---|---|---|
| Deployment | Strong | Live Function URL, IaC, one-command deploy with a stale-build check, one-command teardown |
| Evaluation discipline | Strong | Golden set with verified answers, baselines across three migrations, retrieval/generation split, gated in CI |
| Behaviour testing | Strong | 10 offline route tests, each verified to fail when its bug is reintroduced |
| Session isolation | Strong | Server-side filtering, signed cookie, presigned key pinning; unsafe path is a TypeError |
| Infrastructure | Strong | Terraform, least-privilege IAM, no static credentials anywhere (roles + OIDC) |
| Cost engineering | Strong | 305 MB → 62 MB tied to a real constraint; budget alarm; per-request cost accounting; 60-min data expiry |
| Observability | Moderate | Structured per-query JSON (latency split, tokens, cost) and `/health`; no distributed tracing |
| Documentation | Strong | Decision records including reversed decisions and shipped bugs |
| Debugging evidence | Strong | Over-fetch discovery, OIDC immutable-id subjects, credential shadowing, IAM eventual consistency |
| Eval depth | Moderate | 6 golden cases, saturated by every model tested; no groundedness metric |
| Retrieval sophistication | Moderate | Vector-only; no reranking, no hybrid BM25, no query rewriting |
| Security posture | Moderate | Sessions isolated, credentials short-lived; still no auth or rate limiting on a paid endpoint |
| **Concept novelty** | **Weak** | Most saturated category in the field |
| **Agentic capability** | **Absent** | Single-turn retrieve-and-generate; no tool use, routing, or multi-step reasoning |
| Frontend | Weak | Functional, dated |

## The strongest asset

**The over-fetch discovery**, in `DECISIONS.md` #13. S3 Vectors returns roughly half of `topK`, so `k=5` was silently supplying the model with 2 chunks and degrading every answer. It was found because the golden set caught a retrieval failure during an unrelated refactor — cause, measurements, fix, verification.

That is a production-debugging narrative with evidence attached, and it is worth more in an interview than any feature added this week. It should be led with, not buried.

## Prioritised backlog

Ordered by hiring signal per hour of work, not by technical interest.

### ~~1. Eval gate in CI~~ — done
GitHub Actions running `pytest test_rag.py` on every PR, with the pass count in the job summary. "I gate merges on retrieval scores" is a sentence very few candidates can say truthfully. Cheapest signal available.

Needs an AWS role for GitHub OIDC (no long-lived keys) — itself a credible infrastructure detail.

### ~~2. Per-request cost and latency accounting~~ — done
Log input/output tokens and dollar cost per query; surface a table in the README. Job descriptions name inference latency and token cost explicitly, and nothing currently measures either at request granularity.

### 3. Authentication or rate limiting — ~1 hour
Still the largest remaining gap. Session isolation stops visitors reading each other's documents, but anyone can still spend your Bedrock tokens. Reserved concurrency bounds throughput, not spend.

### ~~3b. Session isolation and 60-minute expiry~~ — done
Per-visitor scoping enforced server-side, plus a scheduled sweep. Isolates *data*, not *spend* — which is why #3 remains open.

### 4. Reranking, measured — ~3 hours
`flashrank` cross-encoder: retrieve 20, rerank to 5, and record eval scores before and after. Converts the weakest technical dimension into a second measurement story, using the harness that already exists.

### 5. Harder eval cases — ~2 hours
Multi-hop questions and ones requiring synthesis across chunks, plus a groundedness check (does every claim trace to a retrieved chunk?). The current set is saturated, so it can no longer distinguish between models — which makes model selection unfalsifiable.

### 6. An agent layer — 1–2 days
The largest differentiation move, and the one that changes the *category* rather than the quality: query routing (retrieve vs. answer directly vs. decline), multi-step retrieve → synthesise → verify, or exposing the corpus as an MCP server. This is where the market has moved and where this project is currently silent.

### 7. Streaming responses — ~3 hours
Infrastructure already supports it (Function URL `RESPONSE_STREAM`); only `/ask_question` needs to emit SSE. Time-to-first-token currently equals time-to-full-answer.

### 8. Per-document scoping in the UI — ~2 hours
`source` is already filterable metadata; the UI doesn't expose a picker, so a question searches every document in the session at once.

## Deliberately not doing

- **Migrating off S3 Vectors.** The cost/latency trade is correct at this scale and the reasoning is documented (#3). Switching would remove an interesting decision, not add one.
- **Reintroducing a framework.** The removal is measured and documented; adding LangChain back for agent orchestration would undo the clearest dependency-discipline evidence in the repo. If an agent layer needs orchestration, weigh it explicitly rather than by default.
- **Frontend rewrite.** Lowest signal-per-hour for backend and AI-engineering roles.
