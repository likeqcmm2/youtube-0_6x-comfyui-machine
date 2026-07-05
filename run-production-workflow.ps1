[CmdletBinding()]
param(
    [string]$Storyboard = ".\work-test\storyboard.xlsx",
    [string]$Timestamps = "C:\Users\ezycloudx-admin\Desktop\Resources\time_stamp.csv",
    [string]$VoiceOver = "C:\Users\ezycloudx-admin\Desktop\Resources\voice_over.mp3",
    [string]$AvatarImage = "C:\Users\ezycloudx-admin\Desktop\Resources\avatar.png",
    [string]$AvatarPrompt = "C:\Users\ezycloudx-admin\Desktop\Resources\Prompt_for_avatar.txt",
    [string]$PersonaImagesDir = "",
    [string]$OutputDir = ".\output",
    [int]$FirstScene = 1,
    [int]$LastScene = 100,
    [int[]]$PersonaMotionScenes = @(),
    [int[]]$PersonaKenBurnScenes = @(),
    [int[]]$PersonaSplitScenes = @(),
    [int]$BackendPort = 41955,
    [double]$AvatarLeadInSeconds = 1.0,
    [double]$AvatarAudioGainDb = 20.0,
    [double]$YoutubeMotionSpeed = 0.6,
    [switch]$Force,
    [switch]$SkipLipsyncCheck,
    [switch]$SkipAssemble,
    [switch]$SkipYoutubeAssemble
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Resolve-PathValue([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return $Path }
    return [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $Path))
}

function Convert-TimecodeToSeconds([string]$Timecode) {
    if ($Timecode -notmatch '^(\d{2}):(\d{2}):(\d{2}\.\d{3})$') { throw "Invalid timecode: $Timecode" }
    return ([int]$Matches[1] * 3600) + ([int]$Matches[2] * 60) + [double]$Matches[3]
}

function Read-Storyboard([string]$Path, [int]$First, [int]$Last) {
    $zip = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entry = $zip.GetEntry("xl/worksheets/sheet1.xml")
        if (-not $entry) { throw "The first XLSX worksheet was not found." }
        $reader = [IO.StreamReader]::new($entry.Open())
        try { [xml]$xml = $reader.ReadToEnd() } finally { $reader.Dispose() }
    } finally { $zip.Dispose() }
    $ns = [Xml.XmlNamespaceManager]::new($xml.NameTable)
    $ns.AddNamespace("m", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
    function Cell([string]$Reference) {
        $node = $xml.SelectSingleNode("//m:c[@r='$Reference']", $ns)
        if (-not $node) { return "" }
        $text = $node.SelectSingleNode(".//m:t", $ns)
        if ($text) { return $text.InnerText.Trim() }
        $value = $node.SelectSingleNode("./m:v", $ns)
        if ($value) { return $value.InnerText.Trim() }
        return ""
    }
    return @($First..$Last | ForEach-Object {
        $row = $_ + 1
        [pscustomobject]@{
            Scene = $_
            ImagePrompt = Cell "C$row"
            MotionPrompt = Cell "D$row"
            Type = Cell "E$row"
        }
    })
}

function Read-Timestamps([string]$Path, [int]$First, [int]$Last) {
    $lines = @(Get-Content $Path | Select-Object -Skip 1)
    return @($First..$Last | ForEach-Object {
        $line = $lines[$_ - 1]
        if ($line -notmatch '(\d{2}:\d{2}:\d{2}\.\d{3})\s+-\s+(\d{2}:\d{2}:\d{2}\.\d{3})') {
            throw "Invalid timestamp for scene $_`: $line"
        }
        $start = Convert-TimecodeToSeconds $Matches[1]
        $end = Convert-TimecodeToSeconds $Matches[2]
        [pscustomobject]@{ Scene = $_; Start = $start; Duration = $end - $start }
    })
}

