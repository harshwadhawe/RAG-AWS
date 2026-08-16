"""Generate the architecture diagram with official AWS service icons.

    uv pip install diagrams && brew install graphviz
    uv run python docs/diagram.py

Writes docs/architecture.png. The Mermaid diagram in the README stays the
canonical one -- it renders inline on GitHub and cannot drift unnoticed. This
produces the higher-fidelity image for slides, posts, and the README header.

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
    "fontsize": "16",
    "bgcolor": "transparent",
    "pad": "0.5",
    "splines": "spline",
    "nodesep": "0.6",
    "ranksep": "1.4",
}

QUERY = Edge(color="#2E7D32", penwidth="2.0")   # thin: question -> answer
UPLOAD = Edge(color="#C2410C", penwidth="3.0")  # bold: bytes bypass Lambda
OBS = Edge(color="#7A7A7A", style="dashed")

with Diagram(
    "LLAMA_RAG — serverless RAG on AWS",
    filename="docs/architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH_ATTR,
):
    user = User("Browser")

    with Cluster("AWS us-east-1 · provisioned by Terraform"):
        url = Endpoint("Function URL\nauth NONE")

        with Cluster("Lambda · arm64 · one shared zip"):
            web = Lambda("web\nFlask + LWA\n1 GB · 120 s")
            ingest = Lambda("ingest\nS3 event\n1 GB · 900 s")

        raw = S3("Raw uploads\nexpire 7 days")

        with Cluster("Amazon Bedrock"):
            titan = Bedrock("Titan Embeddings V2\n1024-d")
            llm = Bedrock("Llama 4 Scout\ntemperature 0")

        vectors = S3("S3 Vectors\n1024-d cosine")
        logs = Cloudwatch("Logs\nJSON metrics")

        # Query path. Single-headed: `a >> Edge() << b` draws arrows BOTH ways.
        user >> QUERY >> url >> QUERY >> web
        web >> QUERY >> titan
        web >> QUERY >> vectors
        web >> QUERY >> llm

        # Upload path -- the bytes never enter a Lambda invocation.
        user >> UPLOAD >> raw >> UPLOAD >> ingest
        ingest >> UPLOAD >> titan
        ingest >> UPLOAD >> vectors

        web >> OBS >> logs
        ingest >> OBS >> logs
