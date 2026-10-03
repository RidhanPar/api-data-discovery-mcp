"""Download the local embedding model (BAAI/bge-small-en-v1.5, ONNX, int8-quantised).

    uv run python scripts/fetch_model.py [--dest DIR]

The files are taken from a public Docker Hub image, pinned by digest, and their
checksums are verified, so every machine (laptop, CI, the Azure image build) gets
byte-identical weights and the measured search results are reproducible.

Requires Docker. The model is MIT-licensed (BAAI); the image is Weaviate's
transformers-inference build.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE = (
    "semitechnologies/transformers-inference@sha256:301b962f1c7541f65db3f6922ee8376cc1a74361a338a0da57bc53e7e5a579d5"
)
MODEL_DIR_IN_IMAGE = "/app/models/model"
DEFAULT_DEST = Path.home() / ".cache" / "nordlys-models" / "bge-small-en-v1.5-onnx-q"
EXPECTED_SHA256 = {
    "model_quantized.onnx": "846e649cf411f347ad4da890508f580939c9b1e82822183afd1b8f1ad4f8618a",
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(dest: Path) -> bool:
    return all((dest / name).exists() and sha256(dest / name) == digest for name, digest in EXPECTED_SHA256.items())


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = parser.parse_args()
    dest: Path = args.dest

    if verify(dest):
        print(f"Model already present and verified: {dest}")
        return 0
    if shutil.which("docker") is None:
        print("Docker is required to fetch the model.", file=sys.stderr)
        return 1

    print(f"Pulling {IMAGE} ...")
    run(["docker", "pull", "-q", IMAGE])
    container = run(["docker", "create", IMAGE])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            run(["docker", "cp", f"{container}:{MODEL_DIR_IN_IMAGE}/.", tmp])
            if not verify(Path(tmp)):
                print("Checksum mismatch - refusing to install the model.", file=sys.stderr)
                return 1
            dest.mkdir(parents=True, exist_ok=True)
            for f in Path(tmp).iterdir():
                if f.is_file():
                    shutil.copy2(f, dest / f.name)
    finally:
        run(["docker", "rm", container])
    print(f"Installed and verified: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
