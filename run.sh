#!/usr/bin/env sh
#
# UJN LLM API —— 启动并守护本地 LiteLLM 代理（macOS / Linux）。
#
# 这是 run.ps1 的逐项对等移植：守护策略、端口冲突处理、探测状态机、
# 退避逻辑全部一致。两处平台差异见下面 setup_no_proxy 与 stop_proxy 的注释。
#
# 只用 POSIX sh 语法，是刻意的：macOS 自带 bash 还是 2007 年的 3.2
# （没有 mapfile / 关联数组），而 zsh 用户的默认 shell 是 zsh。
# 写成 POSIX 子集，sh / bash / zsh / dash 下都是同一份行为：
#     ./run.sh          # 任何 shell 下都能跑
#     zsh run.sh        # 也可以
#
# 因此也刻意【不用】set -e：它遇上 trap 与命令替换会静默改变行为，
# 排查起来比省下的几行判断贵得多。失败一律显式判断，走 die()。

set -u

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$PROJECT_DIR" || exit 1

PORT="${UJN_PORT:-4000}"
PROBE_SECONDS="${UJN_PROBE_SECONDS:-60}"
REFRESH_SECONDS="${UJN_REFRESH_SECONDS:-1800}"
MAX_LOGIN_ATTEMPTS="${UJN_MAX_LOGIN_ATTEMPTS:-3}"

PROBE_TIMEOUT="${UJN_PROBE_TIMEOUT:-45}"
WAIT_PROXY_TIMEOUT=90

LITELLM="$PROJECT_DIR/.venv/bin/litellm"
LITELLM_CONFIG="$PROJECT_DIR/litellm_config.yaml"
MODELS_FILE="$PROJECT_DIR/models.yaml"
LOGIN_SCRIPT="$PROJECT_DIR/ujn_webvpn_login.py"
BUILD_SCRIPT="$PROJECT_DIR/build_litellm_config.py"

PROXY_PID=""
BACKOFF_SECONDS=0

PROBE_BODY=$(mktemp "${TMPDIR:-/tmp}/ujn_probe.XXXXXX") || {
    printf '错误: 无法创建临时文件\n' >&2
    exit 1
}


# --- 基础输出 ---------------------------------------------------------------

stamp() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
warn()  { printf '警告: %s\n' "$*" >&2; }

die() {
    printf '错误: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
用法: ./run.sh [--help]

启动 UJN LLM API 本地代理（LiteLLM），并守护它：
探测上游是否还能打通，必要时自动重新登录换凭据并重启代理。

环境变量（都有默认值，等价于 run.ps1 的同名参数）:
  UJN_PORT                 代理监听端口            (默认 4000)
  UJN_PROBE_SECONDS        上游探测间隔（秒）       (默认 60)
  UJN_REFRESH_SECONDS      主动刷新 Cookie 间隔（秒）(默认 1800)
  UJN_MAX_LOGIN_ATTEMPTS   每次登录最多尝试几次      (默认 3)
  UJN_PROBE_TIMEOUT        单次上游探测超时（秒）    (默认 45)

例:
  ./run.sh
  UJN_PORT=4001 ./run.sh

退出: Ctrl+C 会连同代理一起收掉。
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        *) die "无法识别的参数 '$1'（试试 --help）" ;;
    esac
done


# --- 环境 -------------------------------------------------------------------

