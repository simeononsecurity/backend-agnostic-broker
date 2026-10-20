"""Ollama quickstart via the OpenAI Python SDK (`pip install openai`).

Start the broker with this example's env first, then run this file. The broker is
OpenAI-compatible, so only `base_url` and `api_key` change.
"""

from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="YOUR_API_KEY")

# Non-streaming chat against the offline fake model.
resp = client.chat.completions.create(
    model="echo-fake",
    messages=[{"role": "user", "content": "Hello from the SDK"}],
)
print(resp.choices[0].message.content)

# Streaming.
for chunk in client.chat.completions.create(
    model="echo-fake",
    messages=[{"role": "user", "content": "stream this"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
print()

# With a real Ollama entry enabled, change model="echo-fake" to model="llama3".
