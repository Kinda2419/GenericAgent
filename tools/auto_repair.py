import os, sys, re, time, json, subprocess, threading, traceback, platform
from pathlib import Path
from datetime import datetime

script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # GenericAgent/

# ─── 错误模式库 ───────────────────────────────────────────────
ERROR_PATTERNS = [
    {
        "id": "api_key_invalid",
        "patterns": [r"invalid.?api.?key", r"authentication.*fail", r"401.*unauthorized", r"incorrect.*api.*key"],
        "fix": "reload_api_keys",
        "desc": "API Key 无效或过期",
    },
    {
        "id": "rate_limit",
        "patterns": [r"rate.?limit", r"429", r"too many requests", r"quota.*exceeded", r"resource.*exhausted"],
        "fix": "switch_llm_and_wait",
        "desc": "API 限流",
    },
    {
        "id": "network_error",
        "patterns": [r"connection.*(?:reset|refused|timeout|error)", r"urlopen.*error", r"SSLError",
                     r"RemoteDisconnected", r"ConnectionError", r"timeout.*expired", r"ETIMEDOUT", r"socket.*error"],
        "fix": "retry_with_backoff",
        "desc": "网络连接问题",
    },
    {
        "id": "context_too_long",
        "patterns": [r"context.*(?:length|window).*exceeded", r"maximum.*context", r"token.*limit",
                     r"max.*tokens.*exceeded", r"context_length_exceeded", r"string too long"],
        "fix": "trim_context",
        "desc": "上下文过长",
    },
    {
        "id": "json_decode",
        "patterns": [r"JSONDecodeError", r"json\.decoder", r"Expecting.*value"],
        "fix": "retry_simple",
        "desc": "JSON 解析失败（通常为 API 返回异常）",
    },
    {
        "id": "model_overloaded",
        "patterns": [r"overloaded", r"503", r"service.*unavailable", r"server.*error", r"500.*internal"],
        "fix": "switch_llm_and_wait",
        "desc": "模型服务过载",
    },
    {
        "id": "empty_response",
        "patterns": [r"NoneType.*has no attribute", r"empty.*response", r"content.*None"],
        "fix": "retry_simple",
        "desc": "空响应",
    },
]

# ─── 错误分析 ─────────────────────────────────────────────────
def analyze_error(error_str):
    """分析错误字符串，返回匹配的错误模式列表"""
    error_lower = error_str.lower()
    matches = []
    for pat in ERROR_PATTERNS:
        for regex in pat["patterns"]:
            if re.search(regex, error_lower):
                matches.append(pat)
                break
    return matches


# ─── 修复策略 ─────────────────────────────────────────────────
class RepairStrategy:
    """修复策略执行器，绑定到一个 GeneraticAgent 实例"""

    def __init__(self, agent):
        self.agent = agent

    def reload_api_keys(self, error_str):
        """重新加载 API Key 配置"""
        try:
            self.agent.load_llm_sessions()
            return True, "已重新加载 API Key 配置"
        except Exception as e:
            return False, f"重载 API Key 失败: {e}"

    def switch_llm_and_wait(self, error_str):
        """切换到下一个 LLM 后端并等待"""
        try:
            n_clients = len(self.agent.llmclients)
            if n_clients <= 1:
                time.sleep(10)
                return True, "仅有一个 LLM 后端，已等待 10s 后重试"
            self.agent.next_llm()
            name = self.agent.get_llm_name(model=True)
            time.sleep(3)
            return True, f"已切换到 LLM: {name}，等待 3s"
        except Exception as e:
            return False, f"切换 LLM 失败: {e}"

    def retry_with_backoff(self, error_str):
        """带退避的简单重试"""
        time.sleep(5)
        return True, "网络错误，已等待 5s"

    def trim_context(self, error_str):
        """裁剪上下文"""
        try:
            backend = self.agent.llmclient.backend
            if hasattr(backend, 'history') and len(backend.history) > 6:
                cut = len(backend.history) // 3
                backend.history = backend.history[:2] + backend.history[2 + cut:]
                return True, f"已裁剪 {cut} 条历史消息"
            return True, "上下文不长，直接重试"
        except Exception as e:
            return False, f"裁剪上下文失败: {e}"

    def retry_simple(self, error_str):
        """简单重试"""
        time.sleep(2)
        return True, "已等待 2s，准备重试"

    def execute(self, fix_name, error_str):
        method = getattr(self, fix_name, None)
        if method:
            return method(error_str)
        return False, f"未知修复策略: {fix_name}"