function Start-LtxBackend([string]$Python, [string]$Backend, [string]$AppData, [int]$Port, [string]$Log) {
    $env:LTX_APP_DATA_DIR = $AppData
    $env:LTX_PORT = "$Port"
    $env:LTX_AUTH_TOKEN = ""
    $backendDir = Split-Path -Parent $Backend
    $bootstrapPath = Join-Path (Split-Path -Parent $Log) "ltx-backend-bootstrap.py"
    @(
        "import os"
        "import runpy"
        "import sys"
        "backend_dir = r'$backendDir'"
        "backend = r'$Backend'"
        "os.environ['LTX_APP_DATA_DIR'] = r'$AppData'"
        "os.environ['LTX_PORT'] = '$Port'"
        "os.environ['LTX_AUTH_TOKEN'] = ''"
        "sys.path.insert(0, backend_dir)"
        "runpy.run_path(backend, run_name='__main__')"
    ) | Set-Content -LiteralPath $bootstrapPath -Encoding ASCII
    $process = Start-Process -FilePath $Python -ArgumentList @("-u", $bootstrapPath) `
        -WorkingDirectory $backendDir -RedirectStandardOutput $Log -RedirectStandardError "$Log.error" `
        -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddMinutes(3)
    do {
        if ($process.HasExited) { throw "LTX backend exited. See $Log.error" }
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 | Out-Null
            return $process
        } catch { Start-Sleep -Seconds 2 }
    } until ((Get-Date) -gt $deadline)
    throw "Timed out waiting for LTX backend."
}

function Invoke-Ltx([string]$BaseUrl, [string]$Route, [hashtable]$Body) {
    return Invoke-RestMethod -Uri "$BaseUrl$Route" -Method Post -ContentType "application/json" `
        -Body ($Body | ConvertTo-Json) -TimeoutSec 7200
}

function Run-Ffmpeg([string[]]$Arguments, [string]$Failure) {
    & $script:ffmpeg @Arguments
    if ($LASTEXITCODE -ne 0) { throw $Failure }
}

function Invoke-FinalAssembly(
    [object[]]$Scenes,
    [hashtable]$TimestampByScene,
    [string]$VideoDir,
    [string]$VoicePath,
    [string]$WorkDir,
    [string]$Output,
    [int]$Fps,
    [double]$MotionSpeed,
    [bool]$Youtube1080p
) {
    $inputs = @()
    $filters = @()
    $previousEndFrame = 0
    $frameRows = @("scene,type,frames")
    $motionCount = 0

    for ($index = 0; $index -lt $Scenes.Count; $index++) {
        $scene = $Scenes[$index]
        $sceneNumber = [int]$scene.Scene
        $videoPath = Join-Path $VideoDir "scene_$sceneNumber.mp4"
        if (-not (Test-Path -LiteralPath $videoPath)) { throw "Scene video not found: $videoPath" }
        $timestamp = $TimestampByScene[$sceneNumber]
        if (-not $timestamp) { throw "Timestamp not found for scene_$sceneNumber." }
        $endSeconds = $timestamp.Start + $timestamp.Duration
        $endFrame = [int][Math]::Floor(($endSeconds * $Fps) + 0.5)
        $frames = $endFrame - $previousEndFrame
        if ($frames -lt 1) { throw "Scene $sceneNumber has invalid frame count: $frames" }
        $previousEndFrame = $endFrame
        $frameRows += "$sceneNumber,$($scene.Type),$frames"
        $inputs += @("-i", $videoPath)

        $chain = "[$index`:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setpts=PTS-STARTPTS,"
        if ($scene.Type -eq "Motion" -and $MotionSpeed -ne 1.0) {
            $motionCount++
            $speedText = $MotionSpeed.ToString("0.###", [Globalization.CultureInfo]::InvariantCulture)
            $chain += "setpts=PTS/$speedText,"
        }
        $chain += "fps=$Fps,tpad=stop_mode=clone:stop=-1,trim=end_frame=$frames,setpts=N/($Fps*TB)[v$index]"
        $filters += $chain
    }

    $concatInputs = (0..($Scenes.Count - 1) | ForEach-Object { "[v$_]" }) -join ""
    $filters += ("$concatInputs" + "concat=n=$($Scenes.Count):v=1:a=0[outv]")
    New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null
    $label = if ($Youtube1080p) { "youtube-motion-0.6x" } else { "final" }
    $filterPath = Join-Path $WorkDir "assemble-$label-filter.txt"
    $countsPath = Join-Path $WorkDir "assemble-$label-frame-counts.csv"
    Set-Content -LiteralPath $filterPath -Value ($filters -join ";") -Encoding ASCII
    Set-Content -LiteralPath $countsPath -Value $frameRows -Encoding UTF8

    $totalFrames = 0
    foreach ($row in $frameRows | Select-Object -Skip 1) {
        $totalFrames += [int](($row -split ",")[-1])
    }
    $duration = ($totalFrames / $Fps).ToString("0.000000", [Globalization.CultureInfo]::InvariantCulture)
    if ($Youtube1080p) {
        $videoArgs = @("-c:v", "libx264", "-preset", "medium", "-b:v", "10M", "-maxrate", "12M", "-bufsize", "20M")
        $audioArgs = @("-c:a", "aac", "-b:a", "384k")
    } else {
        $videoArgs = @("-c:v", "libx264", "-preset", "medium", "-crf", "18")
        $audioArgs = @("-c:a", "aac", "-b:a", "192k")
    }

    $ffmpegArgs = @("-nostdin", "-y") + $inputs + @(
        "-i", $VoicePath,
        "-filter_complex_script", $filterPath,
        "-map", "[outv]",
        "-map", "$($Scenes.Count):a:0"
    ) + $videoArgs + @(
        "-r", "$Fps",
        "-pix_fmt", "yuv420p"
    ) + $audioArgs + @(
        "-t", $duration,
        "-movflags", "+faststart",
        $Output
    )
    Run-Ffmpeg $ffmpegArgs "Final assembly failed: $Output"
    Write-Host "Created $Output with $totalFrames frames ($duration seconds). Motion slowed: $motionCount"
}

