#!/usr/bin/env python3
"""Mask flags in a video: Grounding DINO (detect) + SAM 2 (segment) + ffmpeg (encode).

Usage:
    python mask_flags.py test_clip.mp4 debug.mp4 --mode debug --max-frames 300
    python mask_flags.py test_clip.mp4 masked.mp4 --mode blur
"""
import os

# Some ops are not implemented on MPS yet; let them fall back to CPU.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import argparse
import subprocess
import time
from collections import deque

import cv2
import numpy as np
import torch
from PIL import Image
from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
from sam2.sam2_image_predictor import SAM2ImagePredictor


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--prompt", default=(
        "a Chinese national flag flying from a flagpole. "
        "a Chinese flag badge on clothing."
    ),
                    help="lowercase, ending with a period (Grounding DINO convention)")
    ap.add_argument("--box-thr", type=float, default=0.10)
    ap.add_argument("--text-thr", type=float, default=0.10)
    ap.add_argument("--min-aspect", type=float, default=1.0,
                    help="minimum box width/height for a flag")
    ap.add_argument("--min-red-ratio", type=float, default=0.45,
                    help="minimum fraction of red pixels inside a flag box")
    ap.add_argument("--min-yellow-ratio", type=float, default=0.001,
                    help="minimum fraction of yellow pixels inside a flag box")
    ap.add_argument("--min-area-ratio", type=float, default=0.001,
                    help="minimum flag-box fraction of the detection frame")
    ap.add_argument("--det-width", type=int, default=960,
                    help="width used for detection/segmentation; mask is upscaled afterwards")
    ap.add_argument("--hold", type=int, default=3,
                    help="keep masks from the last N frames to avoid flicker")
    ap.add_argument("--dilate", type=int, default=15, help="mask dilation in pixels (full-res)")
    ap.add_argument("--max-boxes", type=int, default=5)
    ap.add_argument("--mode", choices=["blur", "pixelate", "debug"], default="blur")
    ap.add_argument("--max-frames", type=int, default=0, help="0 = whole video")
    ap.add_argument("--dino-id", default="IDEA-Research/grounding-dino-tiny")
    ap.add_argument("--sam-id", default="facebook/sam2.1-hiera-small")
    ap.add_argument("--device", default="auto", choices=["auto", "mps", "cpu"])
    return ap.parse_args()