setup_no_proxy() {
    # 本地直连，避免系统代理接管 127.0.0.1
    #
    # 关键：必须把 .ujn.edu.cn 也加进去。若 profile 里设了 HTTP_PROXY/HTTPS_PROXY
    # （FlClash 等），LiteLLM 会继承它并把【所有】上游请求发给那个代理；
    # 代理软件一关就变成死地址，上游全部 500。实测：
    #   NO_PROXY=localhost,127.0.0.1                  -> 死代理时 HTTP 500
    #   NO_PROXY=localhost,127.0.0.1,.ujn.edu.cn      -> 死代理时 HTTP 200
    # webvpn 是 202.194.65.6（国内教育网），实测直连 0.2s，不需要代理。
    #
    # 用赋值会盖掉用户 profile 里已有的值，所以先并入既有值再写回。
    #
    # 【与 Windows 的关键差异】Linux/macOS 上 NO_PROXY 与 no_proxy 是两个
    # 【互相独立】的变量，各库认哪个不一定（curl 只认小写，Python requests
    # 两个都认）。所以两个都要设。Windows 环境变量不分大小写，设一次即可 ——
    # run.ps1 里那句"不要再写 no_proxy"的注释只对 Windows 成立。
    _merged="localhost,127.0.0.1,::1,.ujn.edu.cn"

    for _existing in "${NO_PROXY:-}" "${no_proxy:-}"; do
        [ -n "$_existing" ] || continue
        _rest="$_existing"
        # 按逗号切分。用 case 而不是数组 —— POSIX sh 没有数组。
        while [ -n "$_rest" ]; do
            case "$_rest" in
                *,*) _item="${_rest%%,*}"; _rest="${_rest#*,}" ;;
                *)   _item="$_rest";       _rest="" ;;
            esac
            _item=$(printf '%s' "$_item" | tr -d '[:space:]')
            [ -n "$_item" ] || continue
            case ",$_merged," in
                *",$_item,"*) ;;                                  # 已有，跳过
                *) _merged="$_merged,$_item" ;;
            esac
        done
    done

    NO_PROXY="$_merged"
    no_proxy="$_merged"
    export NO_PROXY no_proxy
    export PYTHONIOENCODING=utf-8
}


# --- 登录与配置生成 ---------------------------------------------------------

webvpn_login() {
    # 登录并保存会话；成功返回 0。最多试 $1 次。
    _max="$1"
    _attempt=1
    while [ "$_attempt" -le "$_max" ]; do
        uv run python "$LOGIN_SCRIPT" --headless
        _rc=$?
        [ "$_rc" -eq 0 ] && return 0
        warn "  WebVPN 登录失败（退出码 $_rc），第 $_attempt/$_max 次"
        [ "$_attempt" -lt "$_max" ] && sleep 5
        _attempt=$((_attempt + 1))
    done
    return 1
}

update_litellm_config() {
    uv run python "$BUILD_SCRIPT"
}

get_probe_model() {
    # 从 models.yaml 取第一个模型名，只用于探测上游是否还能打通。
    #
    # 这个值【必须每轮重读】，不能只在启动时取一次：models.yaml 会被自动
    # 同步更新（build 脚本生成配置前会拉上游清单，上游随时可能下线模型），
    # 一旦探测用的模型下线，探测就永远 400，守护会误判成配置问题而不再重启。
    [ -f "$MODELS_FILE" ] || return 1
    sed -n 's/^[[:space:]]*-[[:space:]]*\([^[:space:]].*\)$/\1/p' "$MODELS_FILE" \
        | head -n 1 \
        | sed 's/[[:space:]]*$//' \
        | tr -d "\"'"
}


# --- 代理进程 ---------------------------------------------------------------

start_proxy() {
    [ -x "$LITELLM" ] || die "找不到 $LITELLM，先跑 uv sync"

    # 直接跑 venv 里的 litellm，不经 uv：uv 会多一层父进程，
    # 杀父进程时子进程可能变孤儿继续占着端口。
    #
    # 不重定向输出：litellm 的报错要能立刻看见（对应的 run.ps1 用 -NoNewWindow）。
    "$LITELLM" --config "$LITELLM_CONFIG" --port "$PORT" --host 127.0.0.1 &
    PROXY_PID=$!
}