$storyboardPath = Resolve-PathValue $Storyboard
$timestampsPath = Resolve-PathValue $Timestamps
$voicePath = Resolve-PathValue $VoiceOver
$avatarImagePath = Resolve-PathValue $AvatarImage
$avatarPromptPath = Resolve-PathValue $AvatarPrompt
$outputPath = Resolve-PathValue $OutputDir
$imageDir = Join-Path $outputPath "images"
$videoDir = Join-Path $outputPath "videos"
$audioDir = Join-Path $outputPath "avatar-audio"
$ltxAudioDir = Join-Path $outputPath "avatar-audio-ltx"
$workDir = Join-Path $outputPath "work"
$rawAvatarDir = Join-Path $workDir "avatar-videos-with-leadin"
@($outputPath, $imageDir, $videoDir, $audioDir, $ltxAudioDir, $workDir, $rawAvatarDir) | ForEach-Object {
    New-Item -ItemType Directory -Path $_ -Force | Out-Null
}

$appData = Join-Path $env:LOCALAPPDATA "LTXDesktop"
$python = Join-Path $appData "python\python.exe"
$backend = "C:\Program Files\LTX Desktop\resources\backend\ltx2_server.py"
foreach ($required in @($storyboardPath, $timestampsPath, $voicePath, $avatarImagePath, $avatarPromptPath, $python, $backend)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required file not found: $required" }
}
$script:ffmpeg = & $python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
$scenes = Read-Storyboard $storyboardPath $FirstScene $LastScene
$personaPath = if ($PersonaImagesDir) { Resolve-PathValue $PersonaImagesDir } else { "" }
foreach ($scene in $scenes | Where-Object Type -eq "Persona Story") {
    if ($PersonaMotionScenes -contains $scene.Scene) { $scene.Type = "Motion" }
    elseif ($PersonaKenBurnScenes -contains $scene.Scene) { $scene.Type = "Still Image + Ken Burn" }
    elseif ($PersonaSplitScenes -contains $scene.Scene) { $scene.Type = "Avatar/Split-screen" }
    else { continue }
    if (-not $personaPath) { throw "PersonaImagesDir is required for mapped Persona Story scenes." }
    $source = Join-Path $personaPath "scene_$($scene.Scene).png"
    if (-not (Test-Path $source)) { throw "Persona image not found: $source" }
    Copy-Item $source (Join-Path $imageDir "scene_$($scene.Scene).png") -Force
}
$timestampByScene = @{}
$timestampLines = @(Get-Content $timestampsPath | Select-Object -Skip 1)
foreach ($sceneNumber in $FirstScene..$LastScene) {
    $line = $timestampLines[$sceneNumber - 1]
    if ($line -notmatch '(\d{2}:\d{2}:\d{2}\.\d{3})\s+-\s+(\d{2}:\d{2}:\d{2}\.\d{3})') {
        throw "Invalid timestamp for scene $sceneNumber`: $line"
    }
    $startText = $Matches[1]
    $endText = $Matches[2]
    $start = Convert-TimecodeToSeconds $startText
    $end = Convert-TimecodeToSeconds $endText
    $timestampByScene[$sceneNumber] = [pscustomobject]@{ Start = $start; Duration = $end - $start }
}
$avatarPromptText = (Get-Content $avatarPromptPath -Raw).Trim()
$baseUrl = "http://127.0.0.1:$BackendPort"
$backendProcess = $null

