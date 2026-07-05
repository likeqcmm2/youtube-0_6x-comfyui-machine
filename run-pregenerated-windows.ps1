[CmdletBinding()]
param(
    [string]$Resources = "C:\Users\ezycloudx-admin\Desktop\Resources",
    [string]$OutputDir = "",
    [int]$FirstScene = 1,
    [int]$LastScene = 435,
    [int]$BackendPort = 41954,
    [double]$MotionSpeed = 0.6,
    [switch]$Force,
    [switch]$SkipMotion,
    [switch]$SkipAvatar,
    [switch]$SkipKenBurn,
    [switch]$SkipAssemble,
    [switch]$Youtube1080p,
    [string]$FinalName = "final_video_motion_0_6x_youtube1080_corrected.mp4"
)

$ErrorActionPreference = "Stop"

function Resolve-PathValue([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return $Path }
    return [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $Path))
}

$resourcesPath = Resolve-PathValue $Resources
$outputPath = if ($OutputDir) { Resolve-PathValue $OutputDir } else { Join-Path $resourcesPath "production_output" }
$appData = Join-Path $env:LOCALAPPDATA "LTXDesktop"
$python = Join-Path $appData "python\python.exe"
$backend = "C:\Program Files\LTX Desktop\resources\backend\ltx2_server.py"
$workflow = Join-Path $PSScriptRoot "ltx_linux_workflow.py"
$workDir = Join-Path $outputPath "work"
$runnerDir = Join-Path $workDir "runner"
$backendDir = Split-Path -Parent $backend
$backendLog = Join-Path $runnerDir "backend.log"
$workflowLog = Join-Path $runnerDir "workflow.log"
$bootstrap = Join-Path $runnerDir "ltx-backend-bootstrap.py"

foreach ($required in @(
    $python,
    $backend,
    $workflow,
    (Join-Path $resourcesPath "storyboard_elias_yoder.xlsx"),
    (Join-Path $resourcesPath "time_stamp.csv"),
    (Join-Path $resourcesPath "voice_over.mp3"),
    (Join-Path $resourcesPath "avatar.png"),
    (Join-Path $resourcesPath "Prompt_for_avatar.txt"),
    (Join-Path $resourcesPath "output_scenes")
)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required path not found: $required" }
}

New-Item -ItemType Directory -Path $runnerDir -Force | Out-Null
$ffmpeg = & $python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
if (-not (Test-Path -LiteralPath $ffmpeg)) { throw "Bundled FFmpeg not found: $ffmpeg" }

@(
    "import os",
    "import runpy",
    "import sys",
    "backend_dir = r'$backendDir'",
    "backend = r'$backend'",
    "os.environ['LTX_APP_DATA_DIR'] = r'$appData'",
    "os.environ['LTX_PORT'] = '$BackendPort'",
    "os.environ['LTX_AUTH_TOKEN'] = ''",
    "sys.path.insert(0, backend_dir)",
    "runpy.run_path(backend, run_name='__main__')"
) | Set-Content -LiteralPath $bootstrap -Encoding ASCII

$backendProcess = Start-Process -FilePath $python -ArgumentList @("-u", $bootstrap) `
    -WorkingDirectory $backendDir -RedirectStandardOutput $backendLog -RedirectStandardError "$backendLog.error" `
    -WindowStyle Hidden -PassThru

try {
    $deadline = (Get-Date).AddMinutes(4)
    do {
        if ($backendProcess.HasExited) { throw "LTX backend exited. See $backendLog.error" }
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/health" -TimeoutSec 2 | Out-Null
            break
        } catch {
            Start-Sleep -Seconds 2
        }
    } until ((Get-Date) -gt $deadline)

    if ((Get-Date) -gt $deadline) { throw "Timed out waiting for LTX backend." }

    $env:FFMPEG = $ffmpeg
    $args = @(
        "`"$workflow`"",
        "--resources", "`"$resourcesPath`"",
        "--output-dir", "`"$outputPath`"",
        "--base-url", "`"http://127.0.0.1:$BackendPort`"",
        "--first-scene", "$FirstScene",
        "--last-scene", "$LastScene",
        "--motion-speed", "$MotionSpeed",
        "--final-name", "`"$FinalName`""
    )
    if ($Force) { $args += "--force" }
    if ($SkipMotion) { $args += "--skip-motion" }
    if ($SkipAvatar) { $args += "--skip-avatar" }
    if ($SkipKenBurn) { $args += "--skip-kenburn" }
    if ($SkipAssemble) { $args += "--skip-assemble" }
    if ($Youtube1080p) { $args += "--youtube-1080p" }

    $cmdLine = "`"$python`" " + ($args -join " ") + " > `"$workflowLog`" 2>&1"
    & cmd.exe /d /c $cmdLine
    if ($LASTEXITCODE -ne 0) { throw "Workflow failed with exit code $LASTEXITCODE. See $workflowLog" }
} finally {
    if ($backendProcess -and -not $backendProcess.HasExited) {
        Stop-Process -Id $backendProcess.Id -Force
    }
}
