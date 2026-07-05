#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET
import subprocess

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def timecode_seconds(value: str) -> float:
    match = re.match(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$", value.strip())
    if not match:
        raise ValueError(f"Invalid timecode: {value}")
    frac = (match.group(4) or "0").ljust(3, "0")[:3]
    return (
        int(match.group(1)) * 3600
        + int(match.group(2)) * 60
        + int(match.group(3))
        + int(frac) / 1000
    )


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
        start = timecode_seconds(match.group(1))
        end = timecode_seconds(match.group(2))
        timestamps[scene] = {"start": start, "end": end, "duration": max(0.001, end - start)}
    return timestamps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", default="/root/Resources")
    parser.add_argument("--production-output", default="/root/Resources/production_output")
    parser.add_argument("--output", default="/root/Resources/production_output/final_video_motion_0_6x_youtube1080.mp4")
    parser.add_argument("--first-scene", type=int, default=1)
    parser.add_argument("--last-scene", type=int, default=371)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--motion-speed", type=float, default=0.6)
    args = parser.parse_args()

    resources = Path(args.resources)
    production = Path(args.production_output)
    video_dir = production / "videos"
    work_dir = production / "work"
    voice = resources / "voice_over.mp3"
    output = Path(args.output)
    fps = args.fps

    scenes = read_storyboard(resources / "storyboard_elias_yoder.xlsx", args.first_scene, args.last_scene)
    timestamps = read_timestamps(resources / "time_stamp.csv", args.first_scene, args.last_scene)

    inputs = []
    filters = []
    frame_counts = []
    prev_end_frame = 0
    motion_count = 0

    for idx, scene_info in enumerate(scenes):
        scene = scene_info["scene"]
        video_path = video_dir / f"scene_{scene}.mp4"
        if not video_path.exists():
            raise FileNotFoundError(video_path)

        end_frame = int(timestamps[scene]["end"] * fps + 0.5)
        frames = end_frame - prev_end_frame
        if frames < 1:
            raise RuntimeError(f"Scene {scene} invalid frame count {frames}")
        prev_end_frame = end_frame
        frame_counts.append((scene, scene_info["type"], frames))
        inputs += ["-i", str(video_path)]

        chain = (
            f"[{idx}:v]"
            "scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,"
            "setpts=PTS-STARTPTS,"
        )
        if scene_info["type"] == "Motion":
            motion_count += 1
            chain += f"setpts=PTS/{args.motion_speed},"
        chain += (
            f"fps={fps},"
            "tpad=stop_mode=clone:stop=-1,"
            f"trim=end_frame={frames},"
            f"setpts=N/({fps}*TB)[v{idx}]"
        )
        filters.append(chain)

    filters.append("".join(f"[v{i}]" for i in range(len(scenes))) + f"concat=n={len(scenes)}:v=1:a=0[outv]")

    work_dir.mkdir(parents=True, exist_ok=True)
    filter_path = work_dir / "assemble-motion-0.6x-youtube1080-filter.txt"
    counts_path = work_dir / "assemble-motion-0.6x-youtube1080-frame-counts.csv"
    filter_path.write_text(";".join(filters), encoding="ascii")
    with counts_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["scene", "type", "frames"])
        writer.writerows(frame_counts)

    total_frames = sum(frames for _, _, frames in frame_counts)
    duration = total_frames / fps
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        *inputs,
        "-i",
        str(voice),
        "-filter_complex_script",
        str(filter_path),
        "-map",
        "[outv]",
        "-map",
        f"{len(scenes)}:a:0",
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
        str(fps),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "384k",
        "-t",
        f"{duration:.6f}",
        "-movflags",
        "+faststart",
        str(output),
    ]

    print(f"Motion scenes slowed: {motion_count}")
    print(f"Output duration: {duration:.3f}s, frames: {total_frames}")
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