try {
    $backendProcess = Start-LtxBackend $python $backend $appData $BackendPort (Join-Path $workDir "backend.log")

    foreach ($scene in $scenes | Where-Object { $_.ImagePrompt -and $_.Type -ne "Persona Story" }) {
        $destination = Join-Path $imageDir "scene_$($scene.Scene).png"
        if ((Test-Path $destination) -and -not $Force) { continue }
        Write-Host "IMAGE scene_$($scene.Scene) [$($scene.Type)]"
        $result = Invoke-Ltx $baseUrl "/api/generate-image" @{
            prompt = $scene.ImagePrompt; width = 1920; height = 1080; numSteps = 4; numImages = 1
        }
        Copy-Item $result.image_paths[0] $destination -Force
    }

    foreach ($scene in $scenes | Where-Object Type -eq "Motion") {
        $image = Join-Path $imageDir "scene_$($scene.Scene).png"
        $destination = Join-Path $videoDir "scene_$($scene.Scene).mp4"
        if ((Test-Path $destination) -and -not $Force) { continue }
        Write-Host "MOTION scene_$($scene.Scene)"
        $result = Invoke-Ltx $baseUrl "/api/generate" @{
            prompt = $scene.MotionPrompt; resolution = "1080p"; model = "fast"; cameraMotion = "none"
            negativePrompt = ""; duration = 5; fps = 24; audio = $false; imagePath = $image
            audioPath = $null; aspectRatio = "16:9"
        }
        Copy-Item $result.video_path $destination -Force
        Run-Ffmpeg @("-y", "-i", $destination, "-map", "0:v:0", "-c:v", "copy", "$destination.silent.mp4") "Failed to silence scene_$($scene.Scene)."
        Move-Item "$destination.silent.mp4" $destination -Force
    }

    foreach ($scene in $scenes | Where-Object { $_.Type -in @("Avatar", "Avatar/Split-screen") }) {
        $sceneNumber = [int]$scene.Scene
        $timestamp = $timestampByScene[$sceneNumber]
        if (-not $timestamp) { throw "Timestamp not found for scene_$sceneNumber." }
        $audio = Join-Path $audioDir "scene_$($scene.Scene).mp3"
        $destinationName = if ($scene.Type -eq "Avatar/Split-screen") { "scene_$($scene.Scene)_1.mp4" } else { "scene_$($scene.Scene).mp4" }
        $destination = Join-Path $videoDir $destinationName
        if ($Force -or -not (Test-Path $audio)) {
            $start = $timestamp.Start.ToString("0.000", [Globalization.CultureInfo]::InvariantCulture)
            $duration = $timestamp.Duration.ToString("0.000", [Globalization.CultureInfo]::InvariantCulture)
            Run-Ffmpeg @("-y", "-ss", $start, "-t", $duration, "-i", $voicePath, "-vn", "-codec:a", "libmp3lame", "-q:a", "2", $audio) "Failed to cut avatar audio scene_$($scene.Scene)."
        }
        $ltxAudio = Join-Path $ltxAudioDir "scene_$($scene.Scene).mp3"
        if ($Force -or -not (Test-Path $ltxAudio)) {
            $leadIn = $AvatarLeadInSeconds.ToString("0.000", [Globalization.CultureInfo]::InvariantCulture)
            $paddedAudio = Join-Path $ltxAudioDir "scene_$($scene.Scene).padded.tmp.mp3"
            Run-Ffmpeg @(
                "-y",
                "-f", "lavfi",
                "-t", $leadIn,
                "-i", "anullsrc=r=44100:cl=stereo",
                "-i", $audio,
                "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                "-map", "[a]",
                "-codec:a", "libmp3lame",
                "-q:a", "0",
                $paddedAudio
            ) "Failed to add avatar lead-in scene_$($scene.Scene)."
            Run-Ffmpeg @(
                "-y",
                "-i", $paddedAudio,
                "-filter:a", "volume=$($AvatarAudioGainDb)dB",
                "-q:a", "0",
                $ltxAudio
            ) "Failed to boost LTX avatar audio scene_$($scene.Scene)."
            Remove-Item -LiteralPath $paddedAudio -Force -ErrorAction SilentlyContinue
        }
        if ((Test-Path $destination) -and -not $Force) { continue }
        Write-Host "AVATAR scene_$($scene.Scene) [$($scene.Type)]"
        $generationDuration = [Math]::Max(6, [Math]::Ceiling($timestamp.Duration + $AvatarLeadInSeconds))
        $avatarResolution = if ($generationDuration -gt 5) { "720p" } else { "1080p" }
        $result = Invoke-Ltx $baseUrl "/api/generate" @{
            prompt = $avatarPromptText; resolution = $avatarResolution; model = "fast"; cameraMotion = "none"
            negativePrompt = ""; duration = $generationDuration; fps = 24; audio = $true; imagePath = $avatarImagePath
            audioPath = $ltxAudio; aspectRatio = "16:9"
        }
        $rawDestination = Join-Path $rawAvatarDir $destinationName
        Copy-Item $result.video_path $rawDestination -Force
        $trimDuration = $timestamp.Duration.ToString("0.000", [Globalization.CultureInfo]::InvariantCulture)
        $leadIn = $AvatarLeadInSeconds.ToString("0.000", [Globalization.CultureInfo]::InvariantCulture)
        Run-Ffmpeg @(
            "-y",
            "-i", $rawDestination,
            "-ss", $leadIn,
            "-t", $trimDuration,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuv420p",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            $destination
        ) "Failed to trim avatar lead-in scene_$($scene.Scene)."
    }
}
finally {
    if ($backendProcess -and -not $backendProcess.HasExited) { Stop-Process -Id $backendProcess.Id -Force }
}