stop_proxy() {
    [ -n "$PROXY_PID" ] || return 0

    # 【与 Windows 的差异】这里不用 setsid 建独立进程组 —— macOS 默认
    # 没有 setsid，用不了 kill -- -PGID。改为杀 PID 本身，再轮询进程是否
    # 真的退出，赖着不走就 -9。
    #
    # 等的是【进程】而不是端口：端口可能被别的进程立刻接走，
    # 那样会一直等不到"空闲"而白耗到超时。
    kill "$PROXY_PID" 2>/dev/null || true

    _i=0
    while [ "$_i" -lt 10 ]; do
        kill -0 "$PROXY_PID" 2>/dev/null || break
        sleep 1
        _i=$((_i + 1))
    done

    if kill -0 "$PROXY_PID" 2>/dev/null; then
        kill -9 "$PROXY_PID" 2>/dev/null || true
    fi
    PROXY_PID=""
}

wait_proxy_up() {
    _deadline=$(( $(date +%s) + $1 ))
    while [ "$(date +%s)" -lt "$_deadline" ]; do
        _code=$(curl -s --noproxy '*' --max-time 5 -o /dev/null \
                     -w '%{http_code}' \
                     "http://127.0.0.1:$PORT/health/liveliness" 2>/dev/null) || _code=000
        [ "$_code" = "200" ] && return 0
        sleep 2
    done
    return 1
}

probe_upstream() {
    # 探测运行中的代理能否真的打到上游。打印状态字：
    #   ok      正常
    #   dead    连不上/超时 —— 代理进程挂了或网络不通（拿不到状态码）
    #   session 401/403/502/503，或「500 + 响应体是登录页 HTML」
    #   config  400 —— 多半是探测用的模型名有问题，重启无用
    #   unknown 其它
    #
    # 为什么要看 500 的响应体（实测 2026-09-16）：WebVPN/ChatUJN 会话失效时
    # 上游返回的是登录页 HTML，LiteLLM 解析 JSON 失败后对外表现为 500 ——
    # 状态码上与"上游内部错误"无法区分。不区分的话会话失效型 500 落进
    # unknown，守护什么都不做，代理永远坏着；但也不能见 500 就重登
    # （上游偶发内部错误很常见，会陷入重启风暴）。
    # 判据：响应体含 <!DOCTYPE html（登录页必然是 HTML，JSON 错误不会是）。
    #
    # dead / session 都会触发重启（代理进程本身可能是坏的），
    # 但【只有 session 会重新登录换凭据】—— 见守护循环里的说明。
    #
    # 比 run.ps1 简单的地方：curl 的 -o 直接拿到【原始】响应体，
    # 不存在 PowerShell 那个"ErrorDetails.Message 已被剥掉标签"的坑。
    _model="$1"
    [ -n "$_model" ] || { printf 'config'; return; }

    # --noproxy '*'：探测目标全是本机，绝不能被系统代理接管。
    _code=$(curl -s --noproxy '*' --max-time "$PROBE_TIMEOUT" \
                 -o "$PROBE_BODY" -w '%{http_code}' \
                 -X POST "http://127.0.0.1:$PORT/v1/chat/completions" \
                 -H 'Content-Type: application/json' \
                 -d "{\"model\":\"$_model\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}" \
                 2>/dev/null) || _code=000

    case "$_code" in
        200)             printf 'ok' ;;
        400)             printf 'config' ;;
        401|403|502|503) printf 'session' ;;
        500)
            if grep -q '<!DOCTYPE html' "$PROBE_BODY" 2>/dev/null; then
                printf 'session'
            else
                printf 'unknown'
            fi
            ;;
        000)             printf 'dead' ;;
        *)               printf 'unknown' ;;
    esac
}


# --- 端口占用 ---------------------------------------------------------------

