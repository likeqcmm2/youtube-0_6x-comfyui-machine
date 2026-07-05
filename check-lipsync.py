#!/usr/bin/env python3
"""Detect LTX avatar clips whose mouth does not visibly open while audio plays."""

import argparse, csv, os, subprocess, tempfile, urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from scipy.io import wavfile

MOUTH_DARK_THRESHOLD = 60
MOUTH_STD_MIN = 1.0
MOUTH_P90_MIN = 13.0
AUDIO_SILENCE_DB = -45
AUDIO_MIN_SPEECH_RATIO = 0.15

MODEL_DIR = Path(__file__).resolve().parent / "models"
PROTO = MODEL_DIR / "deploy.prototxt"
WEIGHTS = MODEL_DIR / "res10_300x300_ssd_iter_140000.caffemodel"


def load_face_net():
    MODEL_DIR.mkdir(exist_ok=True)
    if not PROTO.exists():
        urllib.request.urlretrieve(
            "https://raw.githubusercontent.com/opencv/opencv/master/samples/dnn/face_detector/deploy.prototxt",
            PROTO,
        )
    if not WEIGHTS.exists():
        urllib.request.urlretrieve(
            "https://raw.githubusercontent.com/opencv/opencv_3rdparty/dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel",
            WEIGHTS,
        )
    return cv2.dnn.readNetFromCaffe(str(PROTO), str(WEIGHTS))


FACE_NET = load_face_net()


def detect_face(frame):
    h, w = frame.shape[:2]
    blob = cv2.dnn.blobFromImage(cv2.resize(frame, (300, 300)), 1.0, (300, 300), (104, 177, 123))
    FACE_NET.setInput(blob)
    detections = FACE_NET.forward()
    best, best_area = None, 0
    for i in range(detections.shape[2]):
        if detections[0, 0, i, 2] < 0.7:
            continue
        x1, y1, x2, y2 = (detections[0, 0, i, 3:7] * [w, h, w, h]).astype(int)
        area = (x2 - x1) * (y2 - y1)
        if area > best_area:
            best, best_area = (x1, y1, x2 - x1, y2 - y1), area
    return best


def analyze_audio(video):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(video), "-ac", "1", "-ar", "16000",
                        "-loglevel", "error", wav_path], check=True)
        rate, data = wavfile.read(wav_path)
        data = data.astype(np.float32) / 32768.0
        size = int(rate * 0.05)
        dbs = [20 * np.log10(np.sqrt(np.mean(data[i:i + size] ** 2)) + 1e-9)
               for i in range(0, len(data) - size, size)]
        ratio = sum(db > AUDIO_SILENCE_DB for db in dbs) / max(len(dbs), 1)
        return ratio >= AUDIO_MIN_SPEECH_RATIO, ratio
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)


def analyze_mouth(video):
    cap = cv2.VideoCapture(str(video))
    h, w = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    box = (int(w * .35), int(h * .62), int(w * .65), int(h * .88))
    values, face_found, frame_index = [], False, 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_index += 1
        if frame_index % 2:
            continue
        face = detect_face(frame)
        if face:
            x, y, fw, fh = face
            box = (x + int(fw * .25), y + int(fh * .65), x + int(fw * .75), y + int(fh * .88))
            face_found = True
        x1, y1, x2, y2 = box
        crop = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[y1:y2, x1:x2]
        if crop.size:
            values.append(np.mean(crop < MOUTH_DARK_THRESHOLD) * 100)
    cap.release()
    if not values:
        return False, face_found, 0.0, 0.0
    values = np.asarray(values)
    std, p90 = float(values.std()), float(np.percentile(values, 90))
    return std >= MOUTH_STD_MIN and p90 >= MOUTH_P90_MIN, face_found, std, p90


@dataclass
class Result:
    file: str
    status: str
    audio_ok: bool
    mouth_ok: bool
    face_detected: bool
    audio_speech_ratio: float
    mouth_std: float
    mouth_p90: float


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", required=True)
    parser.add_argument("--csv", default="lipsync_report.csv")
    parser.add_argument("--errors-txt", default="lipsync_errors.txt")
    args = parser.parse_args()
    folder = Path(args.folder)
    results = []
    for video in sorted(folder.glob("*.mp4")):
        audio_ok, ratio = analyze_audio(video)
        mouth_ok, face, std, p90 = analyze_mouth(video)
        status = "OK" if audio_ok and mouth_ok and face else "FAIL"
        results.append(Result(video.name, status, audio_ok, mouth_ok, face, round(ratio, 4),
                              round(std, 4), round(p90, 4)))
        print(f"{status}: {video.name}")
    with (folder / args.csv).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=Result.__annotations__.keys())
        writer.writeheader()
        writer.writerows(asdict(item) for item in results)
    failed = [item.file for item in results if item.status != "OK"]
    (folder / args.errors_txt).write_text("\n".join(failed), encoding="utf-8")
    print(f"Failed: {len(failed)}/{len(results)}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
