# reasoning_effort 双客户端兼容 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Claude Code（Anthropic 协议）与 Codex（Responses 协议）都能自由使用各自的 effort 档位，不再因 `reasoning_effort` 取值不被上游接受而 400。

**Architecture:** 在 `build_litellm_config.py` 生成配置时，为每个模型部署写入 `additional_drop_params: ["reasoning_effort"]`，让 LiteLLM 在转发到上游前丢弃该字段。上游（vLLM）随后使用模型自身的默认推理强度。这样两个客户端送来的任何 effort 形态都不再影响连通性。代价是 effort 档位失去实际调节作用 —— 这是实测权衡的结果，见下方「为什么是丢弃」。

**Tech Stack:** Python 3.12 · uv · LiteLLM Proxy 1.100.1 · pytest

**Spec:** 无独立 spec。本计划依据 2026-09-14 的实测结论编写，证据见下方「实测依据」。

## Global Constraints

- 旧项目 `D:\git_software\UJNAPI` **不得修改**（它为当前会话提供 token 服务）
- `config.yaml`、`litellm_config.yaml`、`ujn_webvpn_state.json` 必须保持被 `.gitignore` 排除
- 凭据（WebVPN Cookie、UJN API Key）**不得写入任何被 git 追踪的文件**
- 生成的 `litellm_config.yaml` 必须是**纯 ASCII**（LiteLLM 用 GBK 读取，非 ASCII 会启动崩溃）
- `run.ps1` 必须保存为 **UTF-8 带 BOM**（PowerShell 5.1 对无 BOM 的中文会报语法错误）
- 所有 Python 代码走 `uv run`，Python 3.12
- 测试放在 `tests/`，用 pytest 运行：`uv run pytest tests/ -q`

## 实测依据（2026-09-14）

**上游 vLLM 直连，`reasoning_effort` 的接受面（HTTP 状态）：**

| 模型 | none | low | medium | high | xhigh | max |
|---|---|---|---|---|---|---|
| `deepseek-v41-flash` | 200 | 200 | **400** | 200 | 200 | 200 |
| `GLM-5.3-Flash` | 200 | 200 | 200 | 200 | 200 | 200 |
| `GLM-5.3` | 200 | 200 | 200 | 200 | 200 | 200 |
| `deepseek-v4-flash` | 200 | 200 | 200 | 200 | 200 | 200 |

上游对 deepseek 的拒绝信息：

```
400: DeepSeek V4.1 reasoning_effort must be low, high, xhigh, max, or an integer within [1, 100]
```

**Codex 的形态会额外触发错误。** Codex 发 `reasoning: {"effort": ..., "summary": ...}`。
LiteLLM 在 `litellm/responses/litellm_completion_transformation/transformation.py:313` 处，
当 `reasoning_param` 含 `summary` 键时，**把整个 dict 赋给 `reasoning_effort`**：

```python
if "summary" in reasoning_param:
    reasoning_effort = reasoning_param      # <- 传下去的是 dict，不是字符串
```

上游收到 dict 后报：

```
{'type': 'literal_error', 'loc': 'body.reasoning_effort',
 'msg': "Input should be 'none', 'minimal', 'low', 'medium', 'high', 'xhigh' or 'max'",
 'input': '<dict of 2 items>'}
```

**Claude Code 的 effort 走另一条路**，不进 `reasoning_effort`：它在 `output_config.effort` 里，
经 `litellm/llms/anthropic/chat/transformation.py:1579` 映射。实测 `output_config.effort`
取 `low/medium/high/xhigh/max` 全部 200，**Claude Code 侧本来就是通的**。

**修复验证（4003 端口，`additional_drop_params: ["reasoning_effort"]`）：**

| 请求形态 | 4000（现状） | 4003（加 drop） |
|---|---|---|
`reasoning={effort:medium}` | 400 | **200** |
`reasoning={effort:high,summary:auto}` | 400 | **200** |
`reasoning={effort:high,summary:detailed}` | 400 | **200** |
`output_config.effort=high` | 200 | 200 |
thinking + effort（Claude Code 完整形态） | 200 | 200 |

**为什么不改成「映射 effort 取值」而是直接丢弃：**

实测丢弃 effort 前后的生成质量（同题、3 次采样）：

```
传 effort=high    : 1.5s / out=129 / reasoning=113 chars
                    1.8s / out=170 / reasoning=151 chars
丢弃（模型默认）  : 2.1s / out=145 / reasoning=143 chars
                    1.8s / out=171 / reasoning=146 chars
                    1.3s / out=121 / reasoning=103 chars
```

