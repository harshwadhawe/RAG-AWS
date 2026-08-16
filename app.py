# app.py

from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.utils import secure_filename
import json
import os
import tempfile
import time
import uuid
from flask import jsonify
import boto3
from botocore.config import Config as BotoConfig
from populate_database import (process_pdfs_and_populate_database, clear_database,
                               search, list_sources)
from get_embedding_function import REGION, embed_query
from flask_wtf.csrf import CSRFProtect, generate_csrf
from langsmith import traceable

app = Flask(__name__)
# Must be stable across instances: Flask sessions are signed client-side cookies,
# so a per-instance random key would invalidate every CSRF token on cold start.
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-only-insecure-key')
ON_LAMBDA = bool(os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))
UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET', '')

# Uploads go browser -> S3 directly via a presigned POST, so Lambda's 6 MB
# invocation limit does not apply and this ceiling is a product choice rather
# than a platform one. It is enforced by S3 through a content-length-range
# condition, and mirrored in the browser so the user hears about it before
# spending the upload.
MAX_UPLOAD_BYTES = int(os.environ.get('MAX_UPLOAD_MB', '64')) * 1024 * 1024

# Only constrains the legacy direct-to-Flask upload path, which is what runs
# when UPLOAD_BUCKET is unset (local development without S3).
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES if not ON_LAMBDA else 6 * 1024 * 1024


# Single source of truth for product branding -- changing the name is one edit
# here (or one env var), not a search-and-replace across templates.
# Tracing on Lambda: the API key is fetched from SSM at cold start rather than
# passed as a Terraform-managed environment variable, so the secret never enters
# terraform.tfstate. Locally it comes from .env.local instead.
LANGSMITH_KEY_PARAM = os.environ.get('LANGSMITH_API_KEY_PARAM', '')
if LANGSMITH_KEY_PARAM and not os.environ.get('LANGSMITH_API_KEY'):
    try:
        os.environ['LANGSMITH_API_KEY'] = boto3.client(
            'ssm', region_name=REGION
        ).get_parameter(Name=LANGSMITH_KEY_PARAM, WithDecryption=True)['Parameter']['Value']
    except Exception as exc:  # tracing is never worth failing a request over
        print(json.dumps({'event': 'tracing_key_unavailable', 'error': str(exc)}))
        os.environ.pop('LANGSMITH_TRACING', None)

TRACING_ON = os.environ.get('LANGSMITH_TRACING', '').lower() == 'true'

APP_NAME = os.environ.get('APP_NAME', 'Paper Trail')
APP_TAGLINE = os.environ.get('APP_TAGLINE', 'Answers from your documents, with receipts.')
SOURCE_URL = os.environ.get('SOURCE_URL', 'https://github.com/harshwadhawe/RAG-AWS')


@app.context_processor
def inject_branding():
    return dict(app_name=APP_NAME, app_tagline=APP_TAGLINE, source_url=SOURCE_URL,
                max_upload_bytes=MAX_UPLOAD_BYTES)

csrf = CSRFProtect(app)

@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=generate_csrf())


PROMPT_TEMPLATE = """
Answer the question based only on the following context:

{context}

---

Answer the question based on the above context: {question}
"""

# `us.` prefixed IDs are cross-region inference profiles, which Llama 3.1 and
# newer require -- those models do not support ON_DEMAND on a bare model ID.
LLM_MODEL = os.environ.get('LLM_MODEL', 'us.meta.llama4-scout-17b-instruct-v1:0')

# Bedrock's Converse API is provider-agnostic: the same call shape works for
# Meta, Amazon, Mistral, and Anthropic models, so swapping LLM_MODEL in .env is
# the only change needed to try a different one. Using invoke_model instead
# would mean hand-building each provider's chat template.
bedrock = boto3.client('bedrock-runtime', region_name=REGION)


# Token counts come from the API and are exact. Prices are configuration --
# they drift, and a hardcoded guess quietly produces confident wrong numbers.
# Titan Text Embeddings V2 is $0.02 per 1M input tokens in US regions; look up
# the current generation rate at https://aws.amazon.com/bedrock/pricing/ and set
# LLM_PRICE_IN/OUT_PER_1M. Unset means cost is reported as null, never guessed.
EMBED_PRICE_PER_1M = float(os.environ.get('EMBED_PRICE_PER_1M', '0.02'))
LLM_PRICE_IN_PER_1M = os.environ.get('LLM_PRICE_IN_PER_1M')
LLM_PRICE_OUT_PER_1M = os.environ.get('LLM_PRICE_OUT_PER_1M')


