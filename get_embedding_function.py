import json
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import boto3
from dotenv import load_dotenv

load_dotenv()

REGION = os.environ.get("AWS_REGION", "us-east-1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
# Must equal the vector index's dimension -- S3 Vectors rejects a mismatch, and
# the index dimension is immutable after creation.
EMBED_DIMENSION = int(os.environ.get("EMBED_DIMENSION", "1024"))


@lru_cache(maxsize=1)
def _bedrock():
    return boto3.client("bedrock-runtime", region_name=REGION)


def _embed_one(text):
    response = _bedrock().invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text, "dimensions": EMBED_DIMENSION}),
    )
    return json.loads(response["body"].read())["embedding"]


def embed_texts(texts):
    """Embed a list of strings. Returns one vector per input, in order.

    Titan takes one input per call, so ingesting a document is N round trips.
    The pool keeps that off the critical path for multi-page PDFs.
    """
    texts = list(texts)
    if len(texts) == 1:
        return [_embed_one(texts[0])]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_embed_one, texts))


@lru_cache(maxsize=512)
def embed_query(text):
    """Embed a single query string. Cached -- repeat questions skip the model."""
    return tuple(_embed_one(text))