foreach ($scene in $scenes | Where-Object { $_.Type -in @("Still Image + Ken Burn", "Avatar/Split-screen") }) {
    $image = Join-Path $imageDir "scene_$($scene.Scene).png"
    $destinationName = if ($scene.Type -eq "Avatar/Split-screen") { "scene_$($scene.Scene)_2.mp4" } else { "scene_$($scene.Scene).mp4" }
    $destination = Join-Path $videoDir $destinationName
    if ((Test-Path $destination) -and -not $Force) { continue }
    Write-Host "KENBURN scene_$($scene.Scene) [$($scene.Type)]"
    Run-Ffmpeg @(
        "-y", "-loop", "1", "-i", $image,
        "-vf", "scale=8000:-1,zoompan=z='zoom+0.001':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=150:s=1920x1080,format=yuv420p",
        "-an", "-c:v", "h264_nvenc", "-t", "5", $destination
    ) "Failed Ken Burn scene_$($scene.Scene)."
}

foreach ($scene in $scenes | Where-Object Type -eq "Avatar/Split-screen") {
    $left = Join-Path $videoDir "scene_$($scene.Scene)_1.mp4"
    $right = Join-Path $videoDir "scene_$($scene.Scene)_2.mp4"
    $destination = Join-Path $videoDir "scene_$($scene.Scene).mp4"
    if ((Test-Path $destination) -and -not $Force) { continue }
    Write-Host "SPLIT scene_$($scene.Scene)"
    Run-Ffmpeg @(
        "-y", "-i", $left, "-i", $right,
        "-filter_complex", "[0:v]scale=960:1080:force_original_aspect_ratio=increase,crop=960:1080[left];[1:v]scale=960:1080:force_original_aspect_ratio=increase,crop=960:1080[right];[left][right]hstack=inputs=2[v]",
        "-map", "[v]", "-map", "0:a:0", "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23",
        "-c:a", "aac", "-shortest", $destination
    ) "Failed split-screen scene_$($scene.Scene)."
}

Write-Host "Completed scenes $FirstScene-$LastScene in $outputPath"

if (-not $SkipLipsyncCheck) {
    $avatarVideoDir = Join-Path $outputPath "avatar-videos"
    New-Item -ItemType Directory -Path $avatarVideoDir -Force | Out-Null
    foreach ($scene in $scenes | Where-Object Type -eq "Avatar") {
        Copy-Item (Join-Path $videoDir "scene_$($scene.Scene).mp4") $avatarVideoDir -Force
    }
    foreach ($scene in $scenes | Where-Object Type -eq "Avatar/Split-screen") {
        Copy-Item (Join-Path $videoDir "scene_$($scene.Scene)_1.mp4") $avatarVideoDir -Force
    }
    Write-Host "Avatar clips copied to $avatarVideoDir for optional lipsync validation."
}

if (-not $SkipAssemble) {
    Invoke-FinalAssembly `
        -Scenes $scenes `
        -TimestampByScene $timestampByScene `
        -VideoDir $videoDir `
        -VoicePath $voicePath `
        -WorkDir $workDir `
        -Output (Join-Path $outputPath "final_video.mp4") `
        -Fps 24 `
        -MotionSpeed 1.0 `
        -Youtube1080p $false
}

if (-not $SkipYoutubeAssemble) {
    Invoke-FinalAssembly `
        -Scenes $scenes `
        -TimestampByScene $timestampByScene `
        -VideoDir $videoDir `
        -VoicePath $voicePath `
        -WorkDir $workDir `
        -Output (Join-Path $outputPath "final_video_motion_0_6x_youtube1080_corrected.mp4") `
        -Fps 24 `
        -MotionSpeed $YoutubeMotionSpeed `
        -Youtube1080p $true
}
