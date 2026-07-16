# YouTube 0.6x ComfyUI Production Machine

This repo is the reusable Vast.ai machine setup for daily storyboard video production.

It installs and drives the current ComfyUI pipeline that replaces the old LTX Desktop workflow:

- **Avatar** scenes are generated with the packaged **Wan InfiniteTalk ComfyUI workflow**.
- **Motion** scenes are generated with the packaged **LTX I2V FFLF ComfyUI workflow**.
- **Still Image + Ken Burn** scenes are generated with FFmpeg.
- **Avatar/Split-screen** scenes render avatar on the left, Ken Burn visual on the right, then combine them.
- The final YouTube export is:

```text
/root/Resources/production_output_comfy/final_video_motion_0_6x_youtube1080_25fps_ebook_promo_10M.mp4
```

Motion scenes are slowed to **0.6x** during final assembly. Timing still follows the hard `time_stamp.csv` file.

## What Is Included

```text
installer/vast_comfyui_infinite_ltx_installer.sh
workflows/
  LTX_I2V_FFLF_85frames_input_enabled.json
  Wan InfiniteTalk - Duration 4, 5, 6 Seconds.json
ltx_comfyui_workflow.py
setup_vast_machine.sh
```

The repo intentionally does **not** include daily production assets. Upload those separately as `/root/Resources`.

## Required Daily Resources

Each day, upload your current `Resources` folder to the Vast server:

```text
/root/Resources/
  storyboard.xlsx                         # or any .xlsx storyboard file
  time_stamp.csv                          # required; timestamps always come from this file
  voice_over.mp3
  avatar.png                              # avatar.jpg and avatar.jpeg are also supported
  Prompt_for_avatar.txt
  New_Ebook.mov
  ebook_promo_times.txt
  output_scenes/
    scene_1.png
    scene_2.png
    scene_3.png
    ...
```

The storyboard Excel can use the new format:

| Column | Meaning |
|---|---|
| A | Timecode, ignored for production timing |
| B | Audio text/reference |
| C | Visual prompt |
| D | Scene type |
| E | Motion prompt |

Supported scene types:

- `Motion`
- `Avatar`
- `Still Image`
- `Still Image + Ken Burn`
- `Split screen`
- `Avatar/Split-screen`

The workflow removes macOS AppleDouble files such as `._Storyboard.xlsx` and `._avatar.jpeg` before reading resources, so archives created on macOS are safe to use.

## Ebook Promo Times

The final export always includes Ebook promo replacements. Put the daily replacement ranges in:

```text
/root/Resources/ebook_promo_times.txt
```

Each non-empty line is one replacement range:

```text
03:00.200 - 03:14.450
17:53.550 - 18:05.800
```

Accepted time formats are `HH:MM:SS.mmm` and `MM:SS.mmm`. For each range, the script takes the same duration from the beginning of `/root/Resources/New_Ebook.mov`, scales/crops it to 1920x1080 at 25fps, replaces only the video in that final timeline range, and keeps the original `voice_over.mp3` audio.

## New Vast Server Setup

SSH into the new Vast instance, then run:

```bash
cd /workspace
git clone REPLACE_WITH_THIS_REPO_URL youtube-0_6x-comfyui-machine
cd youtube-0_6x-comfyui-machine
bash setup_vast_machine.sh
```

The setup script:

1. Runs `installer/vast_comfyui_infinite_ltx_installer.sh`.
2. Installs ComfyUI, custom nodes, API wrapper, models, and SageAttention settings.
3. Copies the packaged workflows into:

```text
/opt/comfyui-api-wrapper/workflows/
```

The installer uses `hf download` with `hf-xet` and `HF_XET_HIGH_PERFORMANCE=1` for Hugging Face model downloads.

## Upload Resources

From your Mac, upload the daily Resources folder:

```bash
rsync -az --progress -e 'ssh -p YOUR_VAST_PORT' \
  /Users/truongdonghai/Desktop/Resources/ \
  root@YOUR_VAST_HOST:/root/Resources/
```

Example direct Vast SSH shape:

