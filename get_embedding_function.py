from functools import lru_cache

import ollama

# llama3.2 is chat-only and has no embedding head; needs a dedicated embedding model.
EMBED_MODEL = "nomic-embed-text"


def embed_texts(texts):
    """Embed a list of strings. Returns one vector per input, in order."""
    return ollama.embed(model=EMBED_MODEL, input=list(texts))["embeddings"]


@lru_cache(maxsize=512)
def embed_query(text):
    """Embed a single query string. Cached -- repeat questions skip the model."""
    return tuple(embed_texts([text])[0])
