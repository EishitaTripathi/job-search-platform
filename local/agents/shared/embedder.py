"""Local embeddings via ONNX Runtime + all-MiniLM-L6-v2.

Usage:
    embedder = LocalEmbedder()
    vector = embedder.embed("some text")         # returns list[float], 384 dims
    vectors = embedder.embed_batch(["a", "b"])    # batch embedding

Used for email classification few-shot retrieval in ChromaDB (local/PII only).
Cloud embeddings use Bedrock Titan — the two vector spaces never intersect.
"""

import os

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


class LocalEmbedder:
    """ONNX-based sentence embedder for local use."""

    def __init__(self, model_path: str | None = None):
        model_path = model_path or os.environ.get(
            "ONNX_MODEL_PATH", "local/models/all-MiniLM-L6-v2"
        )
        self._session = ort.InferenceSession(
            os.path.join(model_path, "model.onnx"),
            providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(
            os.path.join(model_path, "tokenizer.json")
        )
        self._tokenizer.enable_padding(length=128)
        self._tokenizer.enable_truncation(max_length=128)

    def embed(self, text: str) -> list[float]:
        """Embed a single text string. Returns 384-dim float vector."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts. Returns list of 384-dim float vectors."""
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        outputs = self._session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )

        # Mean pooling over token embeddings (masked)
        token_embeddings = outputs[0]  # (batch, seq_len, hidden_dim)
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        summed = np.sum(token_embeddings * mask_expanded, axis=1)
        counts = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        embeddings = summed / counts

        # L2 normalize
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        normalized = embeddings / norms

        return normalized.tolist()
