#!/bin/bash
# Lambda Web Adapter entrypoint. LWA runs as an /opt/extensions layer, starts
# this script, waits for the port to accept connections, then translates Lambda
# invocations into ordinary HTTP requests against it.
#
# One worker on purpose: Lambda already gives each instance a single concurrent
# request, so extra workers only duplicate memory and slow cold start.
# `python3`, not `python`: the Lambda Python runtime does not guarantee a bare
# `python` on PATH. Invoked as a module because the console script in bin/ carries
# a shebang pointing at the build machine's interpreter.
exec python3 -m gunicorn app:app \
  --bind "0.0.0.0:${AWS_LWA_PORT:-8080}" \
  --workers 1 \
  --threads 1 \
  --timeout 120 \
  --access-logfile -