输出长度与 reasoning 长度落在同一区间，**没有可观测的质量损失**。上游默认已在推理。
而「映射」方案需要维护一张 客户端取值 → 上游取值 的换算表，且 `summary` 子字段仍需单独剥离，
出错面更大。用户明确要求「两种都要支持」——丢弃是同时满足两者的最小改动。

---

## File Structure

| 文件 | 职责 | 本计划中的改动 |
|---|---|---|
| `build_litellm_config.py` | 由 config.yaml + state + models.yaml 生成 LiteLLM 配置 | 在 `render_config` 的每个部署里加 `additional_drop_params` |
| `tests/test_build_config.py` | 配置生成的单元测试 | 新增 4 个测试 |

`run.ps1`、`ujn_webvpn_login.py`、`README.md`、`docs/客户端配置指南.md` 本计划**不改**。

---

### Task 1: 在生成的配置里丢弃 reasoning_effort

**Files:**
- Modify: `build_litellm_config.py:243`（`render_config` 内 `litellm_params` 字典字面量的
  `"extra_headers": dict(headers),` 之后；该函数定义在 213 行）
- Test: `tests/test_build_config.py`（文件末尾追加）

**Interfaces:**
- Consumes: 无（`render_config` 已存在，签名为 `render_config(settings: dict[str, str], cookie_header: str, models: list[str]) -> str`）
- Produces: `render_config` 的输出中，每个 `model_list` 条目的 `litellm_params` 新增
  `additional_drop_params`，类型 `list[str]`，值 `["reasoning_effort"]`。后续再无其他任务消费此产物。

- [ ] **Step 1: 写失败的测试**

在 `tests/test_build_config.py` 末尾追加：

```python


# --- reasoning_effort 必须被丢弃（双客户端兼容）----------------------------
# 背景：上游对 effort 取值挑食（deepseek 拒绝 medium），且 Codex 的
# reasoning={effort,summary} 会被 LiteLLM 整份dict塞进 reasoning_effort。
# 丢弃它，让上游用自己的默认值 —— 实测无质量损失（见计划文档）。

def test_render_config_drops_reasoning_effort_on_every_deployment():
    """每个部署都要 drop：漏掉任何一个，那个模型的 medium 档仍会 400。"""
    config = yaml.safe_load(render_config(SETTINGS, "a=b", ["GLM-5.3", "deepseek-v41-flash"]))

    assert config["model_list"], "model_list 不应为空"
    for entry in config["model_list"]:
        assert entry["litellm_params"]["additional_drop_params"] == ["reasoning_effort"], (
            f"{entry['model_name']} 缺少 additional_drop_params"
        )


def test_render_config_drop_params_is_a_list_not_a_string():
    """LiteLLM 要求 list；写成字符串会被静默忽略，等于没修。"""
    config = yaml.safe_load(render_config(SETTINGS, "a=b", ["GLM-5.3"]))

    drop = config["model_list"][0]["litellm_params"]["additional_drop_params"]
    assert isinstance(drop, list)
    assert all(isinstance(x, str) for x in drop)


def test_render_config_drop_params_stays_per_deployment():
    """drop 必须落在每个部署的 litellm_params 里，不能提到全局 litellm_settings。

    背景：litellm_settings 是全局的；把 drop 放那儿会影响所有部署，
    将来若某个模型需要保留 effort 就没有回旋余地。
    """
    config = yaml.safe_load(render_config(SETTINGS, "a=b", ["GLM-5.3"]))

    assert "additional_drop_params" not in config["litellm_settings"]
    assert config["model_list"][0]["litellm_params"]["additional_drop_params"] == ["reasoning_effort"]


def test_render_config_still_pure_ascii_with_drop_params():
    """回归：新增字段不得引入非 ASCII（LiteLLM 用 GBK 读该文件）。"""
    out = render_config(SETTINGS, "a=b", ["GLM-5.3", "/models/Qwen3.8-Flash-Next"])

    non_ascii = [c for c in out if ord(c) > 127]
    assert not non_ascii, f"config must be ASCII-only, found {non_ascii[:5]}"
```

