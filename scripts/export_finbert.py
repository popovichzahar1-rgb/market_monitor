"""One-time export of ProsusAI/finbert to a quantized ONNX model.

Produces <out>/model.onnx (int8, ~110 MB instead of ~440 MB), tokenizer.json and
config.json; runtime then only needs `onnxruntime` + `tokenizers` (no torch).

    pip install -e ".[export]"
    python scripts/export_finbert.py --out models/finbert-onnx

In GitHub Actions this runs only on a cache miss (see .github/workflows).
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

REPO = "ProsusAI/finbert"
SAMPLES = [
    "Stocks rallied to a record high after strong earnings.",
    "The exchange halted withdrawals after a $200 million hack.",
    "The central bank left interest rates unchanged.",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="models/finbert-onnx")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--revision", default="main", help="pin a commit for reproducibility")
    parser.add_argument("--no-quantize", action="store_true")
    args = parser.parse_args()

    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(args.repo, revision=args.revision, use_fast=True)
    model = AutoModelForSequenceClassification.from_pretrained(args.repo, revision=args.revision)
    model.eval()

    dummy = tokenizer(SAMPLES[:1], return_tensors="pt", padding="max_length", max_length=16,
                      truncation=True)
    names = ["input_ids", "attention_mask", "token_type_ids"]
    fp32 = out / "model_fp32.onnx"
    export_kwargs = dict(
        input_names=names,
        output_names=["logits"],
        dynamic_axes={**{n: {0: "batch", 1: "sequence"} for n in names}, "logits": {0: "batch"}},
        opset_version=17,
    )
    with torch.no_grad():
        try:
            torch.onnx.export(model, tuple(dummy[n] for n in names), str(fp32), dynamo=False,
                              **export_kwargs)
        except TypeError:  # older torch without the `dynamo` argument
            torch.onnx.export(model, tuple(dummy[n] for n in names), str(fp32), **export_kwargs)

    target = out / "model.onnx"
    if args.no_quantize:
        shutil.move(fp32, target)
    else:
        quantize_dynamic(str(fp32), str(target), weight_type=QuantType.QInt8)
        fp32.unlink()
    tokenizer.backend_tokenizer.save(str(out / "tokenizer.json"))
    model.config.to_json_file(str(out / "config.json"))
    print(f"exported {target} ({target.stat().st_size / 1e6:.0f} MB)")

    # Smoke test with the runtime code path.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from market_monitor.config import SentimentConfig
    from market_monitor.scoring.sentiment import OnnxFinbert

    finbert = OnnxFinbert(SentimentConfig(model_dir=str(out)))
    start = time.monotonic()
    for text, result in zip(SAMPLES, finbert.predict(SAMPLES)):
        print(f"{result.label:>8} {result.value:+.2f}  {text}")
    print(f"inference incl. load: {time.monotonic() - start:.2f}s")


if __name__ == "__main__":
    main()
