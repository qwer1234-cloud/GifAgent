# A/B runner for Lil Karina adult-clip optimization experiment.
# Usage:
#   powershell -File scripts/ab_lil_karina_run.ps1 -Phase baseline
#   powershell -File scripts/ab_lil_karina_run.ps1 -Phase optimized

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("baseline", "optimized", "optimized_v2", "optimized_v3")]
    [string]$Phase
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$wslIp = (wsl -e hostname -I).Trim().Split()[0]
$env:GIFAGENT_OLLAMA_BASE = "http://${wslIp}:11434"
$env:PYTHONUNBUFFERED = "1"
if ($Phase -like "optimized*") {
    $env:GIFAGENT_SCORE_PROMPT_MODE = "adult"
} else {
    Remove-Item Env:GIFAGENT_SCORE_PROMPT_MODE -ErrorAction SilentlyContinue
}
Write-Host "GIFAGENT_OLLAMA_BASE=$($env:GIFAGENT_OLLAMA_BASE)"
Write-Host "GIFAGENT_SCORE_PROMPT_MODE=$($env:GIFAGENT_SCORE_PROMPT_MODE)"
Write-Host "PYTHONUNBUFFERED=$($env:PYTHONUNBUFFERED)"

$VideoDir = "C:\Users\sunhao\Desktop\ToWatch\Lil Karina"
$Videos = @(
    "FapHouse.24.03.07.lil.karina.The.Artist.Fucked.His.Muse.mp4",
    "FapHouse.25.10.29.lil.karina.Fucked.Hard.Before.Going.to.University.mp4",
    "FapHouse.25.11.04.lil.karina.Asian.Girl.Is.Shy.Before.Having.Sex.After.College.mp4"
)

$ExportRoot = Join-Path $Root "data\exports\ab_lil_karina\$Phase"
$LogDir = Join-Path $Root "data\exports\ab_lil_karina\logs"
New-Item -ItemType Directory -Path $ExportRoot -Force | Out-Null
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$SummaryPath = Join-Path $ExportRoot "phase_summary.jsonl"
if (Test-Path $SummaryPath) { Remove-Item $SummaryPath -Force }

foreach ($name in $Videos) {
    $video = Join-Path $VideoDir $name
    if (-not (Test-Path -LiteralPath $video)) {
        throw "Missing video: $video"
    }
    $safe = [IO.Path]::GetFileNameWithoutExtension($name)
    $log = Join-Path $LogDir "${Phase}_${safe}.log"
    Write-Host ("=" * 72)
    Write-Host "[$Phase] START $name"
    Write-Host "Log: $log"
    Write-Host ("=" * 72)

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $errLog = "${log}.err"
    # cmd.exe preserves quoted paths with spaces reliably.
    $cmd = "uv run python scripts/test_video_adaptive.py --video `"$video`" --export-dir `"$ExportRoot`" > `"$log`" 2> `"$errLog`""
    cmd.exe /c $cmd
    $code = $LASTEXITCODE
    if (Test-Path $errLog) {
        Get-Content $errLog | Add-Content $log
        Remove-Item $errLog -Force -ErrorAction SilentlyContinue
    }
    $sw.Stop()

    $resultSrc = Join-Path $Root "data\adaptive_test_result.json"
    $resultDst = Join-Path $ExportRoot $safe
    New-Item -ItemType Directory -Path $resultDst -Force | Out-Null
    if (Test-Path $resultSrc) {
        Copy-Item $resultSrc (Join-Path $resultDst "result.json") -Force
    }

    $line = [ordered]@{
        phase           = $Phase
        video           = $name
        exit_code       = $code
        elapsed_sec     = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        export_dir      = (Join-Path $ExportRoot $safe)
        result_json     = (Join-Path $resultDst "result.json")
        finished_at     = (Get-Date -Format "o")
    } | ConvertTo-Json -Compress
    Add-Content -Path $SummaryPath -Value $line
    Write-Host "[$Phase] DONE $name exit=$code elapsed=$([math]::Round($sw.Elapsed.TotalMinutes,1))m"
    if ($code -ne 0) {
        Write-Warning "Non-zero exit for $name - continuing remaining videos"
    }
}

Write-Host "Phase $Phase complete. Summary: $SummaryPath"