> 注：两个 bridge 开关（`use_chat_completions_api` 与
> `use_chat_completions_url_for_anthropic_messages`）已由既有测试
> `test_render_config_sets_both_bridge_flags` 覆盖，本任务不重复断言；
> Step 4 跑全量套件时会一并验证它们没被挤掉。

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_build_config.py::test_render_config_drops_reasoning_effort_on_every_deployment tests/test_build_config.py::test_render_config_drop_params_is_a_list_not_a_string tests/test_build_config.py::test_render_config_drop_params_stays_per_deployment -q`

Expected: 3 个都 FAIL，报 `KeyError: 'additional_drop_params'`。
（第 4 个新测试 `test_render_config_still_pure_ascii_with_drop_params` 此时应已通过 —— 字段还没加，自然纯 ASCII。单独跑它确认通过：

Run: `uv run pytest tests/test_build_config.py::test_render_config_still_pure_ascii_with_drop_params -q`
Expected: PASS）

- [ ] **Step 3: 实现**

在 `build_litellm_config.py` 的 `render_config` 里，找到构造 `litellm_params` 的那段字典字面量
（含 `"model": f"hosted_vllm/{upstream}"` 与 `"use_chat_completions_api": True`），
在 `"extra_headers": dict(headers),` 之后加入一行：

```python
                    "extra_headers": dict(headers),
                    # 丢弃 reasoning_effort：上游对取值挑食（deepseek 拒绝 medium），
                    # 且 Codex 的 reasoning={effort,summary} 会被 LiteLLM 整份 dict
                    # 塞进 reasoning_effort，直接 400。丢弃后上游用自身默认推理强度 ——
                    # 实测无质量损失。这样 Claude Code 与 Codex 两种 effort 形态都能通。
                    "additional_drop_params": ["reasoning_effort"],
```

注意：`litellm_params` 是一个 dict 字面量，**保持缩进与相邻键一致**（20 个空格）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/ -q`

Expected: 全部 PASS（原 31 个 + 新增 4 个 = 35 个）

- [ ] **Step 5: 生成配置并确认字段真的写进去了**

Run:

```bash
uv run python build_litellm_config.py --out check_drop.yaml
uv run python -c "
import yaml
c = yaml.safe_load(open('check_drop.yaml', encoding='utf-8'))
for m in c['model_list']:
    dp = m['litellm_params'].get('additional_drop_params')
    print(' ', m['model_name'], '->', dp)
"
rm -f check_drop.yaml
```

Expected: 8 行输出，每行都是 `-> ['reasoning_effort']`

- [ ] **Step 6: 提交**

```bash
git add build_litellm_config.py tests/test_build_config.py
git commit -m "fix: drop reasoning_effort so Codex and Claude Code both work

The upstream rejects some reasoning_effort values (deepseek refuses
\"medium\"), and Codex's reasoning={effort,summary} shape gets the whole
dict assigned to reasoning_effort by
litellm/responses/litellm_completion_transformation/transformation.py:313,
which the upstream then rejects as a literal_error.

Add additional_drop_params: [\"reasoning_effort\"] to every deployment so
LiteLLM strips the field before forwarding; the upstream then uses its own
default effort. Measured no quality loss: output and reasoning lengths land
in the same range with and without the param.

This leaves Claude Code's output_config.effort path untouched, so both
clients' effort shapes now pass."
```

---

### Task 2: 端到端验证两种客户端形态

**Files:**
- 无文件改动（纯验证任务）
- 只读：`litellm_config.yaml`（由 Task 1 生成的产物）

**Interfaces:**
- Consumes: Task 1 产出的 `litellm_config.yaml`（含 `additional_drop_params`）
- Produces: 无代码产物。产出一份实测结论，用于确认修复在真实端口上成立。

> ⚠ 本任务要停掉并重启 4000 端口的代理。**执行前必须确认当前没有正在跑的长任务** ——
> 重启会掐断进行中的请求。

- [ ] **Step 1: 重新生成生产配置**

```bash
cd /d/git_software/UJN_LLM_API
uv run python build_litellm_config.py
```

Expected: 输出 `模型数 : 8 个`，无报错。

- [ ] **Step 2: 确认新配置不含非 ASCII**

```bash
uv run python -c "
data = open('litellm_config.yaml', 'rb').read()
bad = [(i, b) for i, b in enumerate(data) if b > 127]
print('非 ASCII 字节数:', len(bad))
print('OK' if not bad else f'FAIL 首个位置 {bad[0]}')
"
```

Expected: `非 ASCII 字节数: 0` 然后 `OK`

