param(
    [int] $Port = 4000,
    [int] $ProbeSeconds = 60,
    [int] $RefreshSeconds = 1800,
    [int] $MaxLoginAttempts = 3
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

# 本地直连，避免系统代理接管 127.0.0.1
#
# 关键：必须把 .ujn.edu.cn 也加进去。若 profile 里设了 HTTP_PROXY/HTTPS_PROXY
# （FlClash 等），LiteLLM 会继承它并把【所有】上游请求发给那个代理；
# 代理软件一关就变成死地址，上游全部 500。实测：
#   NO_PROXY=localhost,127.0.0.1                  -> 死代理时 HTTP 500
#   NO_PROXY=localhost,127.0.0.1,.ujn.edu.cn      -> 死代理时 HTTP 200
# webvpn 是 202.194.65.6（国内教育网），实测直连 0.2s，不需要代理。
#
# 用赋值会盖掉用户 profile 里已有的 NO_PROXY，所以先并入既有值再写回。
#
# 注意：Windows 环境变量【不区分大小写】，NO_PROXY 与 no_proxy 是同一个变量，
# 设一次即可 —— 不要再写 $env:no_proxy = ... （那是 Linux/macOS 的习惯，
# 那边两个拼写才是独立的；在 Windows 上纯属冗余）。
$parts = @("localhost", "127.0.0.1", "::1", ".ujn.edu.cn")
foreach ($existing in @($env:NO_PROXY)) {
    if ($existing) {
        foreach ($item in ($existing -split ",")) {
            $trimmed = $item.Trim()
            if ($trimmed -and ($parts -notcontains $trimmed)) { $parts += $trimmed }
        }
    }
}
$env:NO_PROXY = $parts -join ","
$env:PYTHONIOENCODING = "utf-8"

$script:ProxyProcess = $null
$script:BackoffSeconds = 0

function Write-Stamp {
    param([string] $Message)
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $Message"
}

function Invoke-WebVpnLogin {
    param([int] $MaxAttempts = 3)

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        & uv run python .\ujn_webvpn_login.py --headless
        if ($LASTEXITCODE -eq 0) { return $true }
        Write-Warning "  WebVPN 登录失败（退出码 $LASTEXITCODE），第 $attempt/$MaxAttempts 次"
        if ($attempt -lt $MaxAttempts) { Start-Sleep -Seconds 5 }
    }
    return $false
}

function Update-LiteLlmConfig {
    & uv run python .\build_litellm_config.py
    return ($LASTEXITCODE -eq 0)
}