port_owner() {
    # 打印占用 ${1:-$PORT} 的监听进程 PID；没有则什么都不打印。
    #
    # macOS 一定自带 lsof；Linux 上 lsof 可能没装（最小化安装常见），
    # 所以依次退到 ss、netstat。三个都没有就没法查，返回 1 ——
    # 调用方把"查不到"当"没占用"处理，跟只装了 curl 的最小环境一致。
    _p="${1:-$PORT}"

    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$_p" -sTCP:LISTEN -t 2>/dev/null | head -n 1
        return 0
    fi

    if command -v ss >/dev/null 2>&1; then
        ss -lntp 2>/dev/null \
            | awk -v port=":$_p" '$1 == "LISTEN" && $4 ~ port"$" { print $NF }' \
            | grep -o 'pid=[0-9]*' | head -n 1 | cut -d= -f2
        return 0
    fi

    if command -v netstat >/dev/null 2>&1; then
        netstat -lntp 2>/dev/null \
            | awk -v port=":$_p" '$4 ~ port"$" && $NF ~ /^[0-9]+\// { print $NF }' \
            | head -n 1 | cut -d/ -f1
        return 0
    fi

    return 1
}

is_ours_proxy() {
    # 判断 PID $1 是不是本项目的 litellm。
    #
    # 比【命令行】而不是进程名：litellm 只是 python 脚本的入口，
    # 光看进程名区分不出是哪个项目的。
    #
    # 大小写归一后再比：macOS 文件系统大小写不敏感，用户可能用不同大小写
    # 的路径启动（对应的 run.ps1 还要额外把反斜杠换成正斜杠）。
    # PROJECT_DIR 由脚本自身位置推出，所以每个用户各自正确，不是硬编码。
    _pid="$1"
    _cmd=$(ps -p "$_pid" -o command= 2>/dev/null) || return 1
    [ -n "$_cmd" ] || return 1

    case "$_cmd" in
        *litellm*) ;;
        *) return 1 ;;
    esac

    _needle=$(printf '%s' "$PROJECT_DIR" | tr '[:upper:]' '[:lower:]')
    printf '%s' "$_cmd" | tr '[:upper:]' '[:lower:]' | grep -qF "$_needle"
}

stop_process_on_port() {
    _pid="$1"
    kill "$_pid" 2>/dev/null || true

    _i=0
    while [ "$_i" -lt 8 ]; do
        sleep 1
        [ -z "$(port_owner)" ] && return 0
        _i=$((_i + 1))
    done

    kill -9 "$_pid" 2>/dev/null || true
    sleep 1
    [ -z "$(port_owner)" ]
}

find_free_port() {
    _p="$1"
    _max=$((_p + 50))
    while [ "$_p" -lt "$_max" ]; do
        [ -z "$(port_owner "$_p")" ] && { printf '%s' "$_p"; return 0; }
        _p=$((_p + 1))
    done
    return 1
}

resolve_port_conflict() {
    # 端口被占用时给用户三个选择（默认"清理"仅当占用者是本项目时）。
    #
    # 残留的本项目代理很常见：上次 Ctrl+C 后子进程没死透、或直接关了终端窗口。
    # 那种情况直接清掉即可。
    #
    # 但也可能是别的程序占着（实测：4001 可能被 QQ 占）。盲杀是灾难，
    # 所以即使默认清理，也要先把占用者是谁打印出来，让用户能看见自己在清什么。
    _owner=$(port_owner)
    [ -n "$_owner" ] || return 0

    _name=$(ps -p "$_owner" -o comm= 2>/dev/null)
    _cmd=$(ps -p "$_owner" -o command= 2>/dev/null)
    if is_ours_proxy "$_owner"; then _ours=1; else _ours=0; fi

    printf '\n'
    warn "端口 $PORT 已被占用："
    printf '    PID  : %s\n' "$_owner"
    printf '    进程 : %s\n' "${_name:-<未知>}"
    [ -n "$_cmd" ] && printf '    命令 : %s\n' "$(printf '%s' "$_cmd" | cut -c1-110)"
    if [ "$_ours" -eq 1 ]; then
        printf '    归属 : 本项目的代理（上次没退干净）\n'
    else
        printf '    归属 : 不是本项目 —— 清理有风险\n'
    fi
    printf '\n'

    if [ "$_ours" -eq 1 ]; then
        printf '  [1] 结束它并继续  （默认，直接回车）\n'
        _default=1
    else
        printf '  [1] 结束它并继续（慎用：可能杀掉别的程序）\n'
        _default=2
    fi
    printf '  [2] 换一个空闲端口运行\n'
    printf '  [3] 退出，我自己处理\n\n'

    # 非交互（stdin 不是终端）时不卡在 read 上，直接用默认值。
    _answer=""
    if [ -t 0 ]; then
        printf '选择 [1/2/3]（回车 = %s）: ' "$_default"
        read -r _answer || _answer=""
    fi
    [ -n "$_answer" ] || _answer="$_default"
    _answer=$(printf '%s' "$_answer" | tr -d '[:space:]')

    case "$_answer" in
        1)
            stamp "正在结束 PID $_owner ..."
            if stop_process_on_port "$_owner"; then
                stamp "端口 $PORT 已释放"
                return 0
            fi
            die "结束了 PID $_owner，但端口 $PORT 仍被占用（可能需要 sudo）。"
            ;;
        2)
            _new=$(find_free_port $((PORT + 1)))
            [ -n "$_new" ] || die "从 $((PORT + 1)) 起找不到空闲端口。"
            PORT="$_new"
            stamp "改用端口 $PORT"
            warn "  注意：换了端口后，客户端的 Base URL 也要跟着改。"
            ;;
        3)
            die "已取消（端口 $PORT 被 PID $_owner 占用）。"
            ;;
        *)
            die "无法识别的选择 '$_answer'。"
            ;;
    esac
}