- [ ] **Step 3: 重启 4000 端口的代理**

先停掉占用 4000 的进程，再用 `run.ps1` 起：

```bash
# 停掉旧进程
pid=$(netstat -ano | grep ":4000 " | grep LISTENING | awk '{print $5}' | head -1)
[ -n "$pid" ] && taskkill //PID $pid //F
```

然后由用户在一个终端里运行（`run.ps1` 是前台守护循环，不要后台起）：

```powershell
.\run.ps1
```

等日志出现 `代理已就绪: http://127.0.0.1:4000`。

- [ ] **Step 4: 验证 Codex 形态（曾经 400 的那两个）**

```bash
uv run python - <<'EOF'
import httpx, json
c = httpx.Client(timeout=180, trust_env=False)
U = "http://127.0.0.1:4000/v1/responses"
H = {"Content-Type": "application/json", "Authorization": "Bearer dummy"}
base = {"model": "deepseek-v41-flash", "input": "hi", "max_output_tokens": 64}
cases = {
    "无 reasoning (基线)":                       base,
    "reasoning={effort:medium} (曾 400)":        {**base, "reasoning": {"effort": "medium"}},
    "reasoning={effort:high,summary:auto} (曾 400)": {**base, "reasoning": {"effort": "high", "summary": "auto"}},
    "reasoning={effort:high,summary:detailed}":  {**base, "reasoning": {"effort": "high", "summary": "detailed"}},
    "reasoning={effort:max}":                    {**base, "reasoning": {"effort": "max"}},
}
for label, body in cases.items():
    r = c.post(U, headers=H, json=body)
    note = ""
    if r.status_code != 200:
        try: note = json.loads(r.text).get("error", {}).get("message", "")[:90]
        except Exception: note = r.text[:90]
    print(f"  {label:46s} {r.status_code}  {note}")
EOF
```

Expected: 5 行全部 `200`（基线本来就 200，其余 4 行是本次修复的目标）

- [ ] **Step 5: 验证 Claude Code 形态没有回归**

```bash
uv run python - <<'EOF'
import httpx, json
c = httpx.Client(timeout=180, trust_env=False)
U = "http://127.0.0.1:4000/v1/messages"
H = {"Content-Type": "application/json", "x-api-key": "dummy", "anthropic-version": "2023-06-01"}
base = {"model": "deepseek-v41-flash", "max_tokens": 256,
        "messages": [{"role": "user", "content": "hi"}]}
cases = {
    "无 effort (基线)":                    base,
    "output_config.effort=high":          {**base, "output_config": {"effort": "high"}},
    "output_config.effort=max":           {**base, "output_config": {"effort": "max"}},
    "thinking+effort (Claude Code 真实形态)": {**base, "thinking": {"type": "adaptive", "display": "omitted"},
                                              "output_config": {"effort": "high"}},
}
for label, body in cases.items():
    r = c.post(U, headers=H, json=body)
    note = ""
    if r.status_code != 200:
        try: note = json.loads(r.text).get("error", {}).get("message", "")[:90]
        except Exception: note = r.text[:90]
    print(f"  {label:42s} {r.status_code}  {note}")
EOF
```

Expected: 4 行全部 `200`

- [ ] **Step 6: 跑既有测试套件，确认无回归**

```bash
uv run python tests/smoke_test.py
uv run pytest tests/ -q
```

Expected: smoke test `6/6 passed`；pytest `35 passed`

- [ ] **Step 7: 记录结论**

把上面 Step 4/5 的实际输出贴回给用户，并明确说明：

- 哪些形态从 `400` 变成 `200`
- effort 档位现在**不再实际调节**上游推理强度（上游用自己的默认值），这是实测权衡
- 若将来需要真正调节 effort，需要另做映射表，并单独处理 `summary` 子字段

**无需提交**（本任务不改文件）。

---

## 收尾：更新文档

Task 1、2 都通过后，由**控制器**（不是子代理）更新文档，因为改动涉及跨文件一致性判断：

1. `README.md` —— 在「常见问题」里把现有的 effort 段落改为说明「effort 档位已统一丢弃」，
   并给出实测结论。中英文两处都要改。
2. `docs/客户端配置指南.md` —— 更新「关于 effort 档位」那一段，说明两个客户端形态都已支持，
   但 effort 不产生实际效果。

这两处不属于可独立验证的任务单元（是纯文案同步），因此并入收尾，不单列 Task。