def _cost_usd(in_tokens, out_tokens, embed_tokens):
    if LLM_PRICE_IN_PER_1M is None or LLM_PRICE_OUT_PER_1M is None:
        return None
    return round(
        in_tokens / 1e6 * float(LLM_PRICE_IN_PER_1M)
        + out_tokens / 1e6 * float(LLM_PRICE_OUT_PER_1M)
        + embed_tokens / 1e6 * EMBED_PRICE_PER_1M,
        8,
    )


def current_session():
    """Stable per-browser id, stored in Flask's signed session cookie.

    Reuses the cookie that already exists for CSRF, so this costs no new
    infrastructure. Signed with FLASK_SECRET_KEY, so a visitor cannot forge
    another session's id and read their documents.
    """
    if 'sid' not in session:
        session['sid'] = uuid.uuid4().hex[:16]
        session.permanent = False
    return session['sid']


@traceable(run_type="llm", name="bedrock_converse")
def _generate(prompt: str):
    """The model call, as its own span so tokens and latency attribute to it."""
    return bedrock.converse(
        modelId=LLM_MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        # temperature 0: this is extraction from supplied context, not creative
        # writing -- we want the same answer for the same retrieved chunks.
        inferenceConfig={"maxTokens": 1024, "temperature": 0},
    )


@traceable(run_type="chain", name="query_rag")
def query_rag(query_text: str, session_id: str):
    """Answer from the corpus. Returns (answer, source chunk ids, metrics)."""
    started = time.perf_counter()

    embedding = embed_query(query_text)
    embedded_at = time.perf_counter()

    results = search(embedding, session_id, k=5)
    searched_at = time.perf_counter()

    context_text = "\n\n---\n\n".join(text for text, _, _ in results)
    prompt = PROMPT_TEMPLATE.format(context=context_text, question=query_text)

    response = _generate(prompt)
    finished = time.perf_counter()

    response_text = response["output"]["message"]["content"][0]["text"]
    sources = [chunk_id for _, chunk_id, _ in results]
    usage = response.get("usage", {})
    # Titan bills the query embedding; roughly 4 chars per token.
    embed_tokens = max(1, len(query_text) // 4)

    metrics = {
        'embed_ms': round((embedded_at - started) * 1000, 1),
        'search_ms': round((searched_at - embedded_at) * 1000, 1),
        'generate_ms': round((finished - searched_at) * 1000, 1),
        'total_ms': round((finished - started) * 1000, 1),
        'input_tokens': usage.get('inputTokens'),
        'output_tokens': usage.get('outputTokens'),
        'chunks_retrieved': len(results),
        'cost_usd': _cost_usd(usage.get('inputTokens', 0),
                              usage.get('outputTokens', 0), embed_tokens),
        'model': LLM_MODEL,
        'session': session_id,
    }
    # One structured line per query: CloudWatch Logs Insights can aggregate p95
    # latency and cost per model directly off this without extra instrumentation.
    print(json.dumps({'event': 'query', **metrics}))

    return response_text, sources, metrics

@app.teardown_request
def _flush_traces(exception=None):
    """Ship spans before the Lambda container freezes.

    LangSmith batches on a background thread, and Lambda suspends the container
    the moment the response is returned -- so without an explicit flush the
    spans for the request that just ran are simply lost. The cost is a network
    round-trip on the request path, which is why tracing is a deliberate opt-in
    rather than always-on.
    """
    if not (TRACING_ON and ON_LAMBDA):
        return
    try:
        # get_cached_client() returns the instance @traceable buffers into.
        # `Client()` would construct a NEW client and flush its empty queue --
        # the spans would still be sitting on the tracer's client when the
        # container froze, which is exactly how runs end up stuck "pending"
        # in the UI: the start was sent, the end never was.
        from langsmith.run_trees import get_cached_client
        get_cached_client().flush()
    except Exception as exc:
        print(json.dumps({'event': 'trace_flush_failed', 'error': str(exc)}))


@app.route('/', methods=['GET', 'POST'])
def home():
    # The index is the source of truth for "are there documents", scoped to this
    # visitor's session. No server-side state: the id rides in a signed cookie,
    # so any instance can serve any request.
    sid = current_session()
    uploaded_files = list_sources(sid)

    if uploaded_files and request.method == 'POST':
        question = request.form['question']
        response, sources, _ = query_rag(question, sid)
        return render_template('home.html', embeddings_created=True, response=response,
                               sources=sources, question=question, uploaded_files=uploaded_files)

    return render_template('home.html', embeddings_created=bool(uploaded_files),
                           uploaded_files=uploaded_files)

def build_version():
    """Commit the running code was built from, stamped by deploy/build.sh."""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'VERSION')) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return 'dev'


