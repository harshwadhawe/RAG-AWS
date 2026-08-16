from langchain_ollama import OllamaEmbeddings

def get_embedding_function():
    # llama3.2 is chat-only and has no embedding head; needs a dedicated embedding model.
    embeddings = OllamaEmbeddings(model="nomic-embed-text")
    return embeddings
