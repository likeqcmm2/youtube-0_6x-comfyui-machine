#!/usr/bin/env python3
"""Validate storyboard resources before starting a long LTX production run."""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
IMAGE_TYPES = {"Motion", "Still Image + Ken Burn", "Avatar/Split-screen"}
VALID_TYPES = IMAGE_TYPES | {"Avatar", "Persona Story"}


def read_storyboard(path: Path):
    with zipfile.ZipFile(path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in item.findall(".//m:t", NS)))
        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        cells = {}
        max_row = 0
        for cell in root.findall(".//m:c", NS):
            ref = cell.attrib.get("r", "")
            match = re.match(r"([A-Z]+)(\d+)", ref)
            if match:
                max_row = max(max_row, int(match.group(2)))
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
    for row in range(2, max_row + 1):
        scene = row - 1
        image_prompt = cells.get(f"C{row}", "")
        motion_prompt = cells.get(f"D{row}", "")
        scene_type = cells.get(f"E{row}", "")
        if image_prompt or motion_prompt or scene_type:
            scenes.append({"scene": scene, "type": scene_type})
    return scenes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", default="/root/Resources")
    parser.add_argument("--first-scene", type=int, default=1)
    parser.add_argument("--last-scene", type=int, default=None)
    args = parser.parse_args()

    resources = Path(args.resources)
    required = [
        resources / "storyboard_elias_yoder.xlsx",
        resources / "time_stamp.csv",
        resources / "voice_over.mp3",
        resources / "avatar.png",
        resources / "Prompt_for_avatar.txt",
        resources / "output_scenes",
    ]
    missing_paths = [path for path in required if not path.exists()]
    if missing_paths:
        print("Missing required paths:")
        for path in missing_paths:
            print(f"  {path}")
        return 1

    scenes = read_storyboard(resources / "storyboard_elias_yoder.xlsx")
    if args.last_scene is not None:
        scenes = [scene for scene in scenes if args.first_scene <= scene["scene"] <= args.last_scene]

    timestamp_rows = (resources / "time_stamp.csv").read_text(encoding="utf-8-sig").splitlines()[1:]
    bad_timestamps = []
    for scene in scenes:
        index = scene["scene"] - 1
        if index >= len(timestamp_rows):
            bad_timestamps.append((scene["scene"], "<missing row>"))
            continue
        line = timestamp_rows[index]
        if not re.search(r"\d{1,2}:\d{1,2}:\d{1,2}(?:\.\d{1,3})?\s+-\s+\d{1,2}:\d{1,2}:\d{1,2}(?:\.\d{1,3})?", line):
            bad_timestamps.append((scene["scene"], line[:120]))

    image_dir = resources / "output_scenes"
    existing = {
        int(match.group(1))
        for path in image_dir.iterdir()
        if (match := re.fullmatch(r"scene_(\d+)\.png", path.name, re.IGNORECASE))
    }
    required_images = [scene["scene"] for scene in scenes if scene["type"] in IMAGE_TYPES]
    missing_images = [scene for scene in required_images if scene not in existing]
    unknown_types = [scene for scene in scenes if scene["type"] and scene["type"] not in VALID_TYPES]
    blank_types = [scene["scene"] for scene in scenes if not scene["type"]]

    print(f"Scenes: {len(scenes)}")
    print(f"Type counts: {dict(Counter(scene['type'] for scene in scenes))}")
    print(f"Timestamp rows: {len(timestamp_rows)}")
    print(f"Required scene images: {len(required_images)}")
    print(f"Existing scene PNGs: {len(existing)}")

    failed = False
    if missing_images:
        failed = True
        print(f"Missing required images ({len(missing_images)}): {missing_images}")
    if bad_timestamps:
        failed = True
        print(f"Bad timestamp rows ({len(bad_timestamps)}): {bad_timestamps[:50]}")
    if unknown_types:
        failed = True
        print(f"Unknown scene types ({len(unknown_types)}): {unknown_types[:50]}")
    if blank_types:
        failed = True
        print(f"Blank scene types ({len(blank_types)}): {blank_types[:50]}")

    if failed:
        return 1
    print("Resource validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
