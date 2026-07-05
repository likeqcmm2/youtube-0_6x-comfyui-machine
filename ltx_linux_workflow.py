#!/usr/bin/env python3
"""Linux adaptation of ltx-storyboard-workflow for pre-generated scene images."""
from __future__ import annotations

import argparse, base64, csv, json, os, re, shutil, subprocess, sys, time, zipfile
from pathlib import Path
import urllib.request
import xml.etree.ElementTree as ET

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

def run(cmd, msg=None):
    print('+', ' '.join(map(str, cmd)), flush=True)
    p = subprocess.run(cmd)
    if p.returncode != 0:
        raise RuntimeError(msg or f'Command failed: {cmd}')

def ffprobe_duration(path: Path) -> float:
    ffprobe = os.environ.get('FFPROBE')
    if ffprobe:
        out = subprocess.check_output([ffprobe,'-v','error','-show_entries','format=duration','-of','default=nk=1:nw=1',str(path)], text=True).strip()
        return float(out)
    ffmpeg = os.environ.get('FFMPEG', 'ffmpeg')
    p = subprocess.run([ffmpeg, '-hide_banner', '-i', str(path)], capture_output=True, text=True)
    m = re.search(r'Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)', p.stderr + p.stdout)
    if not m:
        raise RuntimeError(f'Could not determine duration for {path}')
    return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))

def timecode_seconds(s: str) -> float:
    m = re.match(r'^(\d{1,2}):(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$', s.strip())
    if not m: raise ValueError(f'Invalid timecode: {s}')
    frac = (m.group(4) or '0').ljust(3, '0')[:3]
    return int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(frac)/1000.0

def read_storyboard(path: Path, first: int, last: int):
    with zipfile.ZipFile(path) as z:
        shared=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            root=ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in root.findall('m:si', NS):
                shared.append(''.join(t.text or '' for t in si.findall('.//m:t', NS)))
        root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        cells={}
        for c in root.findall('.//m:c', NS):
            ref=c.attrib.get('r','')
            typ=c.attrib.get('t')
            v=c.find('m:v', NS)
            is_el=c.find('m:is', NS)
            val=''
            if typ=='s' and v is not None:
                val=shared[int(v.text)]
            elif typ=='inlineStr' and is_el is not None:
                val=''.join(t.text or '' for t in is_el.findall('.//m:t', NS))
            elif v is not None:
                val=v.text or ''
            cells[ref]=val.strip()
    scenes=[]
    for scene in range(first, last+1):
        row=scene+1
        scenes.append({
            'scene': scene,
            'image_prompt': cells.get(f'C{row}','').strip(),
            'motion_prompt': cells.get(f'D{row}','').strip(),
            'type': cells.get(f'E{row}','').strip(),
        })
    return scenes

def read_timestamps(path: Path, first: int, last: int):
    text = path.read_text(encoding='utf-8-sig').splitlines()
    rows = text[1:]
    out={}
    for scene in range(first, last+1):
        line = rows[scene-1]
        m = re.search(r'(\d{1,2}:\d{1,2}:\d{1,2}(?:\.\d{1,3})?)\s+-\s+(\d{1,2}:\d{1,2}:\d{1,2}(?:\.\d{1,3})?)', line)
        if not m: raise ValueError(f'Invalid timestamp for scene {scene}: {line}')
        start, end = timecode_seconds(m.group(1)), timecode_seconds(m.group(2))
        out[scene] = {'start': start, 'end': end, 'duration': max(0.001, end-start)}
    return out