def main():
    args = parse_args()
    device = args.device
    if device == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"device: {device}")

    processor = AutoProcessor.from_pretrained(args.dino_id)
    dino = AutoModelForZeroShotObjectDetection.from_pretrained(args.dino_id).to(device).eval()
    sam = SAM2ImagePredictor.from_pretrained(args.sam_id, device=device)

    cap = cv2.VideoCapture(args.input)
    fps = cap.get(cv2.CAP_PROP_FPS)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if args.max_frames:
        total = min(total, args.max_frames)
    fps_arg = "30000/1001" if abs(fps - 29.97) < 0.01 else f"{fps}"

    sw = args.det_width
    sh = int(round(h * sw / w / 2) * 2)
    scale_x, scale_y = w / sw, h / sh
    print(f"video {w}x{h} @ {fps:.3f} fps, {total} frames; detection size {sw}x{sh}")

    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", fps_arg, "-i", "pipe:0",
         "-i", args.input,
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "libx264", "-crf", "16", "-preset", "fast", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-shortest", args.output],
        stdin=subprocess.PIPE,
    )

    kernel = None
    if args.dilate > 0:
        k = args.dilate * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))

    history = deque(maxlen=args.hold + 1)
    n = 0
    t0 = time.time()

    @torch.inference_mode()
    def detect(rgb_small):
        pil = Image.fromarray(rgb_small)
        inputs = processor(images=pil, text=args.prompt, return_tensors="pt").to(device)
        out = dino(**inputs)
        kwargs = dict(text_threshold=args.text_thr, target_sizes=[(sh, sw)])
        try:
            res = processor.post_process_grounded_object_detection(
                out, inputs.input_ids, threshold=args.box_thr, **kwargs)[0]
        except TypeError:  # older transformers versions
            res = processor.post_process_grounded_object_detection(
                out, inputs.input_ids, box_threshold=args.box_thr, **kwargs)[0]
        boxes = res["boxes"].cpu().numpy()
        scores = res["scores"].cpu().numpy()
        hsv = cv2.cvtColor(rgb_small, cv2.COLOR_RGB2HSV)
        red = (((hsv[..., 0] <= 10) | (hsv[..., 0] >= 170)) &
               (hsv[..., 1] >= 80) & (hsv[..., 2] >= 50))
        yellow = ((hsv[..., 0] >= 15) & (hsv[..., 0] <= 40) &
                  (hsv[..., 1] >= 80) & (hsv[..., 2] >= 80))
        selected = []
        for index in np.argsort(-scores):
            x0 = max(0, int(np.floor(boxes[index, 0])))
            y0 = max(0, int(np.floor(boxes[index, 1])))
            x1 = min(sw, int(np.ceil(boxes[index, 2])))
            y1 = min(sh, int(np.ceil(boxes[index, 3])))
            box_width, box_height = x1 - x0, y1 - y0
            if box_width <= 0 or box_height <= 0:
                continue
            area = box_width * box_height
            if box_width / box_height < args.min_aspect:
                continue
            if area / (sw * sh) < args.min_area_ratio:
                continue
            if np.mean(red[y0:y1, x0:x1]) < args.min_red_ratio:
                continue
            if np.mean(yellow[y0:y1, x0:x1]) < args.min_yellow_ratio:
                continue
            selected.append(index)
            if len(selected) == args.max_boxes:
                break
        order = np.asarray(selected, dtype=int)
        return boxes[order], scores[order]

    @torch.inference_mode()
    def segment(rgb_small, boxes):
        sam.set_image(rgb_small)
        union = np.zeros((sh, sw), dtype=np.uint8)
        for box in boxes:
            masks, _, _ = sam.predict(box=box, multimask_output=False)
            union |= masks[0].astype(np.uint8)
        return union

    while True:
        ok, frame = cap.read()
        if not ok or (args.max_frames and n >= args.max_frames):
            break

        small_bgr = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
        small_rgb = cv2.cvtColor(small_bgr, cv2.COLOR_BGR2RGB)

        boxes, scores = detect(small_rgb)
        if len(boxes):
            mask_small = segment(small_rgb, boxes)
        else:
            mask_small = np.zeros((sh, sw), dtype=np.uint8)
        history.append(mask_small)

        mask = np.maximum.reduce(list(history))
        mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
        if kernel is not None:
            mask = cv2.dilate(mask, kernel)
        soft = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 4)[..., None]

        if args.mode == "blur":
            repl = cv2.GaussianBlur(frame, (0, 0), 25)
        elif args.mode == "pixelate":
            tiny = cv2.resize(frame, (max(w // 32, 1), max(h // 32, 1)), interpolation=cv2.INTER_AREA)
            repl = cv2.resize(tiny, (w, h), interpolation=cv2.INTER_NEAREST)
        else:  # debug: green tint + boxes with scores
            repl = frame.copy()
            repl[..., 1] = np.clip(repl[..., 1].astype(np.int16) + 120, 0, 255).astype(np.uint8)

        out = (frame * (1 - soft) + repl * soft).astype(np.uint8)

        if args.mode == "debug":
            for (x0, y0, x1, y1), s in zip(boxes, scores):
                p0 = (int(x0 * scale_x), int(y0 * scale_y))
                p1 = (int(x1 * scale_x), int(y1 * scale_y))
                cv2.rectangle(out, p0, p1, (0, 255, 255), 3)
                cv2.putText(out, f"{s:.2f}", (p0[0], max(p0[1] - 8, 20)),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)

        ff.stdin.write(out.tobytes())
        n += 1
        if n % 30 == 0:
            rate = n / (time.time() - t0)
            eta = (total - n) / rate if rate > 0 else 0
            print(f"{n}/{total} frames  {rate:.2f} fps  ETA {eta/60:.1f} min", flush=True)

    cap.release()
    ff.stdin.close()
    ff.wait()
    print(f"done: {args.output}")


if __name__ == "__main__":
    main()