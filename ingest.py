"""S3 event handler: ingest a PDF that was uploaded directly to the raw bucket.

Runs as a separate Lambda from the web app, off the request path. The browser
PUTs straight to S3, so uploads never traverse Lambda's 6 MB invocation limit,
and embedding a large PDF can take minutes without holding an HTTP connection.

Shares the deployment package with the web app -- same zip, different handler.
"""

import os
import tempfile
import urllib.parse

import boto3

from populate_database import process_pdfs_and_populate_database

s3 = boto3.client("s3")


def handler(event, context):
    ingested = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 event keys are URL-encoded and use '+' for spaces.
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        if not key.lower().endswith(".pdf"):
            print(f"skipping non-PDF object: {key}")
            continue

        # basename only: chunk ids key on the document name, so the S3 prefix
        # must not leak into them or re-uploads would duplicate.
        filename = os.path.basename(key)
        with tempfile.TemporaryDirectory() as staging:
            path = os.path.join(staging, filename)
            s3.download_file(bucket, key, path)
            print(f"ingesting {filename} ({os.path.getsize(path)} bytes)")
            process_pdfs_and_populate_database([path])

        ingested.append(filename)

    return {"ingested": ingested}
