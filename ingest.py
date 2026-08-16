"""S3 event handler: ingest a PDF that was uploaded directly to the raw bucket.

Runs as a separate Lambda from the web app, off the request path. The browser
PUTs straight to S3, so uploads never traverse Lambda's 6 MB invocation limit,
and embedding a large PDF can take minutes without holding an HTTP connection.

Shares the deployment package with the web app -- same zip, different handler.
"""

import os
import tempfile
import urllib.parse

import datetime
import json

import boto3

from populate_database import (ALL_SESSIONS, _scan, delete_keys,
                               process_pdfs_and_populate_database)

s3 = boto3.client("s3")

UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
# S3 lifecycle rules are expressed in whole days and DynamoDB TTL fires on a
# best-effort basis up to 48h late, so neither can implement an hour-scale
# retention policy. A scheduled sweep is the only mechanism with this precision.
SESSION_TTL_MINUTES = int(os.environ.get("SESSION_TTL_MINUTES", "60"))


def handler(event, context):
    ingested = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 event keys are URL-encoded and use '+' for spaces.
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        if not key.lower().endswith(".pdf"):
            print(f"skipping non-PDF object: {key}")
            continue

        # Keys are `incoming/<session_id>/<filename>` -- the uploader's session
        # is carried by the object path, since the ingest Lambda is triggered by
        # S3 and never sees the HTTP request or its cookie.
        parts = key.split("/")
        if len(parts) != 3 or parts[0] != "incoming":
            print(f"skipping unexpected key shape: {key}")
            continue
        session_id, filename = parts[1], parts[2]

        with tempfile.TemporaryDirectory() as staging:
            path = os.path.join(staging, filename)
            s3.download_file(bucket, key, path)
            print(f"ingesting {filename} ({os.path.getsize(path)} bytes) "
                  f"for session {session_id}")
            process_pdfs_and_populate_database([path], session_id)

        ingested.append(filename)

    return {"ingested": ingested}


def cleanup_handler(event, context):
    """Expire sessions older than SESSION_TTL_MINUTES.

    Invoked on an EventBridge schedule. Deletes each expired session's raw PDFs
    and its vectors together -- leaving one without the other produces a
    confusing half-state (documents that answer queries but cannot be listed,
    or listed documents that return nothing).

    The 7-day S3 lifecycle rule remains as a backstop if this ever stops running.
    """
    if not UPLOAD_BUCKET:
        print("UPLOAD_BUCKET unset; nothing to clean")
        return {"expired_sessions": 0}

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=SESSION_TTL_MINUTES
    )

    # A session is expired when its most recent upload is older than the cutoff,
    # so an active session is not swept out from under a visitor mid-use.
    newest = {}
    stale_objects = {}
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=UPLOAD_BUCKET, Prefix="incoming/"):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            if len(parts) != 3:
                continue
            sid = parts[1]
            newest[sid] = max(newest.get(sid, obj["LastModified"]), obj["LastModified"])
            stale_objects.setdefault(sid, []).append({"Key": obj["Key"]})

    expired = [sid for sid, seen in newest.items() if seen < cutoff]
    if not expired:
        print(f"no sessions older than {SESSION_TTL_MINUTES}m")
        return {"expired_sessions": 0}

    # One index scan for all expired sessions rather than one per session.
    expired_set = set(expired)
    doomed = [key for _, key in _scan(ALL_SESSIONS)
              if key.split(":", 1)[0] in expired_set]

    vectors_deleted = delete_keys(doomed)
    files_deleted = 0
    for sid in expired:
        objects = stale_objects[sid]
        for i in range(0, len(objects), 1000):  # DeleteObjects caps at 1000
            s3.delete_objects(Bucket=UPLOAD_BUCKET,
                              Delete={"Objects": objects[i:i + 1000]})
        files_deleted += len(objects)

    print(json.dumps({"event": "cleanup", "expired_sessions": len(expired),
                      "vectors_deleted": vectors_deleted,
                      "files_deleted": files_deleted,
                      "ttl_minutes": SESSION_TTL_MINUTES}))
    return {"expired_sessions": len(expired), "vectors_deleted": vectors_deleted,
            "files_deleted": files_deleted}