@app.route('/health')
def health():
    """Liveness plus the deployed build id, so a stale deploy is detectable."""
    return jsonify({'status': 'ok', 'version': build_version(), 'model': LLM_MODEL})


@app.route('/documents')
def documents():
    """Polled by the upload page to watch ingestion complete."""
    return jsonify({'documents': list_sources(current_session())})


@app.route('/upload_url', methods=['POST'])
def upload_url():
    """Hand the browser a presigned POST so it uploads straight to S3.

    This is what keeps uploads off Lambda entirely: the file never passes
    through an invocation, so the 6 MB payload limit does not apply. A presigned
    POST (rather than PUT) is used because it can carry a content-length-range
    condition -- S3 itself rejects oversized files, which matters when the
    endpoint issuing these is public and unauthenticated.
    """
    if not UPLOAD_BUCKET:
        return jsonify({'error': 'uploads are not configured'}), 503

    filename = secure_filename(request.form.get('filename', ''))
    if not filename.lower().endswith('.pdf'):
        return jsonify({'error': 'only PDF files are accepted'}), 400

    # Force SigV4: boto3 still signs presigned POSTs with SigV2 in us-east-1,
    # which regions created after 2014 reject outright.
    s3 = boto3.client('s3', region_name=REGION,
                      config=BotoConfig(signature_version='s3v4'))
    presigned = s3.generate_presigned_post(
        Bucket=UPLOAD_BUCKET,
        Key=f'incoming/{current_session()}/{filename}',
        Fields={'Content-Type': 'application/pdf'},
        Conditions=[
            {'Content-Type': 'application/pdf'},
            ['content-length-range', 1, MAX_UPLOAD_BYTES],
        ],
        ExpiresIn=300,
    )
    return jsonify({'upload': presigned, 'document': filename})


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method != 'POST':
        return render_template('upload.html')

    files = [f for f in request.files.getlist('pdf_files') if f.filename]
    if not files:
        return redirect(url_for('home'))

    # Staged in a temp dir and discarded: the vectors are the durable artifact,
    # and a Lambda filesystem is read-only apart from /tmp and dies with the
    # instance. Chunk ids key on the filename, so the staging path is irrelevant.
    with tempfile.TemporaryDirectory() as staging:
        filepaths = []
        for file in files:
            filepath = os.path.join(staging, secure_filename(file.filename))
            file.save(filepath)
            filepaths.append(filepath)
        process_pdfs_and_populate_database(filepaths, current_session())

    return redirect(url_for('home'))

def clear_uploads(session_id=None):
    """Delete the raw PDFs backing the index. Returns the count removed.

    Clearing vectors alone leaves the source files in S3, which is a confusing
    half-state: the documents vanish from search but still occupy the bucket
    until the 7-day lifecycle rule expires them, and nothing re-ingests them
    because S3 events only fire on new objects.
    """
    if not UPLOAD_BUCKET:
        return 0

    prefix = f'incoming/{session_id}/' if session_id else 'incoming/'
    s3 = boto3.client('s3', region_name=REGION)
    removed = 0
    for page in s3.get_paginator('list_objects_v2').paginate(
            Bucket=UPLOAD_BUCKET, Prefix=prefix):
        objects = [{'Key': o['Key']} for o in page.get('Contents', [])]
        if objects:
            s3.delete_objects(Bucket=UPLOAD_BUCKET, Delete={'Objects': objects})
            removed += len(objects)
    return removed


@app.route('/reset_rag', methods=['POST'])
def reset_rag():
    sid = current_session()
    vectors = clear_database(sid)
    files = clear_uploads(sid)
    print(json.dumps({'event': 'reset', 'session': sid,
                      'vectors_deleted': vectors, 'files_deleted': files}))
    return redirect(url_for('home'))

@app.route('/ask_question', methods=['POST'])
def ask_question():
    question = request.form['question']
    response, sources, metrics = query_rag(question, current_session())
    return jsonify({'response': response, 'sources': sources, 'metrics': metrics})


@app.route('/upload_page', methods=['GET'])
def upload_page():
    return render_template('upload.html')

if __name__ == '__main__':
    # Nothing to create: state lives in S3 Vectors, uploads are staged in a
    # temp dir per request. The app writes nothing durable to local disk.
    #
    # Port 5000 by default, but overridable: macOS binds it to AirPlay Receiver
    # (ControlCenter), so a browser may reach AirPlay instead of Flask.
    app.run(debug=True, port=int(os.environ.get('PORT', '5000')))
