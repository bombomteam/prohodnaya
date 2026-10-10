"""Export Silent-Face-Anti-Spoofing MiniFASNet weights (.pth) to ONNX and check the export.

What it does:
  1. For every ``*.pth`` in the models dir (default ``face/models/minifasnet``) builds the network
     exactly like ``src/anti_spoof_predict.py`` of the source repo (model type, input size and
     kernel parsed from the file name with the vendored ``utility.py``), loads the state dict and
     exports ``softmax(model(x))`` to ``<same name>.onnx``.
  2. Compares onnxruntime (CPU) with torch on a random input and prints the max abs difference.
  3. Optionally (``--image``) detects the largest face with YuNet, builds the 80x80 crop with the
     vendored ``CropImage`` (same as the source repo), runs torch and onnxruntime on it and prints
     both outputs, their difference and the fused result (sum of the two models, argmax, label 1 = live).

ONNX contract (both files):
  input  "input": float32 [N, 3, 80, 80], BGR channel order, values 0..255 (no /255, no mean/std)
  output "prob" : float32 [N, 3], softmax probabilities; index 1 = live face, 0 and 2 = spoof classes.

Run from the repo root with the bench venv:
  face/eval/bench/minifasnet/.venv/bin/python face/eval/bench/minifasnet/export_onnx.py \
      --image "case_prerekvizity/Case1_Package1/Первый пакет/Персонал/Ким Андрей Сергеевич/Ким_Андрей_Сергеевич_фото.jpg"
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import torch
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
FACE = HERE.parents[2]  # face/
sys.path.insert(0, str(HERE))

from vendor.MiniFASNet import MiniFASNetV1, MiniFASNetV1SE, MiniFASNetV2, MiniFASNetV2SE  # noqa: E402
from vendor.generate_patches import CropImage  # noqa: E402
from vendor.utility import get_kernel, parse_model_name  # noqa: E402

# Same mapping as src/anti_spoof_predict.py (MODEL_MAPPING).
MODEL_MAPPING = {
    "MiniFASNetV1": MiniFASNetV1,
    "MiniFASNetV2": MiniFASNetV2,
    "MiniFASNetV1SE": MiniFASNetV1SE,
    "MiniFASNetV2SE": MiniFASNetV2SE,
}
LIVE_INDEX = 1  # test.py: label == 1 -> "Real Face"
OPSET = 17


class WithSoftmax(torch.nn.Module):
    """Model + softmax over classes, as in AntiSpoofPredict.predict (F.softmax on the logits)."""

    def __init__(self, net: torch.nn.Module):
        super().__init__()
        self.net = net

    def forward(self, x):
        return F.softmax(self.net(x), dim=1)


def load_torch_model(pth: Path) -> tuple[torch.nn.Module, int, int, float | None]:
    """Build and load the network the same way as AntiSpoofPredict._load_model."""
    h_input, w_input, model_type, scale = parse_model_name(pth.name)
    kernel_size = get_kernel(h_input, w_input)
    net = MODEL_MAPPING[model_type](conv6_kernel=kernel_size)
    state_dict = torch.load(pth, map_location="cpu", weights_only=True)
    first = next(iter(state_dict))
    if first.find("module.") >= 0:  # weights saved from DataParallel
        state_dict = OrderedDict((k[7:], v) for k, v in state_dict.items())
    net.load_state_dict(state_dict)
    net.eval()
    return WithSoftmax(net).eval(), h_input, w_input, scale


def export(model: torch.nn.Module, h: int, w: int, out: Path) -> None:
    dummy = torch.zeros(1, 3, h, w, dtype=torch.float32)
    torch.onnx.export(
        model,
        (dummy,),
        str(out),
        dynamo=False,  # legacy TorchScript exporter: no onnxscript dependency
        opset_version=OPSET,
        input_names=["input"],
        output_names=["prob"],
        dynamic_axes={"input": {0: "N"}, "prob": {0: "N"}},
        do_constant_folding=True,
    )
    onnx.checker.check_model(onnx.load(str(out)))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_input(crop_bgr_uint8: np.ndarray) -> np.ndarray:
    """HWC uint8 BGR -> NCHW float32 0..255, exactly like the repo's ToTensor (no /255)."""
    return crop_bgr_uint8.transpose(2, 0, 1)[None].astype(np.float32)


