"""Turn the ProsusAI/finbert checkpoint into the three files the service reads.

**Build tooling, run once inside a Docker stage, and never imported by
`screener.sentiment`.** It lives here rather than in the package because it is
the only code in this repository that touches torch and transformers, and the
whole point of the split is that the image which serves the model carries
neither. `deploy/Dockerfile.sentiment` runs it in a builder stage and copies out
the output; nothing else ever runs it.

It writes, into one directory:

  finbert.onnx    the graph, opset 14, batch and sequence both dynamic
  tokenizer.json  the fast tokenizer, which the checkpoint does not ship --
                  ProsusAI/finbert has only vocab.txt, and transformers builds
                  the fast form from it at load time
  labels.json     the output columns in graph order, from the checkpoint's own
                  id2label. FinBERT's are ['positive', 'negative', 'neutral'],
                  which is neither alphabetical nor the order anyone guesses,
                  and reading them wrong sign-flips every score silently

and then **checks its own work before the build is allowed to succeed**. The
export is the step here that can go subtly wrong rather than loudly: a wrong
opset, a dropped token_type_ids, a dynamic axis that is not really dynamic all
produce a graph that loads, runs, and answers differently from the checkpoint.
So the ONNX is run beside the original over a corpus that includes the shapes
this system actually sees -- a one-line headline, a padded batch, a comment past
the 512-token window, cashtags, accents and emoji -- and any disagreement past a
tolerance fails the build. That is cheap here, where torch is already installed,
and impossible later, where it is not.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

# The shapes this system actually puts through the model, not a smoke test. A
# padded batch and an over-length text are where an export goes wrong.
CORPUS = [
    "Revenue beat expectations and margins expanded.",
    "The company slashed its full-year guidance after a weak quarter.",
    "Shares were unchanged in after-hours trading.",
    "$NVDA ripping again 🚀🚀 nobody can stop this",
    "Moody's cut the outlook to negative; spreads widened on the 2031s.",
    "Ok.",
    "Provisão para créditos de liquidação duvidosa aumentou no trimestre.",
    # Past the 512-token window, so truncation is exercised on both sides.
    "The quarter was mixed. " * 400,
]

# Probabilities, so this is in the units of the answer. Float32 arithmetic
# reordered by graph optimisation moves the last decimal place or two; anything
# larger is a different model rather than a different summation order.
TOLERANCE = 2e-4


def export(repo: str, revision: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)

    tokenizer = AutoTokenizer.from_pretrained(repo, revision=revision)
    model = AutoModelForSequenceClassification.from_pretrained(repo, revision=revision)
    model.eval()

    labels = [model.config.id2label[i] for i in range(model.config.num_labels)]
    if sorted(labels) != ["negative", "neutral", "positive"]:
        raise SystemExit(f"unexpected labels in {repo}: {labels}")

    tokenizer.save_pretrained(out / "tokenizer")
    # Lifted up beside the graph. The service reads one file, not a directory
    # laid out the way transformers happens to lay one out.
    (out / "tokenizer.json").write_bytes((out / "tokenizer" / "tokenizer.json").read_bytes())
    (out / "labels.json").write_text(json.dumps(labels))

    sample = tokenizer(CORPUS[:2], return_tensors="pt", padding=True, truncation=True)
    names = ("input_ids", "attention_mask", "token_type_ids")
    torch.onnx.export(
        model,
        tuple(sample[name] for name in names),
        str(out / "finbert.onnx"),
        input_names=list(names),
        output_names=["logits"],
        # Both axes, and the sequence one matters most: exported at a fixed
        # length the graph still runs and quietly reads a fraction of anything
        # longer.
        dynamic_axes={name: {0: "batch", 1: "sequence"} for name in names}
        | {"logits": {0: "batch"}},
        opset_version=14,
        do_constant_folding=True,
        # The TorchScript exporter, not the dynamo one torch now defaults to.
        # `input_names`, `output_names` and `dynamic_axes` are this path's
        # arguments: the new exporter reinterprets them, and the graph the
        # service loads is addressed by those exact names.
        dynamo=False,
    )
    print(f"exported {(out / 'finbert.onnx').stat().st_size / 1e6:.0f} MB, columns {labels}")


def reference(tokenizer, model, texts: list[str]) -> np.ndarray:
    encoded = tokenizer(texts, return_tensors="pt", padding=True, truncation=True, max_length=512)
    with torch.no_grad():
        return torch.softmax(model(**encoded).logits, dim=-1).numpy()


def verify(repo: str, revision: str, out: Path) -> None:
    """Run the exported graph beside the checkpoint and insist they agree."""
    import onnxruntime as ort
    from tokenizers import Tokenizer

    tokenizer = AutoTokenizer.from_pretrained(repo, revision=revision)
    model = AutoModelForSequenceClassification.from_pretrained(repo, revision=revision)
    model.eval()

    # Deliberately the *service's* tokenizer, loaded the way the service loads
    # it, rather than the transformers one used to build the reference. That is
    # what makes this a check on the files being shipped instead of a check on
    # two copies of the same object.
    fast = Tokenizer.from_file(str(out / "tokenizer.json"))
    fast.enable_truncation(max_length=512)
    fast.enable_padding(pad_id=fast.token_to_id("[PAD]"), pad_token="[PAD]")

    session = ort.InferenceSession(
        str(out / "finbert.onnx"), providers=["CPUExecutionProvider"]
    )
    wanted = {tensor.name for tensor in session.get_inputs()}

    worst = 0.0
    # Singly and as one padded batch: padding is the part an export can get
    # wrong without any single-row test noticing.
    for texts in ([[text] for text in CORPUS] + [CORPUS]):
        encodings = fast.encode_batch(texts)
        feed = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        logits = session.run(
            ["logits"], {k: v for k, v in feed.items() if k in wanted}
        )[0]
        shifted = np.exp(logits - logits.max(-1, keepdims=True))
        got = shifted / shifted.sum(-1, keepdims=True)
        worst = max(worst, float(np.abs(got - reference(tokenizer, model, texts)).max()))

    print(f"onnx agrees with the checkpoint to {worst:.2e}")
    if worst > TOLERANCE:
        raise SystemExit(
            f"the exported graph disagrees with {repo} by {worst:.2e}, over {TOLERANCE:.0e}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="ProsusAI/finbert")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    export(args.repo, args.revision, args.out)
    verify(args.repo, args.revision, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
