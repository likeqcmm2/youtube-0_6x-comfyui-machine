#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def run(cmd: list[str], failure: str) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    proc = subprocess.run(cmd)
    if proc.returncode:
        raise RuntimeError(failure)


def timecode_seconds(value: str) -> float:
    value = value.strip()
    match = re.match(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$", value)
    if match:
        frac = (match.group(4) or "0").ljust(3, "0")[:3]
        return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(frac) / 1000
    match = re.match(r"^(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$", value)
    if match:
        frac = (match.group(3) or "0").ljust(3, "0")[:3]
        return int(match.group(1)) * 60 + int(match.group(2)) + int(frac) / 1000
    raise ValueError(f"Invalid timecode: {value}")


def seconds_timecode(value: float) -> str:
    millis = int(round(value * 1000))
    hours, rem = divmod(millis, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def cleanup_appledouble_files(resources: Path) -> None:
    removed = 0
    for path in resources.rglob("._*"):
        if path.is_file():
            path.unlink()
            removed += 1
    if removed:
        print(f"Removed {removed} macOS AppleDouble resource file(s).", flush=True)


def find_avatar_image(resources: Path, explicit: str = "") -> Path:
    if explicit:
        path = Path(explicit)
        path = path if path.is_absolute() else resources / path
        if path.exists():
            return path
        raise FileNotFoundError(f"Avatar image not found: {path}")
    for name in ("avatar.png", "avatar.jpg", "avatar.jpeg", "Avatar.png", "Avatar.jpg", "Avatar.jpeg"):
        path = resources / name
        if path.exists():
            return path
    matches = sorted(
        path for path in resources.iterdir()
        if path.is_file() and path.stem.lower() == "avatar" and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
    )
    if matches:
        return matches[0]
    raise FileNotFoundError(f"No avatar image found in {resources}; expected avatar.png, avatar.jpg, or avatar.jpeg")


def read_ebook_promo_ranges(path: Path, final_duration: float) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"Ebook promo time file not found: {path}. "
            "Create it with lines like: 03:00.200 - 03:14.450"
        )
    ranges = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(
            r"^(\d{1,2}:\d{1,2}(?::\d{1,2})?(?:\.\d{1,3})?)\s*(?:-|,|to|->)\s*"
            r"(\d{1,2}:\d{1,2}(?::\d{1,2})?(?:\.\d{1,3})?)$",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError(f"Invalid ebook promo range at {path}:{line_no}: {raw_line}")
        start = timecode_seconds(match.group(1))
        end = timecode_seconds(match.group(2))
        if end <= start:
            raise ValueError(f"Ebook promo end must be after start at {path}:{line_no}: {raw_line}")
        if start >= final_duration:
            raise ValueError(
                f"Ebook promo start {seconds_timecode(start)} is outside final duration "
                f"{seconds_timecode(final_duration)} at {path}:{line_no}"
            )
        end = min(end, final_duration)
        ranges.append({"start": start, "end": end, "duration": end - start})
    ranges.sort(key=lambda item: item["start"])
    for prev, current in zip(ranges, ranges[1:]):
        if current["start"] < prev["end"]:
            raise ValueError(
                f"Ebook promo ranges overlap: {seconds_timecode(prev['start'])}-{seconds_timecode(prev['end'])} "
                f"and {seconds_timecode(current['start'])}-{seconds_timecode(current['end'])}"
            )
    if not ranges:
        raise ValueError(f"No ebook promo ranges found in {path}")
    return ranges


def find_storyboard(resources: Path, explicit: str = "") -> Path:
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else resources / path
    for name in ("storyboard.xlsx", "Storyboard.xlsx", "storyboard_elias_yoder.xlsx"):
        path = resources / name
        if path.exists() and not path.name.startswith("._"):
            return path
    matches = sorted(path for path in resources.glob("*.xlsx") if not path.name.startswith(("~$", "._")))
    if matches:
        return matches[0]
    raise FileNotFoundError(f"No storyboard XLSX found in {resources}")


def normalize_scene_type(value: str) -> str:
    mapping = {
        "still": "Still Image + Ken Burn",
        "still image": "Still Image + Ken Burn",
        "still image + ken burn": "Still Image + Ken Burn",
        "ken burn": "Still Image + Ken Burn",
        "split": "Avatar/Split-screen",
        "split screen": "Avatar/Split-screen",
        "avatar/split-screen": "Avatar/Split-screen",
        "avatar / split-screen": "Avatar/Split-screen",
        "avatar": "Avatar",
        "motion": "Motion",
    }
    key = " ".join((value or "").strip().lower().split())
    return mapping.get(key, (value or "").strip())


def read_storyboard(path: Path, first: int, last: int) -> list[dict]:
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

    def cell(ref: str) -> str:
        return cells.get(ref, "").strip()

    headers = {col: cell(f"{col}1") for col in ("A", "B", "C", "D", "E")}
    type_header = headers["D"].lower()
    new_format = (
        headers["A"].lower() == "timecode"
        and "prompt" in headers["C"].lower()
        and ("loại" in type_header or "loai" in type_header)
    )

    scenes = []
    for scene in range(first, last + 1):
        row = scene + 1
        if new_format:
            item = {
                "scene": scene,
                "timecode": cell(f"A{row}"),
                "audio_text": cell(f"B{row}"),
                "image_prompt": cell(f"C{row}"),
                "motion_prompt": cell(f"E{row}"),
                "type": normalize_scene_type(cell(f"D{row}")),
            }
        else:
            item = {
                "scene": scene,
                "image_prompt": cell(f"C{row}"),
                "motion_prompt": cell(f"D{row}"),
                "type": normalize_scene_type(cell(f"E{row}")),
            }
        scenes.append(item)
    return scenes


def read_timestamps(path: Path, first: int, last: int) -> dict[int, dict]:
    if not path.exists():
        raise FileNotFoundError(f"Timestamp CSV not found: {path}")
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


def post_json(url: str, payload: dict, timeout: int = 7200) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {text}") from exc


def call_comfy(wrapper_url: str, workflow: dict, request_id: str) -> str:
    result = post_json(f"{wrapper_url.rstrip('/')}/generate/sync", {
        "input": {
            "request_id": request_id,
            "workflow_json": workflow,
            "return_outputs_as_base64": False,
        }
    })
    if result.get("status") != "completed":
        raise RuntimeError(f"ComfyUI job failed: {json.dumps(result, indent=2)[:4000]}")
    outputs = result.get("output") or []
    if not outputs:
        raise RuntimeError(f"ComfyUI job completed without output: {json.dumps(result, indent=2)[:4000]}")
    local_path = outputs[0].get("local_path")
    if not local_path or not Path(local_path).exists():
        raise RuntimeError(f"ComfyUI output missing on disk: {outputs[0]}")
    return local_path


def load_workflow(template_dir: Path, name: str) -> dict:
    return json.loads((template_dir / name).read_text(encoding="utf-8"))


def normalize_workflow_paths(value):
    if isinstance(value, dict):
        return {key: normalize_workflow_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_workflow_paths(item) for item in value]
    if isinstance(value, str):
        return value.replace("\\", "/")
    return value


def copy_to_comfy_input(src: Path, input_dir: Path, name: str) -> Path:
    input_dir.mkdir(parents=True, exist_ok=True)
    dst = input_dir / name
    shutil.copy2(src, dst)
    return dst


def patch_ltx_workflow(workflow: dict, image_path: Path, prompt: str, request_id: str, frames: int, fps: int) -> dict:
    seconds, extra = divmod(frames, fps)
    workflow["381"]["inputs"]["image_paths"] = str(image_path)
    workflow["10:464"]["inputs"]["value"] = prompt
    workflow["10:365:139"]["inputs"]["value"] = seconds
    workflow["10:365:138"]["inputs"]["b"] = extra
    workflow["10:467"]["inputs"]["value"] = fps
    workflow["5"]["inputs"]["filename_prefix"] = f"storyboard/{request_id}"
    workflow["376"]["inputs"]["num_images"] = 1
    workflow["11:6:409"]["inputs"]["num_images"] = 1
    return workflow


def patch_infinite_workflow(workflow: dict, image_name: str, audio_name: str, prompt: str, request_id: str) -> dict:
    workflow["32"]["inputs"]["image"] = image_name
    workflow["171"]["inputs"]["audio"] = audio_name
    workflow["171"]["inputs"]["audiopreview"] = {
        "params": {"start_time": 0, "duration": 0, "filename": audio_name, "type": "input"}
    }
    workflow["14"]["inputs"]["text"] = prompt
    workflow["182"]["inputs"]["filename_prefix"] = f"storyboard/{request_id}"
    return workflow


def make_kenburn(ffmpeg: str, image: Path, destination: Path, duration: float = 5.0) -> None:
    run([
        ffmpeg, "-nostdin", "-y", "-loop", "1", "-i", str(image),
        "-vf", "scale=8000:-1,zoompan=z='zoom+0.001':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=150:s=1920x1080,format=yuv420p",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-t", f"{duration:.3f}", str(destination),
    ], f"Failed Ken Burn: {destination}")


def assemble_split(ffmpeg: str, left: Path, right: Path, destination: Path) -> None:
    run([
        ffmpeg, "-nostdin", "-y", "-i", str(left), "-i", str(right),
        "-filter_complex",
        "[0:v]scale=960:1080:force_original_aspect_ratio=increase,crop=960:1080[left];"
        "[1:v]scale=960:1080:force_original_aspect_ratio=increase,crop=960:1080[right];"
        "[left][right]hstack=inputs=2[v]",
        "-map", "[v]", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-shortest", str(destination),
    ], f"Failed split-screen: {destination}")


def apply_ebook_promo(
    ffmpeg: str,
    source_video: Path,
    ebook_video: Path,
    promo_ranges: list[dict],
    work_dir: Path,
    output_video: Path,
    fps: int,
    final_duration: float,
) -> None:
    if not ebook_video.exists():
        raise FileNotFoundError(f"Ebook promo video not found: {ebook_video}")

    promo_dir = work_dir / "ebook-promo-segments"
    if promo_dir.exists():
        shutil.rmtree(promo_dir)
    promo_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    cursor = 0.0

    def encode_source_part(start: float, end: float, index: int) -> None:
        if end <= start:
            return
        part = promo_dir / f"{index:04d}_source.mp4"
        if part.exists():
            parts.append(part)
            return
        run([
            ffmpeg, "-nostdin", "-y",
            "-ss", f"{start:.3f}",
            "-t", f"{end - start:.3f}",
            "-i", str(source_video),
            "-map", "0:v:0",
            "-vf", (
                "scale=1920:1080:force_original_aspect_ratio=increase,"
                f"crop=1920:1080,fps={fps},format=yuv420p"
            ),
            "-an",
            "-c:v", "libx264",
            "-preset", "medium",
            "-b:v", "10M",
            "-maxrate", "12M",
            "-bufsize", "20M",
            "-r", str(fps),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(part),
        ], f"Failed to prepare source promo part: {part}")
        parts.append(part)

    def encode_ebook_part(duration: float, index: int) -> None:
        part = promo_dir / f"{index:04d}_ebook.mp4"
        if part.exists():
            parts.append(part)
            return
        run([
            ffmpeg, "-nostdin", "-y",
            "-i", str(ebook_video),
            "-t", f"{duration:.3f}",
            "-vf", (
                "scale=1920:1080:force_original_aspect_ratio=increase,"
                f"crop=1920:1080,fps={fps},format=yuv420p"
            ),
            "-an",
            "-c:v", "libx264",
            "-preset", "medium",
            "-b:v", "10M",
            "-maxrate", "12M",
            "-bufsize", "20M",
            "-r", str(fps),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(part),
        ], f"Failed to prepare ebook promo part: {part}")
        parts.append(part)

    part_index = 0
    for promo in promo_ranges:
        encode_source_part(cursor, promo["start"], part_index)
        part_index += 1
        encode_ebook_part(promo["duration"], part_index)
        part_index += 1
        cursor = promo["end"]
    encode_source_part(cursor, final_duration, part_index)

    def concat_quote(path: Path) -> str:
        return "'" + str(path).replace("'", "'\\''") + "'"

    concat_list = promo_dir / "ebook-promo-concat.txt"
    concat_list.write_text("\n".join(f"file {concat_quote(part)}" for part in parts) + "\n", encoding="utf-8")
    run([
        ffmpeg, "-nostdin", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output_video),
    ], f"Failed to concat ebook promo video: {output_video}")


def assemble_youtube_final(
    ffmpeg: str,
    scenes: list[dict],
    timestamps: dict[int, dict],
    video_dir: Path,
    voice: Path,
    work_dir: Path,
    output: Path,
    fps: int,
    motion_speed: float,
    ebook_video: Path,
    ebook_promo_times: Path,
) -> None:
    frame_rows = ["scene,type,frames"]
    previous_end_frame = 0
    motion_count = 0
    segment_dir = work_dir / "assemble-motion-0.6x-youtube1080-segments"
    segment_dir.mkdir(parents=True, exist_ok=True)

    for idx, item in enumerate(scenes):
        scene = item["scene"]
        src = video_dir / f"scene_{scene}.mp4"
        if not src.exists():
            raise FileNotFoundError(src)

        end_frame = int(timestamps[scene]["end"] * fps + 0.5)
        frames = end_frame - previous_end_frame
        if frames < 1:
            raise RuntimeError(f"Scene {scene} invalid frame count {frames}")
        previous_end_frame = end_frame
        frame_rows.append(f"{scene},{item['type']},{frames}")

        segment = segment_dir / f"{idx:04d}_scene_{scene}.mp4"
        if segment.exists():
            continue

        vf = (
            "scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,"
            "setpts=PTS-STARTPTS,"
        )
        if item["type"] == "Motion" and motion_speed != 1.0:
            motion_count += 1
            vf += f"setpts=PTS/{motion_speed},"
        vf += (
            f"fps={fps},"
            "tpad=stop_mode=clone:stop=-1,"
            f"trim=end_frame={frames},"
            f"setpts=N/({fps}*TB),"
            "format=yuv420p"
        )
        run([
            ffmpeg, "-nostdin", "-y",
            "-i", str(src),
            "-vf", vf,
            "-an",
            "-c:v", "libx264",
            "-preset", "medium",
            "-b:v", "10M",
            "-maxrate", "12M",
            "-bufsize", "20M",
            "-r", str(fps),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(segment),
        ], f"Final segment assembly failed: {segment}")

    counts_path = work_dir / "assemble-motion-0.6x-youtube1080-frame-counts.csv"
    counts_path.write_text("\n".join(frame_rows) + "\n", encoding="utf-8")

    total_frames = previous_end_frame
    duration = total_frames / fps

    def concat_quote(path: Path) -> str:
        return "'" + str(path).replace("'", "'\\''") + "'"

    concat_list = work_dir / "assemble-motion-0.6x-youtube1080-concat.txt"
    concat_video = work_dir / "assemble-motion-0.6x-youtube1080-video-only.mp4"
    promo_video = work_dir / "assemble-motion-0.6x-youtube1080-ebook-promo-video-only.mp4"
    concat_list.write_text(
        "\n".join(f"file {concat_quote(segment_dir / f'{idx:04d}_scene_{item['scene']}.mp4')}" for idx, item in enumerate(scenes)) + "\n",
        encoding="utf-8",
    )
    run([
        ffmpeg, "-nostdin", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(concat_video),
    ], f"Final video concat failed: {concat_video}")

    promo_ranges = read_ebook_promo_ranges(ebook_promo_times, duration)
    print(
        "Applying ebook promo ranges: "
        + ", ".join(f"{seconds_timecode(item['start'])}-{seconds_timecode(item['end'])}" for item in promo_ranges),
        flush=True,
    )
    apply_ebook_promo(ffmpeg, concat_video, ebook_video, promo_ranges, work_dir, promo_video, fps, duration)

    run([
        ffmpeg, "-nostdin", "-y",
        "-i", str(promo_video),
        "-i", str(voice),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "384k",
        "-t", f"{duration:.6f}",
        "-movflags", "+faststart",
        str(output),
    ], f"Final YouTube assembly failed: {output}")
    print(
        f"Created {output} with {total_frames} frames ({duration:.3f}s), "
        f"{len(promo_ranges)} ebook promo range(s). Motion slowed: {motion_count}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", default="/root/Resources")
    parser.add_argument("--storyboard", default="")
    parser.add_argument("--output-dir", default="/root/Resources/production_output_comfy")
    parser.add_argument("--wrapper-url", default="http://127.0.0.1:18288")
    parser.add_argument("--template-dir", default="/opt/comfyui-api-wrapper/workflows")
    parser.add_argument("--comfy-input-dir", default="/workspace/ComfyUI/input")
    parser.add_argument("--first-scene", type=int, default=1)
    parser.add_argument("--last-scene", type=int, default=3)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--motion-frames", type=int, default=85)
    parser.add_argument("--motion-speed", type=float, default=0.6)
    parser.add_argument("--final-name", default="final_video_motion_0_6x_youtube1080_25fps_ebook_promo_10M.mp4")
    parser.add_argument("--avatar-image", default="")
    parser.add_argument("--ebook-video", default="New_Ebook.mov")
    parser.add_argument("--ebook-promo-times", default="ebook_promo_times.txt")
    parser.add_argument("--avatar-lead-in", type=float, default=0.0)
    parser.add_argument("--avatar-gain-db", type=float, default=0.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-assemble", action="store_true")
    args = parser.parse_args()

    ffmpeg = "ffmpeg"
    resources = Path(args.resources)
    cleanup_appledouble_files(resources)
    output = Path(args.output_dir)
    video_dir = output / "videos"
    audio_dir = output / "avatar-audio"
    ltx_audio_dir = output / "avatar-audio-ltx"
    raw_avatar_dir = output / "work" / "avatar-videos-with-leadin"
    for path in (output, video_dir, audio_dir, ltx_audio_dir, raw_avatar_dir):
        path.mkdir(parents=True, exist_ok=True)

    storyboard = find_storyboard(resources, args.storyboard)
    scenes = read_storyboard(storyboard, args.first_scene, args.last_scene)
    timestamps = read_timestamps(resources / "time_stamp.csv", args.first_scene, args.last_scene)
    avatar_prompt = (resources / "Prompt_for_avatar.txt").read_text(encoding="utf-8").strip()
    avatar_image = find_avatar_image(resources, args.avatar_image)
    ebook_video = Path(args.ebook_video)
    ebook_video = ebook_video if ebook_video.is_absolute() else resources / ebook_video
    ebook_promo_times = Path(args.ebook_promo_times)
    ebook_promo_times = ebook_promo_times if ebook_promo_times.is_absolute() else resources / ebook_promo_times
    comfy_input = Path(args.comfy_input_dir)
    templates = Path(args.template_dir)

    motion_scenes = [item for item in scenes if item["type"] == "Motion"]
    avatar_scenes = [item for item in scenes if item["type"] in {"Avatar", "Avatar/Split-screen"}]
    kenburn_scenes = [item for item in scenes if item["type"] in {"Still Image + Ken Burn", "Avatar/Split-screen"}]
    split_scenes = [item for item in scenes if item["type"] == "Avatar/Split-screen"]
    known_types = {"Motion", "Avatar", "Avatar/Split-screen", "Still Image + Ken Burn"}
    unknown = [item for item in scenes if item["type"] not in known_types]
    if unknown:
        scene = unknown[0]["scene"]
        raise RuntimeError(f"Unsupported scene type for scene_{scene}: {unknown[0]['type']!r}")

    print(f"PHASE motion: {len(motion_scenes)} scene(s)", flush=True)
    for item in motion_scenes:
        scene = item["scene"]
        destination = video_dir / f"scene_{scene}.mp4"
        if destination.exists() and not args.force:
            print(f"SKIP MOTION scene_{scene}", flush=True)
            continue

        print(f"MOTION scene_{scene}", flush=True)
        src_image = resources / "output_scenes" / f"scene_{scene}.png"
        input_image = copy_to_comfy_input(src_image, comfy_input, f"storyboard_scene_{scene}.png")
        request_id = f"storyboard-motion-scene-{scene}-{int(time.time())}"
        frames = max(1, args.motion_frames)
        workflow = patch_ltx_workflow(
            load_workflow(templates, "LTX_I2V_FFLF_85frames_input_enabled.json"),
            input_image,
            item["motion_prompt"] or item["image_prompt"],
            request_id,
            frames,
            args.fps,
        )
        workflow = normalize_workflow_paths(workflow)
        out_path = call_comfy(args.wrapper_url, workflow, request_id)
        shutil.copy2(out_path, destination)
        run([ffmpeg, "-nostdin", "-y", "-i", str(destination), "-map", "0:v:0", "-c:v", "copy", f"{destination}.silent.mp4"],
            f"Failed to remove Motion audio: scene_{scene}")
        Path(f"{destination}.silent.mp4").replace(destination)

    print(f"PHASE avatar: {len(avatar_scenes)} scene(s)", flush=True)
    for item in avatar_scenes:
        scene = item["scene"]
        scene_type = item["type"]
        avatar_dest = video_dir / (f"scene_{scene}_1.mp4" if scene_type == "Avatar/Split-screen" else f"scene_{scene}.mp4")
        if avatar_dest.exists() and not args.force:
            print(f"SKIP AVATAR scene_{scene} [{scene_type}]", flush=True)
            continue

        print(f"AVATAR scene_{scene} [{scene_type}]", flush=True)
        timestamp = timestamps[scene]
        audio = audio_dir / f"scene_{scene}.mp3"
        if args.force or not audio.exists():
            run([
                ffmpeg, "-nostdin", "-y", "-ss", f"{timestamp['start']:.3f}", "-t", f"{timestamp['duration']:.3f}",
                "-i", str(resources / "voice_over.mp3"), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(audio),
            ], f"Failed to cut avatar audio: scene_{scene}")
        ltx_audio = ltx_audio_dir / f"scene_{scene}.mp3"
        if args.force or not ltx_audio.exists():
            shutil.copy2(audio, ltx_audio)

        image_input = copy_to_comfy_input(avatar_image, comfy_input, "storyboard_avatar.png")
        audio_input = copy_to_comfy_input(ltx_audio, comfy_input, f"storyboard_scene_{scene}_avatar.mp3")
        request_id = f"storyboard-avatar-scene-{scene}-{int(time.time())}"
        workflow = patch_infinite_workflow(
            load_workflow(templates, "Wan InfiniteTalk - Duration 4, 5, 6 Seconds.json"),
            image_input.name,
            audio_input.name,
            avatar_prompt or "the character is talking",
            request_id,
        )
        workflow = normalize_workflow_paths(workflow)
        out_path = call_comfy(args.wrapper_url, workflow, request_id)
        raw = raw_avatar_dir / (f"scene_{scene}_1.mp4" if scene_type == "Avatar/Split-screen" else f"scene_{scene}.mp4")
        shutil.copy2(out_path, raw)
        run([
            ffmpeg, "-nostdin", "-y", "-i", str(raw), "-ss", "0.000", "-t", f"{timestamp['duration']:.3f}",
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(avatar_dest),
        ], f"Failed to trim avatar lead-in: scene_{scene}")

    print(f"PHASE kenburn: {len(kenburn_scenes)} scene(s)", flush=True)
    for item in kenburn_scenes:
        scene = item["scene"]
        scene_type = item["type"]
        destination = video_dir / (f"scene_{scene}_2.mp4" if scene_type == "Avatar/Split-screen" else f"scene_{scene}.mp4")
        if destination.exists() and not args.force:
            print(f"SKIP KENBURN scene_{scene} [{scene_type}]", flush=True)
            continue

        print(f"KENBURN scene_{scene} [{scene_type}]", flush=True)
        make_kenburn(ffmpeg, resources / "output_scenes" / f"scene_{scene}.png", destination)

    print(f"PHASE split: {len(split_scenes)} scene(s)", flush=True)
    for item in split_scenes:
        scene = item["scene"]
        destination = video_dir / f"scene_{scene}.mp4"
        if destination.exists() and not args.force:
            print(f"SKIP SPLIT scene_{scene}", flush=True)
            continue

        print(f"SPLIT scene_{scene}", flush=True)
        left = video_dir / f"scene_{scene}_1.mp4"
        right = video_dir / f"scene_{scene}_2.mp4"
        if not left.exists():
            raise FileNotFoundError(left)
        if not right.exists():
            raise FileNotFoundError(right)
        assemble_split(ffmpeg, left, right, destination)

    print(f"Completed scenes {args.first_scene}-{args.last_scene}: {video_dir}")
    if not args.skip_assemble:
        final_path = output / args.final_name
        print(f"ASSEMBLE youtube final: {final_path}", flush=True)
        assemble_youtube_final(
            ffmpeg,
            scenes,
            timestamps,
            video_dir,
            resources / "voice_over.mp3",
            output / "work",
            final_path,
            args.fps,
            args.motion_speed,
            ebook_video,
            ebook_promo_times,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
