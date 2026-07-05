from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
res = Path("/root/Resources")
out = res / "production_output"
video = out / "videos"

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

avatar = []
split = []
for scene in range(1, 372):
    scene_type = cells.get(f"E{scene + 1}", "").strip()
    if scene_type == "Avatar":
        avatar.append(scene)
    elif scene_type == "Avatar/Split-screen":
        split.append(scene)

(res / "Prompt_for_avatar.txt").write_text(
    "the man speaking directly to the camera, stable camera.\n",
    encoding="utf-8",
)

paths = []
for scene in avatar:
    paths.extend(
        [
            video / f"scene_{scene}.mp4",
            out / "avatar-videos" / f"scene_{scene}.mp4",
            out / "avatar-audio" / f"scene_{scene}.mp3",
        ]
    )
for scene in split:
    paths.extend(
        [
            video / f"scene_{scene}.mp4",
            video / f"scene_{scene}_1.mp4",
            out / "avatar-videos" / f"scene_{scene}_1.mp4",
            out / "avatar-audio" / f"scene_{scene}.mp3",
        ]
    )

for name in (
    "final_video.mp4",
    "final_video_motion_0_6x_youtube1080.mp4",
    "final_video_motion_0_6x_youtube1080_corrected.mp4",
):
    paths.append(out / name)

removed = []
for path in paths:
    if path.exists():
        path.unlink()
        removed.append(str(path))

print("avatar_count", len(avatar))
print("split_count", len(split))
print("removed", len(removed))
print("avatar_scenes", ",".join(map(str, avatar)))
print("split_scenes", ",".join(map(str, split)))