# --- 重启 -------------------------------------------------------------------

restart_proxy() {
    # 重启代理。$1 为 1 时，先重新登录并重新生成配置再重启 —— 这一步是关键：
    # 只重启而不换 Cookie，等于拿同一份失效凭据反复重试，白重启。
    if [ "$1" -eq 1 ]; then
        stamp "重新登录 WebVPN 以获取新 Cookie..."
        if webvpn_login "$MAX_LOGIN_ATTEMPTS"; then
            stamp "重新生成 litellm_config.yaml..."
            if update_litellm_config; then
                stamp "已拿到新 Cookie 并写入配置"
            else
                warn "  配置生成失败，将用旧配置重启"
            fi
        else
            warn "  登录失败，将用旧 Cookie 重启（大概率仍不通）"
            BACKOFF_SECONDS=$((BACKOFF_SECONDS + 60))
            [ "$BACKOFF_SECONDS" -gt 600 ] && BACKOFF_SECONDS=600
            warn "  退避到 ${BACKOFF_SECONDS}s，避免重启风暴"
        fi
    fi

    stop_proxy
    start_proxy
    if wait_proxy_up "$WAIT_PROXY_TIMEOUT"; then
        stamp "代理已就绪: http://127.0.0.1:$PORT"
        return 0
    fi
    warn "  代理重启后仍未就绪"
    return 1
}


# --- 退出清理 ---------------------------------------------------------------

cleanup() {
    trap - INT TERM HUP

    printf '\n'
    stamp "正在停止代理..."
    stop_proxy
    rm -f "$PROBE_BODY" 2>/dev/null || true
    stamp "已退出。"
    exit 130
}


# --- 守护循环 ---------------------------------------------------------------

