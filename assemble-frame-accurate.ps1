param(
    [string]$Config = ".\config.json"
)
$ErrorActionPreference = "Stop"
$cfg = Get-Content ([IO.Path]::GetFullPath($Config)) -Raw | ConvertFrom-Json
$baseDir = Split-Path -Parent ([IO.Path]::GetFullPath($Config))
function Resolve-ConfigPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return $Path }
    return [IO.Path]::GetFullPath((Join-Path $baseDir $Path))
}
$fps = [int]$cfg.video_fps
$outputDir = Resolve-ConfigPath $cfg.output_dir
$videoDir = Join-Path $outputDir "videos"
$workDir = Join-Path $outputDir "work"
$timestampsPath = Resolve-ConfigPath $cfg.timestamps
$voicePath = Resolve-ConfigPath $cfg.voice_over
$final = Join-Path $outputDir "final_video.mp4"
$python = Join-Path $env:LOCALAPPDATA "LTXDesktop\python\python.exe"
$ffmpeg = & $python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"

function Seconds([string]$Timecode) {
    if ($Timecode -notmatch '^(\d{2}):(\d{2}):(\d{2}\.\d{3})$') { throw "Invalid timecode: $Timecode" }
    return ([int]$Matches[1] * 3600) + ([int]$Matches[2] * 60) + [double]$Matches[3]
}

$firstScene = [int]$cfg.first_scene
$lastScene = [int]$cfg.last_scene
$sceneCount = $lastScene - $firstScene + 1
$lines = @(Get-Content $timestampsPath | Select-Object -Skip $firstScene -First $sceneCount)
$inputs = @()
$filters = @()
$previousEndFrame = 0
$frameCounts = @()

for ($index = 0; $index -lt $sceneCount; $index++) {
    $scene = $firstScene + $index
    $line = $lines[$index]
    if ($line -notmatch '(\d{2}:\d{2}:\d{2}\.\d{3})\s+-\s+(\d{2}:\d{2}:\d{2}\.\d{3})') {
        throw "Invalid timestamp for scene $scene`: $line"
    }
    $endSeconds = Seconds $Matches[2]
    # Round cumulative absolute boundaries, then derive each scene's frame count.
    $endFrame = [int][Math]::Round($endSeconds * $fps, [MidpointRounding]::AwayFromZero)
    $frames = $endFrame - $previousEndFrame
    if ($frames -lt 1) { throw "Scene $scene has invalid frame count: $frames" }
    $frameCounts += $frames
    $previousEndFrame = $endFrame
    $inputs += @("-i", (Join-Path $videoDir "scene_$scene.mp4"))
    $filters += "[$index`:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=$fps,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop=-1,trim=end_frame=$frames,setpts=PTS-STARTPTS[v$index]"
}

$concat = (0..($sceneCount - 1) | ForEach-Object { "[v$_]" }) -join ""
$filters += "${concat}concat=n=$sceneCount`:v=1:a=0[outv]"
$filterPath = Join-Path $workDir "assemble-frame-accurate-filter.txt"
Set-Content $filterPath ($filters -join ";") -Encoding Ascii
$frameMap = Join-Path $workDir "assemble-frame-counts.csv"
@("scene,frames") + (0..($sceneCount - 1) | ForEach-Object { "$($firstScene + $_),$($frameCounts[$_])" }) |
    Set-Content $frameMap -Encoding Ascii

$totalFrames = ($frameCounts | Measure-Object -Sum).Sum
$duration = ($totalFrames / $fps).ToString("0.000000", [Globalization.CultureInfo]::InvariantCulture)
$args = @("-y") + $inputs + @(
    "-i", $voicePath, "-filter_complex_script", $filterPath,
    "-map", "[outv]", "-map", "$sceneCount`:a:0",
    "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", "$fps",
    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
    "-t", $duration, "-movflags", "+faststart", $final
)
& $ffmpeg @args
if ($LASTEXITCODE -ne 0) { throw "Frame-accurate assembly failed." }
Write-Host "Created $final with $totalFrames frames ($duration seconds)."