def fmt(v: np.ndarray) -> str:
    return "[" + ", ".join(f"{float(x):.4f}" for x in v) + "]"


def load_bgr(path: Path) -> np.ndarray:
    from PIL import Image, ImageOps

    pil = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    return np.ascontiguousarray(np.asarray(pil)[:, :, ::-1])


def yunet_bbox(bgr: np.ndarray, yunet_path: Path) -> list[int] | None:
    """Largest YuNet face as [x, y, w, h] (ints), the bbox format CropImage expects."""
    import cv2

    h, w = bgr.shape[:2]
    det = cv2.FaceDetectorYN.create(str(yunet_path), "", (w, h), 0.6, 0.3, 5000)
    _, faces = det.detect(bgr)
    if faces is None or len(faces) == 0:
        return None
    best = max(faces, key=lambda f: f[2] * f[3])
    x, y, bw, bh = (int(round(v)) for v in best[:4])
    return [x, y, bw, bh]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models-dir", type=Path, default=FACE / "models" / "minifasnet")
    ap.add_argument("--image", type=Path, action="append", default=[],
                    help="real image(s) for the crop check; repeatable")
    ap.add_argument("--yunet", type=Path, default=FACE / "models" / "opencv" / "face_detection_yunet_2023mar.onnx")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    torch.set_num_threads(max(1, (os.cpu_count() or 1) // 2))
    pths = sorted(args.models_dir.glob("*.pth"))
    if not pths:
        print(f"no .pth in {args.models_dir}", file=sys.stderr)
        return 1

    print(f"torch {torch.__version__}, onnx {onnx.__version__}, onnxruntime {ort.__version__}, opset {OPSET}")
    loaded = []
    for pth in pths:
        model, h, w, scale = load_torch_model(pth)
        out = pth.with_suffix(".onnx")
        export(model, h, w, out)
        sess = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
        x = (torch.rand(16, 3, h, w) * 255.0).float()
        with torch.no_grad():
            ref = model(x).numpy()
        got = sess.run(["prob"], {"input": x.numpy()})[0]
        diff = float(np.abs(ref - got).max())
        same_argmax = int((ref.argmax(1) == got.argmax(1)).sum())
        print(f"{pth.name}: input {h}x{w}, crop scale {scale}, kernel {get_kernel(h, w)} -> {out.name} "
              f"({out.stat().st_size} B, sha256 {sha256(out)})")
        print(f"  random input N=16 (0..255): max|torch-ort| = {diff:.3e}, argmax agree {same_argmax}/16")
        loaded.append((pth.name, model, sess, h, w, scale))

    if args.image and not args.yunet.exists():
        print(f"skip image check: YuNet weights not found at {args.yunet}")
        return 0
    cropper = CropImage()
    for img_path in args.image:
        bgr = load_bgr(img_path)
        bbox = yunet_bbox(bgr, args.yunet)
        print(f"image {img_path.name}: {bgr.shape[1]}x{bgr.shape[0]}, YuNet bbox [x,y,w,h] = {bbox}")
        if bbox is None:
            continue
        fused_torch = np.zeros((1, 3))
        fused_ort = np.zeros((1, 3))
        for name, model, sess, h, w, scale in loaded:
            param = {"org_img": bgr, "bbox": bbox, "scale": scale, "out_w": w, "out_h": h, "crop": True}
            if scale is None:
                param["crop"] = False
            crop = cropper.crop(**param)
            inp = to_input(crop)
            with torch.no_grad():
                p_t = model(torch.from_numpy(inp)).numpy()
            p_o = sess.run(["prob"], {"input": inp})[0]
            fused_torch += p_t
            fused_ort += p_o
            print(f"  {name}: torch {fmt(p_t[0])} ort {fmt(p_o[0])} "
                  f"max|diff| {float(np.abs(p_t - p_o).max()):.3e}")
        label = int(np.argmax(fused_ort))
        value = float(fused_ort[0][label] / 2)  # same as test.py: prediction[label] / 2
        print(f"  fused (sum of {len(loaded)} models): ort {fmt(fused_ort[0])} -> label {label} "
              f"({'live' if label == LIVE_INDEX else 'spoof'}), score {value:.4f}; "
              f"live prob mean {float(fused_ort[0][LIVE_INDEX] / len(loaded)):.4f}; "
              f"max|torch-ort| {float(np.abs(fused_torch - fused_ort).max()):.3e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