```bash
ssh -p YOUR_PORT root@YOUR_VAST_HOST -L 8080:localhost:18188
```

## Verify The Machine

On the Vast server:

```bash
tail -220 /var/log/portal/comfyui.log | grep -i -E "Using sage attention 3|error|missing|failed"
curl -s http://127.0.0.1:18288/health || true
ls -lh /opt/comfyui-api-wrapper/workflows/
```

Expected ComfyUI log signal:

```text
Using sage attention 3
```

## Run A Small Test

Render the first few scenes:

```bash
cd /workspace/youtube-0_6x-comfyui-machine
python3 ltx_comfyui_workflow.py \
  --resources /root/Resources \
  --first-scene 1 \
  --last-scene 3 \
  --force \
  --skip-assemble
```

Output clips are written to:

```text
/root/Resources/production_output_comfy/videos/
```

## Run Full Production

Set `--last-scene` to the final scene number in that day's storyboard:

```bash
cd /workspace/youtube-0_6x-comfyui-machine
python3 ltx_comfyui_workflow.py \
  --resources /root/Resources \
  --first-scene 1 \
  --last-scene YOUR_LAST_SCENE
```

Use `--force` only when you want to overwrite existing rendered scene clips:

```bash
python3 ltx_comfyui_workflow.py \
  --resources /root/Resources \
  --first-scene 1 \
  --last-scene YOUR_LAST_SCENE \
  --force
```

## Pipeline Details

### Motion

- Source image:

```text
/root/Resources/output_scenes/scene_N.png
```

- Prompt:
  - Uses column E `Motion Prompt`.
  - Falls back to visual prompt when motion prompt is empty.
- ComfyUI workflow:

```text
workflows/LTX_I2V_FFLF_85frames_input_enabled.json
```

- Default frame count:

```text
85 frames at 25fps
```

This is approximately 3.5 seconds before final timestamp trimming.

### Avatar

- Source avatar:

```text
/root/Resources/avatar.png
```

`avatar.jpg` and `avatar.jpeg` are also accepted automatically.

- Source audio:

```text
/root/Resources/voice_over.mp3
```

- Per-scene audio is cut from `voice_over.mp3` using `time_stamp.csv`.
- No 1 second lead-in is added.
- No 20dB audio boost is applied.
- ComfyUI workflow:

```text
workflows/Wan InfiniteTalk - Duration 4, 5, 6 Seconds.json
```

- Raw InfiniteTalk output is landscape 16:9 720p.
- The script scales/crops the avatar result to YouTube 1080p.

### Avatar/Split-screen

For split scenes:

```text
scene_N_1.mp4 = avatar left side
scene_N_2.mp4 = Ken Burn right side from output_scenes/scene_N.png
scene_N.mp4   = final split-screen scene
```

The combined split-screen keeps avatar audio.

### Final YouTube 0.6x

Final assembly:

- Converts scene video to 1920x1080.
- Uses 25fps frame-accurate boundaries.
- Slows only `Motion` scenes to 0.6x.
- Replaces Ebook promo ranges from `ebook_promo_times.txt` with `New_Ebook.mov`.
- Encodes the final video path at 10M video bitrate, 12M maxrate, 20M buffer, and 384k AAC audio.
- Maps the full original `voice_over.mp3` as final audio.
- Writes:

```text
/root/Resources/production_output_comfy/final_video_motion_0_6x_youtube1080_25fps_ebook_promo_10M.mp4
```

## Useful Logs

```bash
tail -f /var/log/portal/comfyui.log
tail -f /var/log/portal/api-wrapper.log
```

## Resume Behavior

Existing rendered scene files are skipped by default. This makes the workflow resumable after a crash or interrupted SSH session.

Use `--force` to regenerate.

## Output Structure

```text
/root/Resources/production_output_comfy/
  avatar-audio/
  avatar-audio-ltx/
  videos/
    scene_N.mp4
    scene_N_1.mp4
    scene_N_2.mp4
  work/
  final_video_motion_0_6x_youtube1080_25fps_ebook_promo_10M.mp4
```
