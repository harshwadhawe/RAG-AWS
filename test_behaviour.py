"""Behaviour tests for the web surface.

Given / When / Then in plain pytest -- no BDD framework. Gherkin's payoff is a
shared vocabulary with non-technical stakeholders; without them you pay for the
feature files and step registry and get nothing back. The naming carries the
same clarity.

These run offline against in-memory fakes (see conftest.py), so the whole file
finishes in well under a second and needs no AWS credentials. test_rag.py is the
complementary half: it exercises real S3 Vectors and Bedrock for answer quality.

Every test here corresponds to a failure this project actually shipped.
"""

import io


def upload(client, name, content=b"%PDF-1.4 fake"):
    return client.post("/upload", data={"pdf_files": (io.BytesIO(content), name)},
                       content_type="multipart/form-data", follow_redirects=True)


# --- Documents are visible to the session that uploaded them ----------------

def test_a_new_visitor_sees_an_empty_library(client):
    # Given a visitor who has uploaded nothing
    # When they open the home page
    page = client.get("/").get_data(as_text=True)
    # Then they are invited to add documents rather than shown a chat box
    assert "No documents yet" in page


def test_uploading_a_document_makes_it_listed(client):
    # Given a visitor
    # When they upload a PDF
    upload(client, "handbook.pdf")
    # Then it appears in their library
    assert "handbook.pdf" in client.get("/documents").get_json()["documents"]


# --- Session isolation ------------------------------------------------------
# Regression guard for the whole point of session scoping: a public endpoint
# must never answer one visitor from another visitor's documents.

def test_a_second_visitor_cannot_see_the_first_visitors_documents(client):
    # Given one visitor who uploaded a document
    upload(client, "private.pdf")
    assert "private.pdf" in client.get("/documents").get_json()["documents"]

    # When a different browser (no cookies) asks
    client.cookie_jar.clear() if hasattr(client, "cookie_jar") else client.delete_cookie("session")

    # Then they see nothing of the first visitor's
    assert client.get("/documents").get_json()["documents"] == []


def test_answers_are_grounded_only_in_this_sessions_documents(client):
    # Given a visitor with one document
    upload(client, "mine.pdf")
    # When they ask a question
    body = client.post("/ask_question", data={"question": "what is this?"}).get_json()
    # Then every citation belongs to their own session
    assert body["sources"], "expected citations"
    assert all("mine.pdf" in s for s in body["sources"])


# --- Reset ------------------------------------------------------------------
# Shipped bug: reset deleted vectors but left the raw PDFs in S3, so documents
# vanished from search while still occupying the bucket.

def test_reset_removes_both_the_embeddings_and_the_uploaded_files(client, store):
    # Given a visitor with an indexed document
    upload(client, "temp.pdf")
    assert store.vectors and store.files

    # When they clear everything
    client.post("/reset_rag", follow_redirects=True)

    # Then neither the vectors nor the raw files remain
    assert store.vectors == {}, "vectors survived the reset"
    assert store.files == {}, "raw uploads survived the reset"


def test_reset_only_clears_the_callers_own_session(client, store):
    # Given another visitor's document already in the store
    store.ingest(["/tmp/someone_else.pdf"], "another-session")
    # And this visitor's own document
    upload(client, "mine.pdf")

    # When this visitor resets
    client.post("/reset_rag", follow_redirects=True)

    # Then the other visitor's data is untouched
    assert any(sid == "another-session" for sid, _ in store.vectors.values())


# --- Upload path ------------------------------------------------------------
# Shipped bug: a stale template served an upload form that POSTed the file
# straight to Lambda, hitting the 6 MB invocation limit.

def test_the_upload_page_never_posts_file_bytes_through_lambda(client):
    # Given the upload page
    page = client.get("/upload_page").get_data(as_text=True)
    # Then it does not submit a multipart form to the app
    assert "multipart/form-data" not in page, (
        "upload page posts files through Lambda; uploads must use a presigned POST"
    )
    # And it drives the presigned flow instead
    assert "upload_url" in page


def test_presigned_upload_is_refused_when_the_bucket_is_not_configured(client, monkeypatch):
    # Given an environment with no upload bucket (local dev)
    import app as web
    monkeypatch.setattr(web, "UPLOAD_BUCKET", "")
    # When the browser asks for an upload URL
    response = client.post("/upload_url", data={"filename": "x.pdf"})
    # Then it is told so explicitly rather than failing obscurely later
    assert response.status_code == 503


def test_non_pdf_uploads_are_rejected_before_reaching_s3(client, monkeypatch):
    import app as web
    monkeypatch.setattr(web, "UPLOAD_BUCKET", "some-bucket")
    response = client.post("/upload_url", data={"filename": "malware.exe"})
    assert response.status_code == 400


# --- Observability ----------------------------------------------------------

def test_every_answer_reports_its_cost_and_latency(client):
    upload(client, "doc.pdf")
    metrics = client.post("/ask_question", data={"question": "hi"}).get_json()["metrics"]
    for field in ("total_ms", "input_tokens", "output_tokens", "chunks_retrieved", "model"):
        assert field in metrics, f"missing {field}"
