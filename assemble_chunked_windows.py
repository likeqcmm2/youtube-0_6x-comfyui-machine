#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def run(cmd, msg=None):
    print("+", " ".join(map(str, cmd)), flush=True)
    p = subprocess.run(cmd)
    if p.returncode != 0:
        raise RuntimeError(msg or f"Command failed: {cmd}")


def timecode_seconds(value: str) -> float:
    match = re.match(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$", value.strip())
    if not match:
        raise ValueError(f"Invalid timecode: {value}")
    frac = (match.group(4) or "0").ljust(3, "0")[:3]
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(frac) / 1000


def read_storyboard(path: Path, first: int, last: int):
    with zipfile.ZipFile(path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in item.findall(".//m:t", NS)))

        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        cells = {}
        for cell in root.findall(".//m:c", NS):
            ref = cell.attrib.get("r", "")
            typ = cell.attrib.get("t")
            value_node = cell.find("m:v", NS)
            inline_node = cell.find("m:is", NS)
            value = ""
            if typ == "s" and value_node is not None:
                value = shared[int(value_node.text)]
            elif typ == "inlineStr" and inline_node is not None:
                value = "".join(t.text or "" for t in inline_node.findall(".//m:t", NS))
            elif value_node is not None:
                value = value_node.text or ""
            cells[ref] = value.strip()

    scenes = []
    for scene in range(first, last + 1):
        row = scene + 1
        scenes.append({"scene": scene, "type": cells.get(f"E{row}", "").strip()})
    return scenes


def read_timestamps(path: Path, first: int, last: int):
    rows = path.read_text(encoding="utf-8-sig").splitlines()[1:]
    timestamps = {}
    for scene in range(first, last + 1):
        line = rows[scene - 1]
        match = re.search(
            r"(\d{1,2}:\d{1,2}:\d{1,2}(?:\.\d{1,3})?)\s+-\s+(\d{1,2}:\d{1,2}:\d{1,2}(?:\.\d{1,3})?)",
            line,
        )
        if not match:
            raise ValueError(f"Invalid timestamp for scene {scene}: {line}")
        timestamps[scene] = {
            "start": timecode_seconds(match.group(1)),
            "end": timecode_seconds(match.group(2)),
        }
    return timestamps


def shell_quote_for_concat(path: Path) -> str:
    return "file '" + str(path).replace("\\", "/").replace("'", "'\\''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resources", default=r"C:\Users\ezycloudx-admin\Desktop\Resources")
    ap.add_argument("--production-output", default=r"C:\Users\ezycloudx-admin\Desktop\Resources\production_output")
    ap.add_argument("--output", default=r"C:\Users\ezycloudx-admin\Desktop\Resources\production_output\final_video_motion_0_6x_youtube1080_corrected.mp4")
    ap.add_argument("--first-scene", type=int, default=1)
    ap.add_argument("--last-scene", type=int, default=435)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--motion-speed", type=float, default=0.6)
    ap.add_argument("--chunk-size", type=int, default=40)
    args = ap.parse_args()

    ffmpeg = os.environ.get("FFMPEG", "ffmpeg")
    resources = Path(args.resources)
    production = Path(args.production_output)
    video_dir = production / "videos"
    work_dir = production / "work" / "chunked-assemble"
    work_dir.mkdir(parents=True, exist_ok=True)
    output = Path(args.output)
    voice = resources / "voice_over.mp3"
    scenes = read_storyboard(resources / "storyboard_elias_yoder.xlsx", args.first_scene, args.last_scene)
    timestamps = read_timestamps(resources / "time_stamp.csv", args.first_scene, args.last_scene)

    prev = 0
    rows = []
    for sc in scenes:
        end_frame = int(timestamps[sc["scene"]]["end"] * args.fps + 0.5)
        frames = end_frame - prev
        if frames < 1:
            raise RuntimeError(f"Scene {sc['scene']} invalid frame count {frames}")
        prev = end_frame
        rows.append({**sc, "frames": frames})
    total_frames = sum(row["frames"] for row in rows)
    duration = total_frames / args.fps

    chunks = []
    for chunk_index, start in enumerate(range(0, len(rows), args.chunk_size), start=1):
        chunk = rows[start : start + args.chunk_size]
        chunk_out = work_dir / f"chunk_{chunk_index:03d}.mp4"
        chunks.append(chunk_out)
        if chunk_out.exists():
            print(f"SKIP CHUNK {chunk_index}", flush=True)
            continue

        inputs = []
        filters = []
        for idx, row in enumerate(chunk):
            scene = row["scene"]
            src = video_dir / f"scene_{scene}.mp4"
            if not src.exists():
                raise FileNotFoundError(src)
            inputs += ["-i", str(src)]
            chain = (
                f"[{idx}:v]"
                "scale=1920:1080:force_original_aspect_ratio=increase,"
                "crop=1920:1080,"
                "setpts=PTS-STARTPTS,"
            )
            if row["type"] == "Motion" and args.motion_speed != 1.0:
                chain += f"setpts=PTS/{args.motion_speed},"
            chain += (
                f"fps={args.fps},"
                "tpad=stop_mode=clone:stop=-1,"
                f"trim=end_frame={row['frames']},"
                f"setpts=N/({args.fps}*TB)[v{idx}]"
            )
            filters.append(chain)

        filters.append("".join(f"[v{i}]" for i in range(len(chunk))) + f"concat=n={len(chunk)}:v=1:a=0[outv]")
        filter_path = work_dir / f"chunk_{chunk_index:03d}_filter.txt"
        filter_path.write_text(";".join(filters), encoding="ascii")
        run(
            [
                ffmpeg,
                "-nostdin",
                "-y",
                *inputs,
                "-filter_complex_script",
                str(filter_path),
                "-map",
                "[outv]",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-b:v",
                "10M",
                "-maxrate",
                "12M",
                "-bufsize",
                "20M",
                "-r",
                str(args.fps),
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(chunk_out),
            ],
            f"Chunk assembly failed: {chunk_out}",
        )

    concat_path = work_dir / "chunks.txt"
    concat_path.write_text("\n".join(shell_quote_for_concat(path) for path in chunks) + "\n", encoding="utf-8")
    frame_csv = work_dir / "frame-counts.csv"
    with frame_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["scene", "type", "frames"])
        writer.writerows((row["scene"], row["type"], row["frames"]) for row in rows)

    run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-i",
            str(voice),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "384k",
            "-t",
            f"{duration:.6f}",
            "-movflags",
            "+faststart",
            str(output),
        ],
        "Final chunk concat failed",
    )
    print(f"Created {output} with {total_frames} frames ({duration:.3f}s)")


if __name__ == "__main__":
    main()