guard_loop() {
    _since_refresh=0

    while :; do
        sleep "$PROBE_SECONDS"
        _since_refresh=$((_since_refresh + PROBE_SECONDS))

        # 周期性主动刷新：只更新磁盘上的配置，不打断正在跑的请求。
        # 真正加载新 Cookie 要等下一次按需重启。
        # 登录脚本顺带会把 JWT 也重新掏一份写进 state 文件并回填 config.yaml。
        if [ "$_since_refresh" -ge "$REFRESH_SECONDS" ]; then
            _since_refresh=0
            stamp "定期刷新 Cookie 与 JWT..."
            if webvpn_login "$MAX_LOGIN_ATTEMPTS"; then
                if update_litellm_config; then
                    stamp "  已更新 litellm_config.yaml（下次重启生效）"
                else
                    warn "  配置生成失败，保持现有配置"
                fi
            else
                warn "  登录刷新失败，保持现有配置"
            fi
        fi

        if [ "$BACKOFF_SECONDS" -gt 0 ]; then
            warn "退避中，跳过本次探测（剩 ${BACKOFF_SECONDS}s）"
            BACKOFF_SECONDS=$((BACKOFF_SECONDS - PROBE_SECONDS))
            if [ "$BACKOFF_SECONDS" -lt 0 ]; then BACKOFF_SECONDS=0; fi
            continue
        fi

        # 每轮重读：models.yaml 可能刚被自动同步改过（上游下线了探测模型时），
        # 固定用启动时那个值会让探测永远 400，守护误判成配置问题。
        _model=$(get_probe_model)
        _state=$(probe_upstream "$_model")

        case "$_state" in
            ok)
                # 正常，什么都不做 —— 这就是「按需重启」：平时零中断。
                ;;
            dead)
                # 连不上/超时 = 代理进程挂了，与会话有效性无关。
                # 【不要】在这里重新登录：网络抖一下就跑一次浏览器登录既慢又没用，
                # 而且登录本身要联网，抖动期间大概率也失败，白白触发退避。
                # 只有确认的 401/403 等 "session" 才换凭据（见下）。
                warn "代理无响应（网络/进程），仅重启进程，不重新登录..."
                if restart_proxy 0; then BACKOFF_SECONDS=0; fi
                ;;
            session)
                warn "WebVPN 会话失效，换新 Cookie 并重启..."
                if restart_proxy 1; then
                    BACKOFF_SECONDS=0
                    _since_refresh=0
                fi
                ;;
            config)
                warn "探测请求被拒（可能是模型名问题），不重启。检查 models.yaml。"
                ;;
            *)
                warn "探测结果异常: $_state"
                ;;
        esac
    done
}


# --- 启动 -------------------------------------------------------------------

main() {
    command -v uv >/dev/null 2>&1 \
        || die "找不到 uv。先装 uv（https://docs.astral.sh/uv/），再跑 uv sync。"
    command -v curl >/dev/null 2>&1 \
        || die "找不到 curl。探测上游状态要用它，先装上。"

    setup_no_proxy
    trap cleanup INT TERM HUP

    printf '=== UJN LLM API ===\n'
    printf '项目目录 : %s\n' "$PROJECT_DIR"
    printf '代理端口 : %s\n' "$PORT"
    printf '探测间隔 : %ss    Cookie 刷新间隔: %ss\n' "$PROBE_SECONDS" "$REFRESH_SECONDS"
    printf '\n'

    resolve_port_conflict

    stamp "初次 WebVPN 登录..."
    webvpn_login "$MAX_LOGIN_ATTEMPTS" \
        || die "初次 WebVPN 登录失败（试了 $MAX_LOGIN_ATTEMPTS 次）。检查 config.yaml 里的账号密码。"

    stamp "生成 litellm_config.yaml..."
    update_litellm_config || die "生成 litellm_config.yaml 失败"

    if [ -n "$(get_probe_model)" ]; then
        stamp "探测用模型: $(get_probe_model)"
    else
        warn "models.yaml 里没读到模型名 —— 健康探测会跳过重启逻辑。"
    fi

    stamp "启动 LiteLLM 代理..."
    start_proxy
    wait_proxy_up "$WAIT_PROXY_TIMEOUT" || die "代理启动超时（$PORT 没起来）"
    stamp "代理已就绪: http://127.0.0.1:$PORT"
    printf '\n'
    printf '守护中：每 %ss 探测一次；\n' "$PROBE_SECONDS"
    printf '        只有上游返回 401/403/502/503 才重新登录换凭据，\n'
    printf '        网络不通只重启进程（不重新登录）；\n'
    printf '        每 %ss 主动刷新一次 Cookie 与 JWT。Ctrl+C 退出。\n' "$REFRESH_SECONDS"
    printf '\n'

    guard_loop
}

main
