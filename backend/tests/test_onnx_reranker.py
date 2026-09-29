"""ONNX reranker wrapper: preprocessing parity with CrossEncoder, and the
PyTorch fallback. No model download — tokenizer and session are fakes."""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import onnx_reranker  # noqa: E402


class _FakeTokenizer:
    def __init__(self):
        self.calls = []

    def __call__(self, first, second, **kwargs):
        self.calls.append((first, second, kwargs))
        n = len(first)
        return {
            "input_ids": np.arange(n * 3, dtype=np.int32).reshape(n, 3),
            "attention_mask": np.ones((n, 3), dtype=np.int32),
            "token_type_ids": np.zeros((n, 3), dtype=np.int32),
        }


class _Input:
    def __init__(self, name):
        self.name = name


class _FakeSession:
    """Declares only input_ids + attention_mask (like XLM-R); logit = row sum of ids."""

    def __init__(self):
        self.feeds = []

    def get_inputs(self):
        return [_Input("input_ids"), _Input("attention_mask")]

    def run(self, _outputs, feed):
        self.feeds.append(feed)
        return [feed["input_ids"].sum(axis=1, keepdims=True).astype(np.float32) * 0.01]


def test_predict_matches_cross_encoder_preprocessing():
    tok, sess = _FakeTokenizer(), _FakeSession()
    model = onnx_reranker.OnnxCrossEncoder(sess, tok)

    pairs = [("  wawa ", " doc one\n"), ("q2", "doc two"), ("q3", "doc three")]
    scores = model.predict(pairs, batch_size=2, show_progress_bar=False)

    assert [c[0] for c in tok.calls] == [["wawa", "q2"], ["q3"]]  # stripped, batched by 2
    assert tok.calls[0][1] == ["doc one", "doc two"]
    assert tok.calls[0][2] == {
        "padding": True, "truncation": "longest_first", "max_length": None, "return_tensors": "np",
    }
    assert all(set(f) == {"input_ids", "attention_mask"} for f in sess.feeds)  # only declared inputs
    assert all(f["input_ids"].dtype == np.int64 for f in sess.feeds)
    expected_logits = np.array([0.03, 0.12, 0.03])  # row sums of arange ids * 0.01
    np.testing.assert_allclose(scores, 1 / (1 + np.exp(-expected_logits)), rtol=1e-6)
    assert scores.dtype == np.float32


def test_concurrent_first_calls_load_one_reranker(monkeypatch):
    """Startup warm-up + first requests racing into get_reranker must not load
    the ~1 GB model more than once."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import retrieval

    loads = []
    lock = threading.Lock()

    def _slow_load():
        with lock:
            loads.append(1)
        time.sleep(0.2)
        return object()

    monkeypatch.setattr(retrieval, "_reranker", None)
    monkeypatch.setattr(retrieval, "ENABLE_ONNX_RERANKER", True)
    monkeypatch.setattr(retrieval, "load_onnx_reranker", _slow_load)
    with ThreadPoolExecutor(6) as pool:
        models = list(pool.map(lambda _: retrieval.get_reranker(), range(6)))

    assert len(loads) == 1
    assert all(m is models[0] for m in models)


def test_falls_back_to_pytorch_when_export_check_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(onnx_reranker, "ONNX_RERANKER_DIR", str(tmp_path))

    def _bad_export(model_path, meta_path):
        raise ValueError("ONNX export does not match PyTorch")

    monkeypatch.setattr(onnx_reranker, "_export", _bad_export)
    assert onnx_reranker.load_onnx_reranker() is None
