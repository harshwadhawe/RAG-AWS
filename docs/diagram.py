"""Generate the architecture diagram with official AWS service icons.

    brew install graphviz && uv pip install diagrams
    uv run python docs/diagram.py

Writes docs/architecture.png. Deliberately NOT in requirements.txt --
deploy/build.sh installs that file straight into the Lambda zip, and this is a
dev-only tool.

Regenerate whenever the architecture changes; a committed PNG that no longer
matches the code is worse than no diagram.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda

from diagrams.aws.management import Cloudwatch
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import Endpoint

from diagrams.aws.storage import S3
from diagrams.onprem.client import User

GRAPH_ATTR = {
    "fontsize": "20",
    # White, not transparent: a transparent PNG on GitHub's dark theme renders
    # the dark title and labels against dark grey, effectively invisible.
    "bgcolor": "white",
    "pad": "1.0",       # breathing room around the whole canvas
    "nodesep": "1.2",   # space between nodes on the same rank
    "ranksep": "2.2",   # space between ranks -- the main readability lever
    "splines": "ortho", # right-angle routing reads cleanly for infrastructure
    "concentrate": "false",
}
NODE_ATTR = {"fontsize": "13", "margin": "0.3,0.25"}
EDGE_ATTR = {"fontsize": "12"}

# Query path (green), upload path (orange), control plane (blue), telemetry (grey)
Q = lambda label: Edge(color="#2E7D32", penwidth="2.2", label=label, fontcolor="#2E7D32")
U = lambda label: Edge(color="#C2410C", penwidth="2.6", label=label, fontcolor="#C2410C")
O = lambda label: Edge(color="#757575", penwidth="1.4", style="dotted", label=label, fontcolor="#757575")

with Diagram(
    "LLAMA_RAG — serverless RAG on AWS",
    filename="docs/architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH_ATTR,
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    user = User("Browser")

    with Cluster("AWS · us-east-1 · provisioned by Terraform"):

        url = Endpoint("Function URL\nauth NONE · RESPONSE_STREAM")

        with Cluster("Compute · arm64 · one zip · one execution role"):
            web = Lambda("web\nFlask + LWA\n1 GB · 120 s")
            ingest = Lambda("ingest\npypdf → chunk 800/80\n1 GB · 900 s")

        with Cluster("Storage"):
            raw = S3("Raw uploads\ncontent-length-range\nexpire 7 d")
            vectors = S3("S3 Vectors\n1024-d cosine\nsource_text metadata")

        with Cluster("Amazon Bedrock"):
            titan = Bedrock("Titan Embeddings V2\n1024-d")
            llm = Bedrock("Llama 4 Scout\nConverse · temp 0")

        logs = Cloudwatch("Logs\nJSON metrics/query")

    # Only the edges that describe *structure*. The ordered request/upload
    # sequences live in the sequence diagrams in the README -- forcing them in
    # here produced 22 crossing edges and hid the architecture underneath.
    user >> Q("questions") >> url
    url >> Q("") >> web
    user >> U("uploads direct\n(presigned POST)") >> raw
    raw >> U("ObjectCreated") >> ingest

    web >> Q("embed · generate") >> titan
    web >> Q("") >> llm
    web >> Q("search ×4 over-fetch") >> vectors

    ingest >> U("embed") >> titan
    ingest >> U("upsert") >> vectors

    web >> O("latency · tokens · cost") >> logs
    ingest >> O("") >> logs
