"""End-to-end RAG pipeline against the broker: embed -> retrieve -> rerank -> chat.

Uses the OpenAI Python SDK (`pip install openai`) for embeddings and chat, and a
plain stdlib POST for `/v1/rerank` (the OpenAI SDK has no rerank method). Runs
against the fake backend, so it works with no GPU. Start the broker first:
    set -a && . ./examples/rag-pipeline/.env.example && set +a
    python -m broker
"""

import json
import math
import urllib.request

from openai import OpenAI

BASE = "http://127.0.0.1:8080"
KEY = "YOUR_API_KEY"
client = OpenAI(base_url=f"{BASE}/v1", api_key=KEY)

CORPUS = [
    "The capital of France is Paris.",
    "Bananas are a yellow tropical fruit rich in potassium.",
    "Berlin is the capital and largest city of Germany.",
    "The Eiffel Tower is a wrought-iron tower in Paris, France.",
]
QUESTION = "What is the capital of France?"


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def rerank(query, documents, top_n):
    """POST /v1/rerank — not part of the OpenAI SDK, so call it directly."""
    req = urllib.request.Request(
        f"{BASE}/v1/rerank",
        data=json.dumps(
            {"model": "rerank", "query": query, "documents": documents,
             "top_n": top_n, "return_documents": True}
        ).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)["results"]


# 1. EMBED the corpus and the question.
doc_vecs = [e.embedding for e in client.embeddings.create(model="embed", input=CORPUS).data]
q_vec = client.embeddings.create(model="embed", input=QUESTION).data[0].embedding

# 2. RETRIEVE the top-k by cosine similarity (coarse recall).
ranked = sorted(range(len(CORPUS)), key=lambda i: _cosine(q_vec, doc_vecs[i]), reverse=True)
candidates = [CORPUS[i] for i in ranked[:3]]

# 3. RERANK the candidates for precision (cross-encoder reordering).
top = rerank(QUESTION, candidates, top_n=2)
context = [r["document"]["text"] for r in top]

# 4. CHAT with the retrieved context grounding the answer.
answer = client.chat.completions.create(
    model="chat",
    messages=[
        {"role": "system", "content": "Answer using ONLY the provided context."},
        {"role": "user", "content": f"Context:\n- " + "\n- ".join(context) + f"\n\nQ: {QUESTION}"},
    ],
)
print("Top context:", context)
print("Answer:", answer.choices[0].message.content)
