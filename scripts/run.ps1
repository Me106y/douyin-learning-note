# Windows 入口：与 run.sh 等价，CLI 只需要 requests，worker 的依赖由 douyin_note.py 自己按平台追加。
$ErrorActionPreference = "Stop"

$SkillDir = Split-Path -Parent $PSScriptRoot

$UvBin = $env:DOUYIN_NOTE_UV
if (-not $UvBin) {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { $UvBin = $cmd.Source }
}
if (-not $UvBin) {
    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe"),
        (Join-Path $env:APPDATA "Python\Python312\Scripts\uv.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $UvBin = $candidate; break }
    }
}
if (-not $UvBin) {
    [Console]::Error.WriteLine("uv not found. Install uv first: https://docs.astral.sh/uv/")
    [Console]::Error.WriteLine("  winget install --id astral-sh.uv -e")
    [Console]::Error.WriteLine("  or: python -m pip install --user uv")
    exit 1
}

$env:DOUYIN_NOTE_UV = $UvBin
if (-not $env:UV_DEFAULT_INDEX) { $env:UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple" }
if (-not $env:HF_ENDPOINT) { $env:HF_ENDPOINT = "https://hf-mirror.com" }

& $UvBin run --python 3.12 --with requests -- python (Join-Path $SkillDir "scripts\douyin_note.py") @args
exit $LASTEXITCODE