def api_post(base_url: str, route: str, body: dict, timeout: int = 7200):
    data=json.dumps(body).encode('utf-8')
    req=urllib.request.Request(base_url+route, data=data, headers={'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def api_get(base_url: str, route: str, timeout: int = 30):
    with urllib.request.urlopen(base_url+route, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def wait_backend(base_url: str):
    deadline=time.time()+180
    while time.time()<deadline:
        try:
            return api_get(base_url, '/health')
        except Exception:
            time.sleep(2)
    raise RuntimeError('Timed out waiting for backend')

def make_kenburn(ffmpeg: str, image: Path, out: Path, duration: float, fps: int):
    out.parent.mkdir(parents=True, exist_ok=True)
    dframes=max(1, int(round(duration*fps)))
    vf=f"scale=8000:-1,zoompan=z='zoom+0.001':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={dframes}:s=1920x1080:fps={fps},format=yuv420p"
    run([ffmpeg,'-nostdin','-y','-loop','1','-i',str(image),'-vf',vf,'-an','-c:v','libx264','-preset','veryfast','-crf','20','-t',f'{duration:.3f}',str(out)], f'KenBurn failed {out}')

def strip_audio(ffmpeg: str, src: Path, dst: Path):
    tmp=dst.with_suffix('.silent.tmp.mp4')
    run([ffmpeg,'-nostdin','-y','-i',str(src),'-map','0:v:0','-c:v','copy','-an',str(tmp)], f'Strip audio failed {dst}')
    tmp.replace(dst)

def prepare_ltx_avatar_audio(ffmpeg: str, src: Path, dst: Path, lead_in: float, gain_db: float):
    dst.parent.mkdir(parents=True, exist_ok=True)
    padded = dst.with_suffix('.padded.tmp.mp3')
    run([
        ffmpeg,'-nostdin','-y',
        '-f','lavfi','-t',f'{lead_in:.3f}','-i','anullsrc=r=44100:cl=stereo',
        '-i',str(src),
        '-filter_complex','[0:a][1:a]concat=n=2:v=0:a=1[a]',
        '-map','[a]','-ac','2','-codec:a','libmp3lame','-q:a','0',str(padded),
    ], f'Add avatar lead-in failed {dst}')
    run([
        ffmpeg,'-nostdin','-y','-i',str(padded),'-filter:a',f'volume={gain_db:g}dB',
        '-ac','2','-q:a','0',str(dst),
    ], f'Boost avatar audio failed {dst}')
    padded.unlink(missing_ok=True)

def trim_avatar_leadin(ffmpeg: str, src: Path, dst: Path, lead_in: float, duration: float):
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg,'-nostdin','-y','-i',str(src),'-ss',f'{lead_in:.3f}','-t',f'{duration:.3f}',
        '-map','0:v:0','-map','0:a:0?','-c:v','libx264','-preset','veryfast','-crf','18',
        '-vf','scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuv420p',
        '-c:a','aac','-b:a','192k','-movflags','+faststart',str(dst),
    ], f'Trim avatar lead-in failed {dst}')

def avatar_lead_in_for_duration(duration: float, requested: float) -> float:
    # LTX Desktop rejects avatar requests above its short audio window. Keep
    # the audio comfortably below 6s while preserving as much lead-in as possible.
    if duration + requested <= 5.9:
        return requested
    return max(0.25, 5.9 - duration)

def copy_scene_images(scenes, source_dir: Path, image_dir: Path):
    image_dir.mkdir(parents=True, exist_ok=True)
    missing=[]
    for sc in scenes:
        if sc['type'] in ('Motion','Still Image + Ken Burn','Avatar/Split-screen'):
            src=source_dir/f"scene_{sc['scene']}.png"
            dst=image_dir/src.name
            if not src.exists(): missing.append(sc['scene']); continue
            if not dst.exists(): shutil.copy2(src,dst)
    if missing:
        raise RuntimeError(f'Missing scene images: {missing[:50]} count={len(missing)}')

def assemble_frame_accurate(
    ffmpeg: str,
    video_dir: Path,
    voice: Path,
    timestamps: dict,
    scenes,
    output: Path,
    work_dir: Path,
    fps: int,
    motion_speed: float = 1.0,
    youtube_1080p: bool = False,
):
    inputs=[]; filters=[]; prev=0; frame_counts=[]; motion_count=0
    selected=[s for s in scenes]
    for idx, sc in enumerate(selected):
        scene=sc['scene']
        end_frame=int(timestamps[scene]['end']*fps + 0.5)
        frames=end_frame-prev
        if frames<1: raise RuntimeError(f'Scene {scene} invalid frames {frames}')
        prev=end_frame; frame_counts.append((scene, sc['type'], frames))
        inputs += ['-i', str(video_dir/f'scene_{scene}.mp4')]
        chain = (
            f"[{idx}:v]"
            "scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,"
            "setpts=PTS-STARTPTS,"
        )
        if sc['type'] == 'Motion' and motion_speed != 1.0:
            motion_count += 1
            chain += f"setpts=PTS/{motion_speed},"
        chain += (
            f"fps={fps},"
            "tpad=stop_mode=clone:stop=-1,"
            f"trim=end_frame={frames},"
            f"setpts=N/({fps}*TB)[v{idx}]"
        )
        filters.append(chain)
    concat=''.join(f'[v{i}]' for i in range(len(selected)))
    filters.append(f'{concat}concat=n={len(selected)}:v=1:a=0[outv]')
    work_dir.mkdir(parents=True, exist_ok=True)
    filter_path=work_dir/'assemble-frame-accurate-filter.txt'
    filter_path.write_text(';'.join(filters), encoding='ascii')
    (work_dir/'assemble-frame-counts.csv').write_text(
        'scene,type,frames\n'+'\n'.join(f'{s},{t},{f}' for s,t,f in frame_counts)+'\n',
        encoding='utf-8',
    )
    total=sum(f for _,_,f in frame_counts); dur=total/fps
    output.parent.mkdir(parents=True, exist_ok=True)
    if youtube_1080p:
        video_args=['-c:v','libx264','-preset','medium','-b:v','10M','-maxrate','12M','-bufsize','20M']
        audio_args=['-c:a','aac','-b:a','384k']
    else:
        video_args=['-c:v','libx264','-preset','medium','-crf','18']
        audio_args=['-c:a','aac','-b:a','192k']
    run([ffmpeg,'-nostdin','-y',*inputs,'-i',str(voice),'-filter_complex_script',str(filter_path),'-map','[outv]','-map',f'{len(selected)}:a:0',*video_args,'-r',str(fps),'-pix_fmt','yuv420p',*audio_args,'-t',f'{dur:.6f}','-movflags','+faststart',str(output)], 'Final assembly failed')
    print(f'Created {output} with {total} frames ({dur:.3f}s); motion_slowed={motion_count} speed={motion_speed}')

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--resources', default='/root/Resources')
    ap.add_argument('--output-dir', default='/root/Resources/production_output')
    ap.add_argument('--base-url', default='http://127.0.0.1:41954')
    ap.add_argument('--first-scene', type=int, default=1)
    ap.add_argument('--last-scene', type=int, default=371)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--skip-motion', action='store_true')
    ap.add_argument('--skip-avatar', action='store_true')
    ap.add_argument('--skip-kenburn', action='store_true')
    ap.add_argument('--skip-assemble', action='store_true')
    ap.add_argument('--motion-speed', type=float, default=1.0, help='Slow only Motion scenes during final assembly; use 0.6 for the corrected YouTube edit.')
    ap.add_argument('--youtube-1080p', action='store_true', help='Export H.264/AAC settings suitable for YouTube 1080p.')
    ap.add_argument('--final-name', default='final_video.mp4', help='Final output filename inside output dir.')
    ap.add_argument('--avatar-lead-in-seconds', type=float, default=1.0)
    ap.add_argument('--avatar-audio-gain-db', type=float, default=20.0)
    args=ap.parse_args()
    res=Path(args.resources)
    out=Path(args.output_dir)
    image_dir=out/'images'; video_dir=out/'videos'; audio_dir=out/'avatar-audio'; ltx_audio_dir=out/'avatar-audio-ltx'; work_dir=out/'work'; avatar_video_dir=out/'avatar-videos'; raw_avatar_dir=work_dir/'avatar-videos-with-leadin'
    for d in (out,image_dir,video_dir,audio_dir,ltx_audio_dir,work_dir,avatar_video_dir,raw_avatar_dir): d.mkdir(parents=True, exist_ok=True)
    storyboard=res/'storyboard_elias_yoder.xlsx'; timestamps_path=res/'time_stamp.csv'; voice=res/'voice_over.mp3'; avatar_img=res/'avatar.png'; avatar_prompt=(res/'Prompt_for_avatar.txt').read_text(encoding='utf-8').strip()
    scenes=read_storyboard(storyboard,args.first_scene,args.last_scene)
    timestamps=read_timestamps(timestamps_path,args.first_scene,args.last_scene)
    copy_scene_images(scenes, res/'output_scenes', image_dir)
    ffmpeg=os.environ.get('FFMPEG', 'ffmpeg')
    wait_backend(args.base_url)
    fps=24
    # Motion: image-to-video, silent.
    if not args.skip_motion:
        for sc in scenes:
            if sc['type']!='Motion': continue
            n=sc['scene']; dst=video_dir/f'scene_{n}.mp4'
            if dst.exists() and not args.force: print(f'SKIP MOTION {n}', flush=True); continue
            print(f'MOTION {n}', flush=True)
            r=api_post(args.base_url,'/api/generate',{'prompt': sc['motion_prompt'] or sc['image_prompt'], 'resolution':'1080p','model':'fast','cameraMotion':'none','negativePrompt':'','duration':5,'fps':fps,'audio':False,'imagePath':str(image_dir/f'scene_{n}.png'),'audioPath':None,'aspectRatio':'16:9'})
            shutil.copy2(r['video_path'], dst)
            strip_audio(ffmpeg, dst, dst)
    # Avatar and split avatar source.
    if not args.skip_avatar:
        for sc in scenes:
            if sc['type'] not in ('Avatar','Avatar/Split-screen'): continue
            n=sc['scene']; ts=timestamps[n]
            aud=audio_dir/f'scene_{n}.mp3'
            if args.force or not aud.exists():
                run([ffmpeg,'-nostdin','-y','-ss',f"{ts['start']:.3f}",'-t',f"{ts['duration']:.3f}",'-i',str(voice),'-vn','-codec:a','libmp3lame','-q:a','2',str(aud)], f'Audio cut failed {n}')
            dur=ffprobe_duration(aud)
            avatar_lead_in = avatar_lead_in_for_duration(dur, args.avatar_lead_in_seconds)
            ltx_aud=ltx_audio_dir/f'scene_{n}.mp3'
            if args.force or not ltx_aud.exists():
                prepare_ltx_avatar_audio(ffmpeg, aud, ltx_aud, avatar_lead_in, args.avatar_audio_gain_db)
            dst=video_dir/(f'scene_{n}_1.mp4' if sc['type']=='Avatar/Split-screen' else f'scene_{n}.mp4')
            if dst.exists() and not args.force: print(f'SKIP AVATAR {n}', flush=True); continue
            gen_dur=max(6, int((dur + avatar_lead_in) + 0.999))
            avatar_resolution = '720p' if gen_dur > 5 else '1080p'
            raw_dst=raw_avatar_dir/(f'scene_{n}_1.mp4' if sc['type']=='Avatar/Split-screen' else f'scene_{n}.mp4')
            print(f'AVATAR {n} audio={dur:.3f}s lead_in={avatar_lead_in:.3f}s ltx_audio={dur + avatar_lead_in:.3f}s generation_duration={gen_dur}s resolution={avatar_resolution}->1080p', flush=True)
            r=api_post(args.base_url,'/api/generate',{'prompt': avatar_prompt,'resolution':avatar_resolution,'model':'fast','cameraMotion':'none','negativePrompt':'','duration':gen_dur,'fps':fps,'audio':True,'imagePath':str(avatar_img),'audioPath':str(ltx_aud),'aspectRatio':'16:9'})
            shutil.copy2(r['video_path'], raw_dst)
            trim_avatar_leadin(ffmpeg, raw_dst, dst, avatar_lead_in, dur)
            if sc['type']=='Avatar': shutil.copy2(dst, avatar_video_dir/dst.name)
            else: shutil.copy2(dst, avatar_video_dir/dst.name)
    # KenBurn clips: standalone and right side for split-screen.
    if not args.skip_kenburn:
        for sc in scenes:
            if sc['type'] not in ('Still Image + Ken Burn','Avatar/Split-screen'): continue
            n=sc['scene']; dst=video_dir/(f'scene_{n}_2.mp4' if sc['type']=='Avatar/Split-screen' else f'scene_{n}.mp4')
            if dst.exists() and not args.force: print(f'SKIP KENBURN {n}', flush=True); continue
            print(f'KENBURN {n}', flush=True)
            make_kenburn(ffmpeg, image_dir/f'scene_{n}.png', dst, max(5.0, timestamps[n]['duration']), fps)
    # Split-screen.
    if args.skip_avatar or args.skip_kenburn:
        print('SKIP SPLIT stage because avatar or kenburn stage is skipped', flush=True)
    else:
        for sc in scenes:
            if sc['type']!='Avatar/Split-screen': continue
            n=sc['scene']; dst=video_dir/f'scene_{n}.mp4'
            if dst.exists() and not args.force: print(f'SKIP SPLIT {n}', flush=True); continue
            left=video_dir/f'scene_{n}_1.mp4'; right=video_dir/f'scene_{n}_2.mp4'
            print(f'SPLIT {n}', flush=True)
            run([ffmpeg,'-nostdin','-y','-i',str(left),'-i',str(right),'-filter_complex','[0:v]scale=960:1080:force_original_aspect_ratio=increase,crop=960:1080[left];[1:v]scale=960:1080:force_original_aspect_ratio=increase,crop=960:1080[right];[left][right]hstack=inputs=2[v]','-map','[v]','-map','0:a:0','-c:v','libx264','-preset','veryfast','-crf','20','-c:a','aac','-shortest',str(dst)], f'Split failed {n}')
    if not args.skip_assemble:
        assemble_frame_accurate(ffmpeg, video_dir, voice, timestamps, scenes, out/args.final_name, work_dir, fps, args.motion_speed, args.youtube_1080p)

if __name__=='__main__': main()
