#!/usr/bin/env bash
# RAG pipeline via curl: embed -> retrieve (client-side) -> rerank -> chat.
# Start the broker first (see .env.example). This script shows the two broker
# calls; retrieval math (cosine top-k) is done by your app between them.
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8080}
KEY=${KEY:-YOUR_API_KEY}
AUTH="Authorization: Bearer $KEY"
JSON='content-type: application/json'

# 1. Embed the query (repeat for each document; rank client-side by cosine).
curl -s -H "$AUTH" -H "$JSON" \
  -d '{"model":"embed","input":"What is the capital of France?"}' \
  "$BASE/v1/embeddings"

# 2. Rerank your retrieved candidates for precision.
curl -s -H "$AUTH" -H "$JSON" \
  -d '{"model":"rerank","query":"What is the capital of France?",
       "documents":["The capital of France is Paris.",
                    "Berlin is the capital of Germany.",
                    "The Eiffel Tower is in Paris, France."],
       "top_n":2,"return_documents":true}' \
  "$BASE/v1/rerank"

# 3. Chat with the top context grounding the answer.
curl -s -H "$AUTH" -H "$JSON" \
  -d '{"model":"chat","messages":[
        {"role":"system","content":"Answer using ONLY the provided context."},
        {"role":"user","content":"Context: The capital of France is Paris.\n\nQ: What is the capital of France?"}]}' \
  "$BASE/v1/chat/completions"
