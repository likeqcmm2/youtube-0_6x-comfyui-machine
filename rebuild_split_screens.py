from pathlib import Path
import subprocess
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
res = Path("/root/Resources")
video = res / "production_output" / "videos"

with zipfile.ZipFile(res / "storyboard_elias_yoder.xlsx") as zf:
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
        v = cell.find("m:v", NS)
        inline = cell.find("m:is", NS)
        value = ""
        if typ == "s" and v is not None:
            value = shared[int(v.text)]
        elif typ == "inlineStr" and inline is not None:
            value = "".join(t.text or "" for t in inline.findall(".//m:t", NS))
        elif v is not None:
            value = v.text or ""
        cells[ref] = value.strip()

split_scenes = [
    scene for scene in range(1, 372)
    if cells.get(f"E{scene + 1}", "").strip() == "Avatar/Split-screen"
]

for scene in split_scenes:
    left = video / f"scene_{scene}_1.mp4"
    right = video / f"scene_{scene}_2.mp4"
    out = video / f"scene_{scene}.mp4"
    if not left.exists():
        raise FileNotFoundError(left)
    if not right.exists():
        raise FileNotFoundError(right)
    cmd = [
        "ffmpeg", "-nostdin", "-y",
        "-i", str(left),
        "-i", str(right),
        "-filter_complex",
        "[0:v]scale=960:1080:force_original_aspect_ratio=increase,crop=960:1080[left];"
        "[1:v]scale=960:1080:force_original_aspect_ratio=increase,crop=960:1080[right];"
        "[left][right]hstack=inputs=2[v]",
        "-map", "[v]",
        "-map", "0:a:0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-shortest",
        str(out),
    ]
    print("SPLIT", scene, flush=True)
    subprocess.run(cmd, check=True)

print("split_count", len(split_scenes))
