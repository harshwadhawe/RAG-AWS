"""Generate the architecture diagrams with official AWS service icons.

    brew install graphviz && uv pip install diagrams
    uv run python docs/diagram.py

Writes three PNGs into docs/, one per concern:

    architecture_query.png   answering a question
    architecture_ingest.png  getting documents into the index
    architecture_ops.png     lifecycle, delivery, and telemetry

Split deliberately. One diagram covering all three needed ~20 edges across six
clusters, and graphviz routed them into a tangle that hid the architecture it
was meant to show. Each concern is legible on its own.

Deliberately NOT in requirements.txt -- deploy/build.sh installs that file
straight into the Lambda zip, and this is a dev-only tool.

Regenerate whenever the architecture changes; a committed PNG that no longer
matches the code is worse than no diagram.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.devtools import Codepipeline
from diagrams.aws.integration import Eventbridge
from diagrams.aws.management import Cloudformation, Cloudwatch, SystemsManagerParameterStore
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import Endpoint
from diagrams.aws.security import IAMRole
from diagrams.aws.storage import S3
from diagrams.onprem.client import User

GRAPH_ATTR = {
    "fontsize": "20",
    # White, not transparent: a transparent PNG on GitHub's dark theme renders
    # the dark title and labels against dark grey, effectively invisible.
    "bgcolor": "white",
    "pad": "1.0",
    "nodesep": "0.9",
    "ranksep": "1.6",
}
NODE_ATTR = {"fontsize": "13", "margin": "0.25,0.2"}
EDGE_ATTR = {"fontsize": "12"}

QUERY = "#2E7D32"
UPLOAD = "#C2410C"
CONTROL = "#1565C0"


def edge(color, label="", **kw):
    return Edge(color=color, fontcolor=color, label=label, penwidth="2.0", **kw)


def diagram(title, filename):
    return Diagram(
        title,
        filename=f"docs/{filename}",
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=GRAPH_ATTR,
        node_attr=NODE_ATTR,
        edge_attr=EDGE_ATTR,
    )


# --- 1. Answering a question -------------------------------------------------

with diagram("Query path — answering a question", "architecture_query"):
    user = User("Browser\nsigned cookie: sid")

    with Cluster("AWS · us-east-1"):
        url = Endpoint("Function URL\nauth NONE · streaming")
        web = Lambda("web\nFlask + Lambda Web Adapter")

        # Declared last-step-first: graphviz stacks same-rank nodes bottom-up,
        # so this reads 2-3-4 top to bottom. A Bedrock cluster box would group
        # the two models but force them adjacent, putting the vector search
        # outside the steps numbered around it.
        llm = Bedrock("Bedrock · Llama 4 Scout\ntemperature 0")
        vectors = S3("S3 Vectors\nfilter: session_id")
        titan = Bedrock("Bedrock · Titan\nEmbeddings V2 · 1024-d")

    user >> edge(QUERY, "1  question") >> url
    url >> edge(QUERY) >> web
    web >> edge(QUERY, "2  embed query") >> titan
    web >> edge(QUERY, "3  top-k  (×4 over-fetch)") >> vectors
    web >> edge(QUERY, "4  prompt + context") >> llm
    # constraint=false: without it the return edge makes the browser a
    # *downstream* rank, and graphviz folds the whole left-to-right flow back
    # on itself.
    web >> edge(QUERY, "5  answer + citations", style="dashed",
                constraint="false") >> user


# --- 2. Getting documents in -------------------------------------------------

with diagram("Ingestion path — uploads bypass Lambda", "architecture_ingest"):
    user = User("Browser")

    with Cluster("AWS · us-east-1"):
        web = Lambda("web\nsigns the upload policy")
        raw = S3("Raw uploads\nincoming/{sid}/\ncontent-length-range")
        ingest = Lambda("ingest\npypdf · chunk 800/80")
        # Declared last-step-first; graphviz stacks same-rank nodes bottom-up.
        vectors = S3("S3 Vectors\nkey: sid:file:page:idx")
        titan = Bedrock("Bedrock · Titan\nEmbeddings V2")

    # decorate: the browser's three edges span different rank distances, so
    # graphviz stacks their labels into one column where the eye pairs each
    # label with the wrong arrow. The leader lines make the pairing explicit.
    deco = {"decorate": "true"}
    user >> edge(UPLOAD, "A  request an upload URL", **deco) >> web
    web >> edge(UPLOAD, "B  presigned POST policy", style="dashed", **deco) >> user
    user >> edge(UPLOAD, "C  bytes straight to S3 —\nthey never enter a Lambda", **deco) >> raw
    raw >> edge(UPLOAD, "D  ObjectCreated") >> ingest
    ingest >> edge(UPLOAD, "E  embed chunks", **deco) >> titan
    ingest >> edge(UPLOAD, "F  upsert") >> vectors


# --- 3. Operations -----------------------------------------------------------

with diagram("Operations — lifecycle, delivery, telemetry", "architecture_ops"):
    with Cluster("Delivery"):
        tf = Cloudformation("Terraform\ninfra/")
        ci = Codepipeline("GitHub Actions\nOIDC · eval gate")

    with Cluster("AWS · us-east-1"):
        schedule = Eventbridge("EventBridge\nrate(15 min)")
        cleanup = Lambda("cleanup\nexpire sessions > 60 min")
        raw = S3("Raw uploads\n7-day lifecycle backstop")
        vectors = S3("S3 Vectors\nsession's chunks")
        role = IAMRole("Execution role\nno static keys")
        param = SystemsManagerParameterStore("SSM SecureString\nLangSmith key\n(outside Terraform)")
        logs = Cloudwatch("Logs\nJSON metrics per query")

    schedule >> edge(CONTROL, "every 15 min") >> cleanup
    cleanup >> edge(CONTROL, "delete expired") >> raw
    cleanup >> edge(CONTROL) >> vectors

    tf >> edge(CONTROL, "provisions", style="dashed") >> role
    ci >> edge(CONTROL, "assume role · golden set", style="dashed") >> vectors
    role >> edge(CONTROL, "read at cold start", style="dashed") >> param
    role >> edge(CONTROL, "structured logs", style="dotted") >> logs
