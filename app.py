# app.py

from flask import Flask, render_template, request, redirect, url_for
from werkzeug.utils import secure_filename
import json
import os
import tempfile
import time
from flask import jsonify
import boto3
from botocore.config import Config as BotoConfig
from populate_database import (process_pdfs_and_populate_database, clear_database,
                               search, list_sources)
from get_embedding_function import REGION, embed_query
from flask_wtf.csrf import CSRFProtect, generate_csrf

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


@app.context_processor
def inject_upload_limit():
    return dict(max_upload_bytes=MAX_UPLOAD_BYTES)

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


def query_rag(query_text: str):
    """Answer from the corpus. Returns (answer, source chunk ids, metrics)."""
    started = time.perf_counter()

    embedding = embed_query(query_text)
    embedded_at = time.perf_counter()

    results = search(embedding, k=5)
    searched_at = time.perf_counter()

    context_text = "\n\n---\n\n".join(text for text, _, _ in results)
    prompt = PROMPT_TEMPLATE.format(context=context_text, question=query_text)

    response = bedrock.converse(
        modelId=LLM_MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        # temperature 0: this is extraction from supplied context, not creative
        # writing -- we want the same answer for the same retrieved chunks.
        inferenceConfig={"maxTokens": 1024, "temperature": 0},
    )
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
    }
    # One structured line per query: CloudWatch Logs Insights can aggregate p95
    # latency and cost per model directly off this without extra instrumentation.
    print(json.dumps({'event': 'query', **metrics}))

    return response_text, sources, metrics

@app.route('/', methods=['GET', 'POST'])
def home():
    # The index is the single source of truth for "are there documents". No
    # session flag, no local directory listing -- so every instance renders the
    # same page for the same corpus, which is what makes this deployable behind
    # a scale-to-zero, many-instance runtime.
    uploaded_files = list_sources()

    if uploaded_files and request.method == 'POST':
        question = request.form['question']
        response, sources, _ = query_rag(question)
        return render_template('home.html', embeddings_created=True, response=response,
                               sources=sources, question=question, uploaded_files=uploaded_files)

    return render_template('home.html', embeddings_created=bool(uploaded_files),
                           uploaded_files=uploaded_files)

@app.route('/documents')
def documents():
    """Polled by the upload page to watch ingestion complete."""
    return jsonify({'documents': list_sources()})


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
        Key=f'incoming/{filename}',
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
        process_pdfs_and_populate_database(filepaths)

    return redirect(url_for('home'))

@app.route('/reset_rag', methods=['POST'])
def reset_rag():
    clear_database()
    return redirect(url_for('home'))

@app.route('/ask_question', methods=['POST'])
def ask_question():
    question = request.form['question']
    response, sources, metrics = query_rag(question)
    return jsonify({'response': response, 'sources': sources, 'metrics': metrics})


@app.route('/upload_page', methods=['GET'])
def upload_page():
    return render_template('upload.html')

if __name__ == '__main__':
    # Nothing to create: state lives in S3 Vectors, uploads are staged in a
    # temp dir per request. The app writes nothing durable to local disk.
    app.run(debug=True)
