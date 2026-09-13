$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RefreshSeconds = 1800
$MaxLoginAttempts = 3
$Port = 4000

Set-Location $ProjectDir

# 本地直连，避免系统代理接管 127.0.0.1
$env:NO_PROXY = "localhost,127.0.0.1"
$env:no_proxy = "localhost,127.0.0.1"
$env:PYTHONIOENCODING = "utf-8"

function Invoke-WebVpnLogin {
    param([int] $MaxAttempts = 3)

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        Write-Host "WebVPN login attempt $attempt/$MaxAttempts..."
        & uv run python .\ujn_webvpn_login.py --headless
        if ($LASTEXITCODE -eq 0) {
            Write-Host "WebVPN login succeeded."
            return $true
        }
        Write-Warning "WebVPN login failed (exit $LASTEXITCODE)."
        if ($attempt -lt $MaxAttempts) { Start-Sleep -Seconds 5 }
    }
    return $false
}

function Update-LiteLlmConfig {
    & uv run python .\build_litellm_config.py
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to generate litellm_config.yaml"
    }
}

Write-Host "Starting UJN LLM API stack from $ProjectDir"

if (-not (Invoke-WebVpnLogin -MaxAttempts $MaxLoginAttempts)) {
    throw "Initial WebVPN login failed after $MaxLoginAttempts attempts."
}

Update-LiteLlmConfig

# ⚠ LiteLLM 只在启动时读取一次 litellm_config.yaml（已实测：运行中修改文件里的
#   Cookie，服务仍用旧值请求上游）。所以后台刷新 Cookie 后【必须重启代理】才生效。
#   本脚本的做法：后台任务只负责刷新 Cookie 并重新生成 litellm_config.yaml，
#   【不会】自动重启代理。看到「Cookie refreshed」提示后，请 Ctrl+C 停掉本脚本再重跑。
#   若你不需要后台刷新，把这个 job 去掉、改为手动重跑 run.ps1 即可（见 README）。
$refreshJob = Start-Job -Name "UJN-Cookie-Refresh" -ScriptBlock {
    param($ProjectDir, $RefreshSeconds, $MaxLoginAttempts, $Port)

    Set-Location $ProjectDir
    $env:NO_PROXY = "localhost,127.0.0.1"
    $env:no_proxy = "localhost,127.0.0.1"
    $env:PYTHONIOENCODING = "utf-8"

    while ($true) {
        Start-Sleep -Seconds $RefreshSeconds
        for ($attempt = 1; $attempt -le $MaxLoginAttempts; $attempt++) {
            Write-Output "Scheduled WebVPN refresh attempt $attempt/$MaxLoginAttempts..."
            & uv run python .\ujn_webvpn_login.py --headless
            if ($LASTEXITCODE -eq 0) {
                & uv run python .\build_litellm_config.py
                Write-Output "Cookie refreshed and config regenerated. Restart the proxy to apply."
                break
            }
            if ($attempt -lt $MaxLoginAttempts) { Start-Sleep -Seconds 5 }
        }
    }
} -ArgumentList $ProjectDir, $RefreshSeconds, $MaxLoginAttempts, $Port

Write-Host "Started refresh job: $($refreshJob.Id)"
Write-Host "Starting LiteLLM proxy on http://127.0.0.1:$Port"
Write-Host "NOTE: config is read once at startup. After a cookie refresh, restart this script."
Write-Host "Press Ctrl+C to stop."

try {
    & uv run litellm --config "$ProjectDir\litellm_config.yaml" --port $Port --host 127.0.0.1
}
finally {
    Stop-Job -Id $refreshJob.Id -ErrorAction SilentlyContinue
    Remove-Job -Id $refreshJob.Id -Force -ErrorAction SilentlyContinue
}
