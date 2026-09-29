"""ONNX Runtime backend for the cross-encoder reranker.

Same model, same scores, ~25% faster on CPU than PyTorch: on the eval set's
28 real rerank calls, bge-reranker-base scored with a max difference of
3.5e-6 and identical ranking, top-K and rerank-floor survivors in every
call. Nothing about retrieval quality changes — only the runtime.

The model is exported once per RERANKER_MODEL into ONNX_RERANKER_DIR (baked
into the Docker image by the build step's get_reranker() call) and only used
after an export-time check against the PyTorch model on fixed validation
pairs. Any failure — onnxruntime missing, export error, a check that doesn't
match — returns None and retrieval keeps using PyTorch.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import numpy as np

from config import ONNX_RERANKER_DIR, RERANKER_MODEL

logger = logging.getLogger(__name__)

# Export check: every validation score within this of PyTorch's, and the
# same ranking. Measured drift is ~1e-6; ranking/floor decisions only
# change at differences many orders of magnitude larger.
_MAX_ABS_DIFF = 1e-4
_FORMAT_VERSION = 1

_LONG_DOC = (
    "The Village Council approved the Corkscrew Road widening agreement with Lee County, "
    "covering design, right-of-way acquisition and construction phasing from Ben Hill Griffin "
    "Parkway to the I-75 interchange, including sidewalks, bike lanes and drainage. "
) * 12  # well past 512 tokens, so truncation is exercised
_VALIDATION_PAIRS = [
    ("wawa", "Wawa Convenience Food & Beverage Store at 10081 Estero Town Commons Place was approved on 2023-08-22."),
    ("wawa", "The Estero rail trail feasibility study was presented to the Village Council."),
    ("What is the latest on Coconut Point?", "Waypoint Residences proposes 365 apartments at the former Regal Coconut Point theater site."),
    ("What is the latest on Coconut Point?", "  Consent agenda: approval of minutes and financial report.  "),
    ("Was the Estero Crossing rezoning approved?", "Ordinance No. 2019-29 rezoned Estero Crossing to Mixed Use Planned Development."),
    ("Was the Estero Crossing rezoning approved?", "Hertz Arena hosts the Florida Everblades home opener."),
    ("What is happening on Corkscrew Road?", _LONG_DOC),
    ("What is happening on Corkscrew Road?", "Sandy Lane special exception request for a church was continued."),
    ("history of the rail trail", "EsteroToday: Rail trail concept moves forward after council workshop in 2024."),
    ("DCI2021-E004", "DCI2021-E004 development order for Genova was approved with conditions."),
    ("new developments", "Recently approved developments include a medical office on Via Coconut."),
    ("what is a special exception", "A special exception allows a use the zoning district does not permit by right."),
]


class OnnxCrossEncoder:
    """Drop-in for the subset of CrossEncoder that retrieval uses:
    predict(pairs, batch_size=..., show_progress_bar=...) -> sigmoid scores.

    Preprocessing mirrors CrossEncoder.smart_batching_collate_text_only in
    sentence-transformers exactly (strip each text, pad per batch,
    longest_first truncation, max_length=None -> the tokenizer's own limit).
    """

    def __init__(self, session: Any, tokenizer: Any, max_length: int | None = None):
        self.session = session
        self.tokenizer = tokenizer
        self.max_length = max_length
        self._input_names = [i.name for i in session.get_inputs()]

    def predict(self, sentences, batch_size: int = 32, show_progress_bar: bool | None = None, **_: Any) -> np.ndarray:
        scores: list[np.ndarray] = []
        for start in range(0, len(sentences), batch_size):
            batch = sentences[start : start + batch_size]
            enc = self.tokenizer(
                [a.strip() for a, _b in batch],
                [b.strip() for _a, b in batch],
                padding=True,
                truncation="longest_first",
                max_length=self.max_length,
                return_tensors="np",
            )
            feed = {name: enc[name].astype(np.int64) for name in self._input_names}
            logits = self.session.run(None, feed)[0]
            scores.append((1.0 / (1.0 + np.exp(-logits[:, 0].astype(np.float64)))).astype(np.float32))
        return np.concatenate(scores) if scores else np.zeros(0, dtype=np.float32)


def _paths() -> tuple[str, str]:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "__", RERANKER_MODEL)
    return (
        os.path.join(ONNX_RERANKER_DIR, f"{stem}.onnx"),
        os.path.join(ONNX_RERANKER_DIR, f"{stem}.json"),
    )


def _session(path: str):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])


def _export(model_path: str, meta_path: str) -> None:
    """Export RERANKER_MODEL to ONNX and keep it only if it matches PyTorch."""
    import onnxruntime as ort
    import torch
    from sentence_transformers import CrossEncoder

    torch_model = CrossEncoder(
        RERANKER_MODEL,
        default_activation_function=torch.nn.Sigmoid(),
        automodel_args={"attn_implementation": "eager"},  # plain ops trace cleanly
    )
    hf_model = torch_model.model.eval()
    if hf_model.config.num_labels != 1:
        raise ValueError(f"expected a 1-label cross-encoder, got num_labels={hf_model.config.num_labels}")

    sample = torch_model.tokenizer(["q"], ["d"], padding=True, return_tensors="pt")
    input_names = list(sample.keys())  # e.g. input_ids, attention_mask[, token_type_ids]

    class _Logits(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, *tensors):
            return self.inner(**dict(zip(input_names, tensors)), return_dict=True).logits

    os.makedirs(ONNX_RERANKER_DIR, exist_ok=True)
    tmp_path = model_path + ".tmp"
    dynamic = {name: {0: "batch", 1: "seq"} for name in input_names}
    dynamic["logits"] = {0: "batch"}
    with torch.no_grad():
        torch.onnx.export(
            _Logits(hf_model),
            tuple(sample[name] for name in input_names),
            tmp_path,
            input_names=input_names,
            output_names=["logits"],
            dynamic_axes=dynamic,
            opset_version=17,
            dynamo=False,
        )

    candidate = OnnxCrossEncoder(_session(tmp_path), torch_model.tokenizer, torch_model.max_length)
    expected = np.asarray(torch_model.predict(_VALIDATION_PAIRS, batch_size=4, show_progress_bar=False))
    got = candidate.predict(_VALIDATION_PAIRS, batch_size=4)
    diff = float(np.max(np.abs(got - expected)))
    same_order = list(np.argsort(-got, kind="stable")) == list(np.argsort(-expected, kind="stable"))
    del candidate
    if diff > _MAX_ABS_DIFF or not same_order:
        os.remove(tmp_path)
        raise ValueError(f"ONNX export does not match PyTorch (max diff {diff:.2e}, same order {same_order})")

    os.replace(tmp_path, model_path)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "format": _FORMAT_VERSION,
                "model": RERANKER_MODEL,
                "max_abs_diff": diff,
                "torch": torch.__version__,
                "onnxruntime": ort.__version__,
            },
            fh,
            indent=1,
        )
    logger.info("Exported %s to ONNX (validation max diff %.1e)", RERANKER_MODEL, diff)


def load_onnx_reranker() -> OnnxCrossEncoder | None:
    """The ONNX reranker for RERANKER_MODEL, exporting it on first use.
    None when it can't be used — the caller falls back to PyTorch."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        logger.info("onnxruntime not installed; using the PyTorch reranker")
        return None

    model_path, meta_path = _paths()
    try:
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        if not (
            os.path.exists(model_path)
            and meta.get("model") == RERANKER_MODEL
            and meta.get("format") == _FORMAT_VERSION
        ):
            print(f"Exporting reranker {RERANKER_MODEL} to ONNX (one-time)…")
            _export(model_path, meta_path)

        from transformers import AutoTokenizer

        return OnnxCrossEncoder(_session(model_path), AutoTokenizer.from_pretrained(RERANKER_MODEL))
    except Exception as exc:  # noqa: BLE001 — never block retrieval on the fast path
        logger.warning("ONNX reranker unavailable (%s); using the PyTorch reranker", exc)
        return None