# ─── 旁路修复进程管理 ─────────────────────────────────────────
BYPASS_DIR_NAME = "_auto_repair"

def get_bypass_dir():
    d = os.path.join(script_dir, "temp", BYPASS_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d

def is_bypass_running():
    """检查旁路修复进程是否在运行"""
    d = get_bypass_dir()
    pid_file = os.path.join(d, "pid")
    if not os.path.exists(pid_file):
        return False
    try:
        pid = int(open(pid_file).read().strip())
        # 检查进程是否存活
        if platform.system() == "Windows":
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True,
                               creationflags=0x08000000)
            return str(pid) in r.stdout
        else:
            os.kill(pid, 0)
            return True
    except (ValueError, OSError, ProcessLookupError):
        return False

def stop_bypass():
    """停止旁路修复进程"""
    d = get_bypass_dir()
    # 写 _stop 文件
    open(os.path.join(d, "_stop"), "w").write("1")
    time.sleep(1)
    # 如果还活着，强制杀
    pid_file = os.path.join(d, "pid")
    if os.path.exists(pid_file):
        try:
            pid = int(open(pid_file).read().strip())
            if platform.system() == "Windows":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                               creationflags=0x08000000)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            os.remove(pid_file)
        except Exception:
            pass
    return True

def spawn_bypass_repair(error_str, original_query):
    """启动旁路修复子进程"""
    d = get_bypass_dir()
    # 如果已有旁路在运行，不重复启动
    if is_bypass_running():
        return None, "旁路修复进程已在运行"

    # 清理旧文件
    for f in ["_stop", "pid", "status.json", "output.txt", "report.txt"]:
        p = os.path.join(d, f)
        if os.path.exists(p):
            os.remove(p)

    # 写入修复任务
    repair_prompt = _build_repair_prompt(error_str, original_query)
    with open(os.path.join(d, "input.txt"), "w", encoding="utf-8") as f:
        f.write(repair_prompt)

    # 写入状态
    status = {
        "started_at": datetime.now().isoformat(),
        "error": error_str[:500],
        "original_query": original_query[:200],
        "rounds": 0,
        "max_rounds": 5,
        "status": "running",
    }
    with open(os.path.join(d, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    # 启动子进程
    cmd = [
        sys.executable, os.path.join(script_dir, "agentmain.py"),
        "--task", BYPASS_DIR_NAME,
        "--bg"
    ]
    try:
        r = subprocess.run(cmd, cwd=script_dir, capture_output=True, text=True, timeout=10,
                           creationflags=0x08000000 if platform.system() == "Windows" else 0)
        pid = int(r.stdout.strip())
        with open(os.path.join(d, "pid"), "w") as f:
            f.write(str(pid))

        # 启动监控线程
        threading.Thread(target=_bypass_monitor, args=(d, pid), daemon=True).start()

        return pid, f"旁路修复进程已启动 (PID={pid})"
    except Exception as e:
        return None, f"启动旁路修复失败: {e}"


def _build_repair_prompt(error_str, original_query):
    """构建给旁路修复进程的 prompt"""
    return f"""你是 GA 自动修复助手。当前 GA 主进程在执行任务时反复遇到以下错误，需要你诊断并修复。

## 原始任务
{original_query[:500]}

## 错误信息
```
{error_str[:2000]}
```

## 你的任务
1. 分析错误根因
2. 尝试修复（如：修改配置、安装缺失依赖、修复损坏文件等）
3. 验证修复是否成功

## 约束
- 禁止修改 GA 核心源码（ga.py, agentmain.py, agent_loop.py, llmcore.py）
- 可以修改 mykey.py（API配置）、安装 pip 包、修复 temp/ 下的临时文件
- 修复完成后用 ask_user 报告结果
- 如果无法修复，也用 ask_user 如实报告分析结论

请立即开始诊断。"""


def _bypass_monitor(bypass_dir, pid):
    """监控旁路修复进程，5轮未完成则停止并写报告"""
    max_wait = 600  # 最多等10分钟
    check_interval = 15
    elapsed = 0

    while elapsed < max_wait:
        time.sleep(check_interval)
        elapsed += check_interval

        # 检查进程是否还活着
        if not is_bypass_running():
            # 进程已结束 — 检查是否成功
            _finalize_bypass(bypass_dir, "进程已自行结束")
            return

        # 检查 output.txt 判断轮次
        output_file = os.path.join(bypass_dir, "output.txt")
        if os.path.exists(output_file):
            try:
                content = open(output_file, encoding="utf-8", errors="replace").read()
                rounds = len(re.findall(r"Turn \d+", content))
                # 更新 status
                _update_status(bypass_dir, rounds=rounds)
                if rounds >= 5:
                    _finalize_bypass(bypass_dir, f"已运行 {rounds} 轮仍未完成，强制停止")
                    stop_bypass()
                    return
            except Exception:
                pass

    # 超时
    _finalize_bypass(bypass_dir, "超时（10分钟）强制停止")
    stop_bypass()


def _update_status(bypass_dir, **kwargs):
    status_file = os.path.join(bypass_dir, "status.json")
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            status = json.load(f)
        status.update(kwargs)
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _finalize_bypass(bypass_dir, reason):
    """结束旁路修复，写报告"""
    report = {
        "finished_at": datetime.now().isoformat(),
        "reason": reason,
    }
    # 读取 output
    output_file = os.path.join(bypass_dir, "output.txt")
    if os.path.exists(output_file):
        try:
            content = open(output_file, encoding="utf-8", errors="replace").read()
            # 提取最后的摘要
            summaries = re.findall(r"<summary>(.*?)</summary>", content, re.DOTALL)
            report["last_summaries"] = summaries[-3:] if summaries else []
            report["output_tail"] = content[-1000:]
            # 判断是否修复成功
            success_indicators = ["修复成功", "已修复", "fix.*success", "✅.*修复", "问题已解决"]
            report["likely_fixed"] = any(re.search(p, content, re.IGNORECASE) for p in success_indicators)
        except Exception:
            report["output_tail"] = "[读取输出失败]"
            report["likely_fixed"] = False
    else:
        report["output_tail"] = "[无输出]"
        report["likely_fixed"] = False

    _update_status(bypass_dir, status="finished", **report)

    # 写人类可读报告（仅在未成功修复时）
    if not report.get("likely_fixed", False):
        report_file = os.path.join(bypass_dir, "report.txt")
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(f"[自动修复报告] {datetime.now():%Y-%m-%d %H:%M}\n")
            f.write(f"结束原因: {reason}\n")
            f.write(f"修复结果: {'可能已修复' if report.get('likely_fixed') else '未能修复'}\n\n")
            if report.get("last_summaries"):
                f.write("最近进展:\n")
                for s in report["last_summaries"]:
                    f.write(f"  - {s.strip()}\n")
            f.write(f"\n末尾输出:\n{report.get('output_tail', '')}\n")
            f.write("\nNext options and expected consequences:\n")
            f.write("A. Retry the original task without changing GA core source. Expected: low blast radius; may fail again if the root cause is structural. Rollback: none needed.\n")
            f.write("B. Repair environment/config/temp/dependencies only. Expected: fixes transient runtime issues; may change local packages/config. Rollback: restore changed config or uninstall added package.\n")
            f.write("C. Stop and ask for explicit approval before changing GA source/schema. Expected: safest for core stability; task remains unfinished until approved. Rollback: review and revert the proposed diff.\n")
            f.write("Guardrail: auto_repair must not modify GA core source files, tool schemas, or auto_repair.py itself.\n")


# ─── 主入口：错误拦截与自动修复 ───────────────────────────────
class AutoRepairController:
    """
    绑定到 GeneraticAgent 实例，拦截错误并执行修复流程。
    用法：
        controller = AutoRepairController(agent)
        # 在 except 块中:
        should_retry = controller.handle_error(error, raw_query, display_queue)
    """

    def __init__(self, agent):
        self.agent = agent
        self.strategy = RepairStrategy(agent)
        self.consecutive_errors = 0
        self.max_inline_retries = 3
        self._last_error_time = 0
        self._error_history = []

    def reset(self):
        """新任务开始时重置计数"""
        self.consecutive_errors = 0
        self._error_history = []

    def handle_error(self, error, raw_query, display_queue):
        """
        处理错误。返回 True 表示应该重试当前任务，False 表示放弃。
        """
        error_str = format_exception(error)
        now = time.time()

        # 防抖：同一秒内的重复错误不计数
        if now - self._last_error_time < 1:
            return False
        self._last_error_time = now

        self.consecutive_errors += 1
        self._error_history.append({
            "time": datetime.now().isoformat(),
            "error": error_str[:500],
            "attempt": self.consecutive_errors,
        })

        # ── 阶段1：内联修复（最多3次）──
        if self.consecutive_errors <= self.max_inline_retries:
            matches = analyze_error(error_str)
            if matches:
                pat = matches[0]
                attempt = self.consecutive_errors
                display_queue.put({
                    "next": f"\n⚙️ **自动修复** (尝试 {attempt}/{self.max_inline_retries}): "
                            f"检测到 [{pat['desc']}]，正在修复...\n",
                    "source": "system"
                })
                success, msg = self.strategy.execute(pat["fix"], error_str)
                if success:
                    display_queue.put({
                        "next": f"  ✅ {msg}，重试中...\n",
                        "source": "system"
                    })
                    return True  # 重试
                else:
                    display_queue.put({
                        "next": f"  ❌ {msg}\n",
                        "source": "system"
                    })
                    return True  # 仍然重试，换下一个策略
            else:
                # 未匹配已知模式，尝试通用修复
                display_queue.put({
                    "next": f"\n⚙️ **自动修复** (尝试 {self.consecutive_errors}/{self.max_inline_retries}): "
                            f"未匹配已知错误模式，尝试通用恢复...\n",
                    "source": "system"
                })
                self.strategy.retry_with_backoff(error_str)
                # 第2次尝试切换LLM
                if self.consecutive_errors >= 2:
                    self.strategy.switch_llm_and_wait(error_str)
                return True

        # ── 阶段2：3次失败，通知用户 + 启动旁路 ──
        report_msg = self._build_failure_report(error_str, raw_query)
        display_queue.put({"next": report_msg, "source": "system"})

        pid, bypass_msg = spawn_bypass_repair(error_str, raw_query)
        if pid:
            display_queue.put({
                "next": f"\n🔧 **旁路修复已启动** (PID={pid})\n"
                        f"  - 静默修复成功则不打扰你\n"
                        f"  - 5轮未完成会自动停止并报告\n"
                        f"  - 手动停止：在对话中输入 `/repair stop`\n"
                        f"  - 查看状态：输入 `/repair status`\n",
                "source": "system"
            })

            # 启动报告注入线程
            threading.Thread(
                target=self._inject_report_when_ready,
                args=(get_bypass_dir(),),
                daemon=True
            ).start()
        else:
            display_queue.put({
                "next": f"\n⚠️ 旁路修复启动失败: {bypass_msg}\n",
                "source": "system"
            })

        return False  # 不再重试主流程

    def _build_failure_report(self, error_str, raw_query):
        """构建3次失败的汇报消息"""
        msg = f"\n{'='*50}\n"
        msg += f"🚨 **自动修复失败** — 已尝试 {self.max_inline_retries} 次均未成功\n\n"
        msg += f"**错误摘要**: {error_str[:300]}\n\n"
        msg += f"**尝试记录**:\n"
        for h in self._error_history[-3:]:
            msg += f"  - 第{h['attempt']}次 @ {h['time']}: {h['error'][:100]}\n"
        msg += f"\n正在启动旁路修复进程...\n"
        msg += f"{'='*50}\n"
        return msg

    def _inject_report_when_ready(self, bypass_dir):
        """等旁路修复完成后，如果未修复，注入报告到主会话"""
        max_wait = 660  # 略大于旁路的 10 分钟超时
        check_interval = 10
        elapsed = 0

        while elapsed < max_wait:
            time.sleep(check_interval)
            elapsed += check_interval

            status_file = os.path.join(bypass_dir, "status.json")
            if not os.path.exists(status_file):
                continue
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    status = json.load(f)
                if status.get("status") == "finished":
                    if not status.get("likely_fixed", False):
                        # 未修复 → 注入报告到主进程
                        self._inject_bypass_report(bypass_dir, status)
                    # 修复成功 → 静默结束，不打扰
                    return
            except Exception:
                continue

    def _inject_bypass_report(self, bypass_dir, status):
        """将旁路修复报告注入到主会话的 _intervene 文件"""
        task_dir = self.agent.task_dir
        if not task_dir:
            # 非 task 模式，尝试用 handler 注入
            return

        report = f"[旁路修复报告]\n"
        report += f"结束原因: {status.get('reason', '未知')}\n"
        report += f"修复结果: {'可能已修复' if status.get('likely_fixed') else '未能修复'}\n"
        summaries = status.get("last_summaries", [])
        if summaries:
            report += "最近进展:\n"
            for s in summaries:
                report += f"  - {s}\n"
        report += f"\n需要你人工介入检查。"

        try:
            with open(os.path.join(task_dir, "_intervene"), "w", encoding="utf-8") as f:
                f.write(report)
        except Exception:
            pass


def format_exception(e):
    """格式化异常为字符串，包含 traceback"""
    try:
        return "".join(traceback.format_exception(type(e), e, e.__traceback__))
    except Exception:
        return str(e)


# ─── /repair 斜杠命令处理 ─────────────────────────────────────
def handle_repair_command(subcmd):
    """处理 /repair 斜杠命令，返回 (handled, response_text)"""
    subcmd = subcmd.strip().lower()

    if subcmd == "stop":
        if is_bypass_running():
            stop_bypass()
            return True, "✅ 旁路修复进程已停止"
        return True, "ℹ️ 没有正在运行的旁路修复进程"

    elif subcmd == "status":
        d = get_bypass_dir()
        status_file = os.path.join(d, "status.json")
        if not os.path.exists(status_file):
            return True, "ℹ️ 没有旁路修复记录"
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                status = json.load(f)
            running = is_bypass_running()
            msg = f"🔧 **旁路修复状态**\n"
            msg += f"  - 运行中: {'是' if running else '否'}\n"
            msg += f"  - 启动时间: {status.get('started_at', '?')}\n"
            msg += f"  - 已运行轮次: {status.get('rounds', '?')}\n"
            msg += f"  - 状态: {status.get('status', '?')}\n"
            if status.get("reason"):
                msg += f"  - 结束原因: {status['reason']}\n"
            if status.get("likely_fixed") is not None:
                msg += f"  - 修复结果: {'可能已修复 ✅' if status['likely_fixed'] else '未能修复 ❌'}\n"
            return True, msg
        except Exception as e:
            return True, f"⚠️ 读取状态失败: {e}"

    elif subcmd == "report":
        d = get_bypass_dir()
        report_file = os.path.join(d, "report.txt")
        if os.path.exists(report_file):
            content = open(report_file, encoding="utf-8", errors="replace").read()
            return True, f"```\n{content}\n```"
        return True, "ℹ️ 暂无修复报告"

    return False, ""