function Get-ProbeModel {
    # 从 models.yaml 取【全部】模型名，用于轮询探测。
    #
    # 为什么探测全部而不是像以前那样只取第一个（2026-09-19 实测）：
    # LiteLLM 的冷却按 deployment 独立计算，而这里每个模型各自只有一个
    # deployment。只探测模型 A 时，模型 B 冷却完全看不见 —— 守护报 ok，
    # 而客户端正在全挂。这正是 14:05-14:33 那批报错的成因：
    # 探测用的 1.Qwen3.5-27B 一切正常，出问题的却是 deepseek-v41-flash。
    (Select-String -Path (Join-Path $ProjectDir "models.yaml") `
                   -Pattern '^\s*-\s*(\S.*?)\s*$' -ErrorAction SilentlyContinue |
     ForEach-Object { $_.Matches[0].Groups[1].Value.Trim('"', "'") })
}

function Start-Proxy {
    $litellm = Join-Path $ProjectDir ".venv\Scripts\litellm.exe"
    if (-not (Test-Path $litellm)) { throw "找不到 $litellm，先跑 uv sync" }
    $cfg = Join-Path $ProjectDir "litellm_config.yaml"

    # 直接跑 venv 里的 litellm.exe，不经 uv：uv 会多一层父进程，
    # 杀父进程时子进程可能变孤儿继续占着端口。
    $script:ProxyProcess = Start-Process -FilePath $litellm `
        -ArgumentList @("--config", $cfg, "--port", "$Port", "--host", "127.0.0.1") `
        -PassThru -NoNewWindow
}

function Stop-Proxy {
    if ($script:ProxyProcess -and -not $script:ProxyProcess.HasExited) {
        Stop-Process -Id $script:ProxyProcess.Id -Force -ErrorAction SilentlyContinue
        $script:ProxyProcess.WaitForExit(10000) | Out-Null
    }
    $script:ProxyProcess = $null
}

function Wait-ProxyUp {
    param([int] $TimeoutSec = 90)

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health/liveliness" `
                                   -TimeoutSec 5 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Test-ProxyUpstream {
    <#
      探测运行中的代理能否真的打到上游。返回值：
        ok       正常
        dead     连不上/超时 —— 代理进程挂了或网络不通（无状态码）
        session  401/403/502/503，或「500 + 响应体是登录页 HTML」
        cooldown 429 —— LiteLLM 路由器把该模型打入冷却，该模型全线不可用
        config   400 —— 多半是探测用的模型名有问题，重启无用
        unknown  其它

      【为什么用流式探测】（实测 2026-09-19，隔离实验）：上游返回登录页 HTML 时，
        非流式 -> 500（下面的判据能认出来）
        流式   -> 429 "No deployments available for selected model" + cooldown_list=[...]
      两条路径的失败表现【不同】。Claude Code 全程走流式，所以只探非流式会
      在客户端已经全挂时仍然报 ok。这里用 stream=$true，让探测与真实客户端同路。

      为什么要看 500 的响应体（实测 2026-09-16）：WebVPN/ChatUJN 会话失效时
      上游返回的是登录页 HTML，LiteLLM 解析 JSON 失败后对外表现为 500 ——
      状态码上与"上游内部错误"无法区分。不区分的话会话失效型 500 落进
      unknown，守护什么都不做，代理永远坏着；但也不能见 500 就重登
      （上游偶发内部错误很常见，会陷入重启风暴）。
      判据：响应体含 <!DOCTYPE html（登录页必然是 HTML，JSON 错误不会是）。

      dead / session 都会触发重启（代理进程本身可能是坏的），
      但【只有 session 会重新登录换凭据】—— 见守护循环里的说明。
      配置类问题（config）不重启，避免陷入重启风暴。
    #>
    param([string] $Model, [int] $TimeoutSec = 45)

    if (-not $Model) { return "config" }

    $body = @{
        model      = $Model
        max_tokens = 1
        stream     = $true
        messages   = @(@{ role = "user"; content = "ping" })
    } | ConvertTo-Json -Depth 5

    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/chat/completions" `
            -Method POST -Body $body -ContentType "application/json" `
            -TimeoutSec $TimeoutSec -UseBasicParsing
        if ($resp.StatusCode -eq 200) { return "ok" }
        return "unknown"
    }
    catch {
        $status = $null
        if ($_.Exception.Response) {
            try { $status = [int] $_.Exception.Response.StatusCode } catch { }
        }
        if ($null -eq $status) { return "dead" }
        if ($status -eq 400) { return "config" }
        if ($status -eq 401 -or $status -eq 403 -or $status -eq 502 -or $status -eq 503) {
            return "session"
        }
        if ($status -eq 429) { return "cooldown" }
        if ($status -eq 500) {
            # 会话失效型 500：上游把登录页 HTML 当响应体返回。
            #
            # 【必须读 ResponseStream 而不是 ErrorDetails.Message】——
            # 实测（mock 500+HTML）：ErrorDetails.Message 是 PowerShell 把 HTML
            # 解析后提取的【可见文本】（"tpass sso login required"），
            # <!DOCTYPE 等标签被剥掉了，用它匹配必然漏判。
            # ResponseStream 里才是原始 HTML。
            $respBody = ""
            try {
                $s = $_.Exception.Response.GetResponseStream()
                if ($s) {
                    $s.Position = 0
                    $reader = New-Object System.IO.StreamReader($s)
                    $respBody = $reader.ReadToEnd()
                }
            } catch { }
            if ($respBody -match "<!DOCTYPE html") { return "session" }
        }
        return "unknown"
    }
}

function Test-ProxyUpstreamAll {
    <#
      轮询 models.yaml 里的每个模型探测一次，把结果聚合【一个】状态字。

      聚合规则（严重度由高到低，返回最高的那个）：
        session  > dead > cooldown > config > unknown > ok

      为什么这样排序：
        - session 要重新登录，代价最高但最可能真修好，优先级最高。
        - dead 是进程/网络级，覆盖一切局部模型问题。
        - cooldown / config / unknown 都是「部分模型有问题」，
          只要还有模型是好的，就不该触发重启（避免重启风暴）。

      关键：把所有模型的探测结果各自计数，只有「一个 ok 都没有」时才
      把坏状态上报为故障。这样单个模型下线（上游常态）不会导致反复重启。
    #>
    param([int] $TimeoutSec = 45)

    $sawOk = $false
    $worst = "ok"
    $worstRank = 0

    foreach ($m in (Get-ProbeModel)) {
        if (-not $m) { continue }
        $s = Test-ProxyUpstream -Model $m -TimeoutSec $TimeoutSec
        if ($s -eq "ok") { $sawOk = $true }

        # 短路：dead / session 是【进程级 / 凭据级】的，对每个模型都一样，
        # 没必要再探测剩下的。这很重要：最坏情况（litellm 接受连接但上游挂起）
        # 每个探测要等满 TimeoutSec，逐个探完 6 个就是 270s，
        # 会让守护循环远超 ProbeSeconds 而漂移。
        # cooldown / config / unknown 是【单模型级】的，必须继续探完 ——
        # 别的模型可能还是好的（那就该报 ok）。
        if ($s -eq "dead" -or $s -eq "session") { return $s }

        $r = switch ($s) {
            "ok"       { 0 }
            "cooldown" { 3 }
            "config"   { 2 }
            default    { 1 }
        }
        if ($s -ne "ok" -and $r -gt $worstRank) { $worst = $s; $worstRank = $r }
    }

    if ($sawOk) {
        # 还有模型是好的 —— 上游和凭据都没问题，不管局部模型状态。
        return "ok"
    }
    if ($worstRank -eq 0) { return "config" }   # 一个模型名都没读到
    return $worst
}

function Get-PortOwner {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($conn) { return $conn.OwningProcess }
    return $null
}

function Test-IsOursProxy {
    <#
      判断占用端口的进程是不是本项目的 litellm。

      关键：把路径分隔符统一成正斜杠再比。实测命令行里两种混着出现：
        "...D:\git_software\UJN_LLM_API\.venv\Scripts\litellm.exe" --config D:/.../litellm_config.yaml
      直接拿 $ProjectDir（反斜杠）去 -like 匹配会看运气 —— 换个启动方式就误判。

      $ProjectDir 由脚本自身位置推出，所以每个用户各自正确，不是硬编码。
    #>
    param([int] $ProcessId)

    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }

    $norm = { param($s) ([string] $s).Replace("\", "/").ToLowerInvariant() }
    $cmd  = & $norm $proc.CommandLine
    $dir  = & $norm $ProjectDir

    return ($cmd -match "litellm") -and ($cmd.Contains($dir))
}

function Stop-ProcessOnPort {
    param([int] $ProcessId)
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Get-PortOwner)) { return $true }
    }
    return $false
}

function Resolve-PortConflict {
    <#
      端口被占用时给用户三个选择（默认清理）。

      残留的本项目代理很常见：上次 Ctrl+C 后子进程没死透、或直接关了终端窗口。
      那种情况直接清掉即可。

      但也可能是别的程序占着（实测：4001 可能被 QQ 占）。盲杀是灾难，
      所以即使默认清理，也要先把占用者是谁打印出来，让用户能看见自己在清什么。

      成功返回可用的端口号（可能因用户选择而改变）；放弃则 throw。
    #>
    $owner = Get-PortOwner
    if (-not $owner) { return $Port }

    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue
    $name = if ($proc) { $proc.Name } else { "<未知>" }
    $cmd  = if ($proc) { [string] $proc.CommandLine } else { "" }
    $ours = Test-IsOursProxy -ProcessId $owner

    Write-Host ""
    Write-Warning "端口 $Port 已被占用："
    Write-Host "    PID  : $owner"
    Write-Host "    进程 : $name"
    if ($cmd) { Write-Host "    命令 : $($cmd.Substring(0, [Math]::Min(110, $cmd.Length)))" }
    Write-Host "    归属 : $(if ($ours) { '本项目的代理（上次没退干净）' } else { '不是本项目 —— 清理有风险' })"
    Write-Host ""

    $hint = if ($ours) { "结束它并继续" } else { "结束它并继续（慎用：可能杀掉别的程序）" }
    if ($ours) {
        Write-Host "  [1] $hint  （默认，直接回车）"
    } else {
        Write-Host "  [1] $hint"
    }
    Write-Host "  [2] 换一个空闲端口运行"
    Write-Host "  [3] 退出，我自己处理"
    Write-Host ""

    $default = if ($ours) { "1" } else { "2" }
    $answer = Read-Host "选择 [1/2/3]（回车 = $default）"
    if ([string]::IsNullOrWhiteSpace($answer)) { $answer = $default }

    switch ($answer.Trim()) {
        "1" {
            Write-Stamp "正在结束 PID $owner ..."
            if (Stop-ProcessOnPort -ProcessId $owner) {
                Write-Stamp "端口 $Port 已释放"
                return $Port
            }
            throw "结束了 PID $owner，但端口 $Port 仍被占用。"
        }
        "2" {
            $newPort = Find-FreePort -StartAt ($Port + 1)
            if (-not $newPort) { throw "从 $($Port + 1) 起找不到空闲端口。" }
            Write-Stamp "改用端口 $newPort"
            Write-Warning "  注意：换了端口后，客户端的 Base URL 也要跟着改。"
            return $newPort
        }
        "3" {
            throw "已取消（端口 $Port 被 PID $owner 占用）。"
        }
        default {
            throw "无法识别的选择 '$answer'。"
        }
    }
}

function Find-FreePort {
    param([int] $StartAt, [int] $MaxTries = 50)
    for ($p = $StartAt; $p -lt ($StartAt + $MaxTries); $p++) {
        if (-not (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)) {
            return $p
        }
    }
    return $null
}

function Restart-Proxy {
    <#
      重启代理。
      $FreshCookie 为真时，先重新登录并重新生成配置再重启 —— 这一步是关键：
      只重启而不换 Cookie，等于拿同一份失效凭据反复重试，白重启。
    #>
    param([bool] $FreshCookie)

    if ($FreshCookie) {
        Write-Stamp "重新登录 WebVPN 以获取新 Cookie..."
        if (Invoke-WebVpnLogin -MaxAttempts $MaxLoginAttempts) {
            Write-Stamp "重新生成 litellm_config.yaml..."
            if (Update-LiteLlmConfig) {
                Write-Stamp "已拿到新 Cookie 并写入配置"
            } else {
                Write-Warning "  配置生成失败，将用旧配置重启"
            }
        } else {
            Write-Warning "  登录失败，将用旧 Cookie 重启（大概率仍不通）"
            $script:BackoffSeconds = [Math]::Min(($script:BackoffSeconds + 60), 600)
            Write-Warning "  退避到 $($script:BackoffSeconds)s，避免重启风暴"
        }
    }

    Stop-Proxy
    Start-Proxy
    if (Wait-ProxyUp) {
        Write-Stamp "代理已就绪: http://127.0.0.1:$Port"
        return $true
    }
    Write-Warning "  代理重启后仍未就绪"
    return $false
}

# ---------------------------------------------------------------- 启动

Write-Host "=== UJN LLM API ==="
Write-Host "项目目录 : $ProjectDir"
Write-Host "代理端口 : $Port"
Write-Host "探测间隔 : ${ProbeSeconds}s    Cookie 刷新间隔: ${RefreshSeconds}s"
Write-Host ""

$Port = Resolve-PortConflict

Write-Stamp "初次 WebVPN 登录..."
if (-not (Invoke-WebVpnLogin -MaxAttempts $MaxLoginAttempts)) {
    throw "初次 WebVPN 登录失败（试了 $MaxLoginAttempts 次）。检查 config.yaml 里的账号密码。"
}

Write-Stamp "生成 litellm_config.yaml..."
if (-not (Update-LiteLlmConfig)) { throw "生成 litellm_config.yaml 失败" }

$probeModels = @(Get-ProbeModel)
if ($probeModels.Count -eq 0) {
    Write-Warning "models.yaml 里没读到模型名 —— 健康探测会跳过重启逻辑。"
} else {
    Write-Stamp "探测用模型: 全部 $($probeModels.Count) 个（每轮逐个探测，聚合判定）"
}
# 注意：这里只是启动时的摘要。models.yaml 会被自动同步更新
# （build 脚本生成配置前会拉上游清单，上游随时可能下线模型），探测模型
# 若固定不重读，就会指向一个已下线的模型，探测永远 400 -> 守护误判。
# 所以守护循环里每轮探测前要重新取 Get-ProbeModel。

Write-Stamp "启动 LiteLLM 代理..."
Start-Proxy
if (-not (Wait-ProxyUp)) { throw "代理启动超时（$Port 没起来）" }
Write-Stamp "代理已就绪: http://127.0.0.1:$Port"
Write-Host ""
Write-Host "守护中：每 ${ProbeSeconds}s 探测一次；"
Write-Host "        只有上游返回 401/403/502/503 才重新登录换凭据，"
Write-Host "        网络不通只重启进程（不重新登录）；"
Write-Host "        每 ${RefreshSeconds}s 主动刷新一次 Cookie 与 JWT。Ctrl+C 退出。"
Write-Host ""

# ---------------------------------------------------------------- 守护循环

try {
    $sinceRefresh = 0

    while ($true) {
        Start-Sleep -Seconds $ProbeSeconds
        $sinceRefresh += $ProbeSeconds

        # 周期性主动刷新：只更新磁盘上的配置，不打断正在跑的请求。
        # 真正加载新 Cookie 要等下一次按需重启。
        # 登录脚本顺带会把 JWT 也重新掏一份写进 state 文件。
        if ($sinceRefresh -ge $RefreshSeconds) {
            $sinceRefresh = 0
            Write-Stamp "定期刷新 Cookie 与 JWT..."
            if (Invoke-WebVpnLogin -MaxAttempts $MaxLoginAttempts) {
                if (Update-LiteLlmConfig) {
                    Write-Stamp "  已更新 litellm_config.yaml（下次重启生效）"
                } else {
                    Write-Warning "  配置生成失败，保持现有配置"
                }
            } else {
                Write-Warning "  登录刷新失败，保持现有配置"
            }
        }

        if ($script:BackoffSeconds -gt 0) {
            Write-Warning "退避中，跳过本次探测（剩 $($script:BackoffSeconds)s）"
            $script:BackoffSeconds -= $ProbeSeconds
            if ($script:BackoffSeconds -lt 0) { $script:BackoffSeconds = 0 }
            continue
        }

        # 每轮重读：models.yaml 可能刚被自动同步改过（上游下线了探测模型时），
        # 固定用启动时那个值会让探测永远 400，守护误判成配置问题。
        # 探测【全部】模型：冷却按 deployment 独立计算，只探一个会漏掉
        # 其他模型的故障（见 Test-ProxyUpstreamAll 的说明）。
        $state = Test-ProxyUpstreamAll

        switch ($state) {
            "ok" {
                # 正常，什么都不做 —— 这就是「按需重启」：平时零中断。
            }
            "dead" {
                # 连不上/超时 = 代理进程挂了，与会话有效性无关。
                # 【不要】在这里重新登录：网络抖一下就跑一次浏览器登录既慢又没用，
                # 而且登录本身要联网，抖动期间大概率也失败，白白触发退避。
                # 只有 confirmed 401/403 的 "session" 才换凭据（见下）。
                Write-Warning "代理无响应（网络/进程），仅重启进程，不重新登录..."
                if (Restart-Proxy -FreshCookie $false) { $script:BackoffSeconds = 0 }
            }
            "session" {
                Write-Warning "WebVPN 会话失效，换新 Cookie 并重启..."
                if (Restart-Proxy -FreshCookie $true) {
                    $script:BackoffSeconds = 0
                    $sinceRefresh = 0
                }
            }
            "cooldown" {
                # 全部模型都处于 LiteLLM 冷却 —— 上游/凭据可能还有效，
                # 但路由器已把每个 deployment 打入冷却，客户端全线不可用。
                # 只重启进程即可让路由器状态清零，【不要】重新登录：
                # 冷却不代表凭据失效，重登既慢又可能再次失败而触发退避。
                Write-Warning "全部模型处于冷却（LiteLLM 路由器），重启进程以清空状态..."
                if (Restart-Proxy -FreshCookie $false) { $script:BackoffSeconds = 0 }
            }
            "config" {
                Write-Warning "探测请求被拒（可能是模型名问题），不重启。检查 models.yaml。"
            }
            default {
                Write-Warning "探测结果异常: $state"
            }
        }
    }
}
finally {
    Write-Host ""
    Write-Stamp "正在停止代理..."
    Stop-Proxy
    Write-Stamp "已退出。"
}
