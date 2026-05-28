"""
parallel_dispatch.py — gstack skills 并行编排工具
用法：由主agent通过code_run调用，将多个独立gstack skills分发给subagent并行执行。

示例：
    from parallel_dispatch import ParallelDispatcher
    pd = ParallelDispatcher(project_dir="C:/path/to/project")
    pd.add_skill("review", skill_path="gstack-review", extra_context="审查PR #42")
    pd.add_skill("qa", skill_path="gstack-qa", extra_context="测试登录流程")
    pd.launch_all()  # 并行启动所有subagent
    results = pd.monitor()  # 监控等待完成，返回汇总
"""

import os
import sys
import json
import time
import subprocess
import platform
from pathlib import Path
from datetime import datetime

AGENT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
AGENTMAIN = os.path.join(AGENT_ROOT, 'agentmain.py')
SKILLS_DIR = os.path.expanduser('~/.ga/skills')
TEMP_DIR = os.path.join(AGENT_ROOT, 'temp')

# 资源冲突分类
RESOURCE_TAGS = {
    'browser': ['gstack', 'gstack-browse', 'gstack-qa', 'gstack-scrape',
                 'gstack-setup-browser-cookies'],
    'keyboard_mouse': ['gstack', 'gstack-browse'],
    'git': ['gstack-ship', 'gstack-review'],
    'pure_code': ['gstack-autoplan', 'gstack-careful', 'gstack-review',
                  'gstack-context-save', 'gstack-context-restore',
                  'gstack-health', 'gstack-benchmark', 'gstack-benchmark-models',
                  'gstack-skillify', 'gstack-upgrade'],
}

def get_resource_needs(skill_name):
    """返回skill所需的共享资源集合"""
    needs = set()
    for resource, skills in RESOURCE_TAGS.items():
        if skill_name in skills:
            needs.add(resource)
    return needs

def check_conflicts(skill_list):
    """检查skill列表中的资源冲突，返回(parallel_groups, warnings)
    
    返回值:
        parallel_groups: list of lists，每组内的skill可以并行
        warnings: 冲突警告信息
    """
    warnings = []
    skill_resources = {s: get_resource_needs(s) for s in skill_list}
    
    # 贪心分组：逐个skill尝试放入已有组，检查无冲突
    groups = []
    group_resources = []
    
    for skill in skill_list:
        needs = skill_resources[skill]
        placed = False
        for i, (group, occupied) in enumerate(zip(groups, group_resources)):
            exclusive = {'browser', 'keyboard_mouse'}
            conflict = needs & occupied & exclusive
            if not conflict:
                group.append(skill)
                occupied.update(needs)
                placed = True
                break
        if not placed:
            groups.append([skill])
            group_resources.append(set(needs))
    
    if len(groups) > 1:
        warnings.append(
            f"资源冲突分析：{len(groups)}个串行批次，"
            f"批次间因共享资源(browser/键鼠)需串行执行"
        )
    
    return groups, warnings


class SkillTask:
    """单个skill任务封装"""
    def __init__(self, name, skill_path, project_dir, extra_context="",
                 use_codex=False, codex_prompt=None, role_card=None):
        self.name = name
        self.skill_path = skill_path
        self.project_dir = project_dir
        self.extra_context = extra_context
        self.use_codex = use_codex
        self.codex_prompt = codex_prompt
        self.role_card = role_card
        self.task_dir = os.path.join(TEMP_DIR, f"parallel_{name}")
        self.pid = None
        self.status = "pending"
        self.result = None
        self.start_time = None
        self.end_time = None
    
    def prepare_input(self):
        """准备subagent的input.txt和context.json"""
        os.makedirs(self.task_dir, exist_ok=True)
        
        import glob
        for f in glob.glob(os.path.join(self.task_dir, 'output*.txt')):
            os.remove(f)
        
        skill_md = os.path.join(SKILLS_DIR, self.skill_path, 'SKILL.md')
        
        context = {
            "task": f"执行 {self.skill_path} skill",
            "work_dir": os.path.abspath(self.project_dir),
            "skill_file": skill_md,
            "extra_context": self.extra_context,
            "use_codex_if_needed": self.use_codex,
            "role_card": self.role_card,
        }
        with open(os.path.join(self.task_dir, 'context.json'), 'w', encoding='utf-8') as f:
            json.dump(context, f, ensure_ascii=False, indent=2)
        
        input_text = f"""你需要执行一个 gstack skill 任务。

## 任务
先读取 context.json 获取完整上下文，然后读取 skill 文件并按其指令执行。

## Skill 文件
{skill_md}

## 项目目录
{os.path.abspath(self.project_dir)}

## 额外上下文
{self.extra_context if self.extra_context else '(无)'}

## 执行要求
1. 先 file_read("context.json") 获取参数
2. 再 file_read("{skill_md}") 读取 skill 指令
3. 按 skill 指令在项目目录下执行任务
4. 完成后在 output.txt 中输出结构化结果摘要
"""
        if self.role_card:
            role_path = self.role_card
            if not os.path.isabs(role_path):
                role_path = os.path.join(AGENT_ROOT, role_path)
            input_text += f"""

## Worker Role Card
Before acting, read this role card and follow its scope/boundaries:
{os.path.abspath(role_path)}
"""
        if self.use_codex:
            input_text += """
## Codex CLI 授权
遇到复杂编程子任务时可升级调用 Codex CLI，参见 codex_cli_sop.md。
"""
        # 始终注入递归 /team 能力
        input_text += """
## 递归团队能力 (/team)
你拥有通过 Claude Code CLI 开子团队的能力。当你发现某个子任务仍然复杂（需修改 3+ 文件或涉及 2+ 独立模块），
可以用 `claude -p` 启动多个并行子 agent 来协作完成，而不必一个人串行做完。

用法：通过 Bash 工具执行:
```
claude -p "<子任务描述>" --allowedTools "Bash,Read,Write,Edit,MultiEdit,Glob,Grep" --output-format json -C "<workdir>" > /tmp/subtask_N.json 2>&1 &
```
多个无依赖子任务可并行启动，用 `wait` 收集结果后汇总。

规则：最大递归深度 3 层，简单任务直接做不要递归。
"""
        
        with open(os.path.join(self.task_dir, 'input.txt'), 'w', encoding='utf-8') as f:
            f.write(input_text)
    
    def launch(self):
        """启动subagent后台进程"""
        self.prepare_input()
        cmd = [
            sys.executable, AGENTMAIN,
            '--task', f'parallel_{self.name}',
            '--bg'
        ]
        result = subprocess.run(
            cmd, cwd=AGENT_ROOT,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            self.pid = int(result.stdout.strip())
            self.status = "running"
            self.start_time = time.time()
            return self.pid
        else:
            self.status = "failed"
            self.result = f"启动失败: {result.stderr}"
            return None
    
    def check_done(self):
        """检查是否完成（output.txt含[ROUND END]）"""
        if self.status != "running":
            return self.status == "done" or self.status == "failed"
        
        output_file = os.path.join(self.task_dir, 'output.txt')
        if os.path.exists(output_file):
            try:
                with open(output_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                if '[ROUND END]' in content:
                    self.status = "done"
                    self.result = content
                    self.end_time = time.time()
                    return True
            except Exception:
                pass
        return False
    
    def get_progress(self):
        """获取当前进度（读output.txt最后几行）"""
        output_file = os.path.join(self.task_dir, 'output.txt')
        if os.path.exists(output_file):
            try:
                with open(output_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                return ''.join(lines[-5:]) if lines else "(输出为空)"
            except Exception:
                return "(读取失败)"
        return "(尚无输出)"
    
    def intervene(self, message):
        """向subagent注入干预指令"""
        with open(os.path.join(self.task_dir, '_intervene'), 'w', encoding='utf-8') as f:
            f.write(message)
    
    def inject_keyinfo(self, info):
        """向subagent注入关键信息"""
        with open(os.path.join(self.task_dir, '_keyinfo'), 'w', encoding='utf-8') as f:
            f.write(info)
    
    def stop(self):
        """请求subagent停止"""
        with open(os.path.join(self.task_dir, '_stop'), 'w', encoding='utf-8') as f:
            f.write('stop')


class ParallelDispatcher:
    """并行编排调度器"""
    
    def __init__(self, project_dir="."):
        self.project_dir = os.path.abspath(project_dir)
        self.tasks = {}
        self.groups = None
        self.launched = False
    
    def add_skill(self, name, skill_path, extra_context="", use_codex=False, role_card=None):
        """添加一个skill任务"""
        self.tasks[name] = SkillTask(
            name=name,
            skill_path=skill_path,
            project_dir=self.project_dir,
            extra_context=extra_context,
            use_codex=use_codex,
            role_card=role_card,
        )
        return self
    
    def analyze(self):
        """分析资源冲突，返回并行分组方案"""
        skill_names = [t.skill_path for t in self.tasks.values()]
        groups, warnings = check_conflicts(skill_names)
        
        path_to_names = {}
        for name, task in self.tasks.items():
            path_to_names.setdefault(task.skill_path, []).append(name)
        
        self.groups = []
        for group in groups:
            task_names = []
            for skill_path in group:
                if skill_path in path_to_names:
                    task_names.extend(path_to_names[skill_path])
                    path_to_names[skill_path] = []
            if task_names:
                self.groups.append(task_names)
        
        report = {
            "total_tasks": len(self.tasks),
            "parallel_groups": self.groups,
            "group_count": len(self.groups),
            "warnings": warnings,
            "tasks": {
                name: {
                    "skill": t.skill_path,
                    "resources": list(get_resource_needs(t.skill_path)),
                    "role_card": t.role_card,
                }
                for name, t in self.tasks.items()
            }
        }
        return report
    
    def launch_all(self):
        """按分组启动所有任务（组内并行，组间串行）"""
        if not self.groups:
            self.analyze()
        
        results = []
        for batch_idx, batch in enumerate(self.groups):
            print(f"\n{'='*50}")
            print(f"[Batch {batch_idx+1}/{len(self.groups)}] 启动: {batch}")
            print(f"{'='*50}")
            
            pids = {}
            for name in batch:
                task = self.tasks[name]
                pid = task.launch()
                if pid:
                    pids[name] = pid
                    print(f"  ✓ {name} (PID={pid}) 已启动")
                else:
                    print(f"  ✗ {name} 启动失败: {task.result}")
            
            if batch_idx < len(self.groups) - 1:
                print(f"\n  等待 Batch {batch_idx+1} 完成...")
                batch_results = self._wait_batch(batch, timeout=600)
                results.append(batch_results)
        
        self.launched = True
        return {
            "launched": True,
            "groups": self.groups,
            "completed_batches": results,
        }
    
    def _wait_batch(self, task_names, timeout=600):
        """等待一批任务完成"""
        start = time.time()
        while time.time() - start < timeout:
            all_done = all(
                self.tasks[n].check_done() for n in task_names
            )
            if all_done:
                return {n: self.tasks[n].status for n in task_names}
            time.sleep(5)
        
        return {
            n: self.tasks[n].status + (" (timeout)" if self.tasks[n].status == "running" else "")
            for n in task_names
        }
    
    def monitor(self, interval=10, timeout=600):
        """监控所有运行中的任务，阻塞直到全部完成或超时"""
        start = time.time()
        while time.time() - start < timeout:
            statuses = {}
            all_done = True
            for name, task in self.tasks.items():
                task.check_done()
                statuses[name] = {
                    "status": task.status,
                    "elapsed": f"{time.time() - task.start_time:.0f}s" if task.start_time else "-",
                    "progress_tail": task.get_progress()[-200:] if task.status == "running" else "",
                }
                if task.status == "running":
                    all_done = False
            
            if all_done:
                return self._build_summary()
            
            running = [n for n, s in statuses.items() if s["status"] == "running"]
            done = [n for n, s in statuses.items() if s["status"] == "done"]
            print(f"[{time.time()-start:.0f}s] done={done} running={running}")
            time.sleep(interval)
        
        return self._build_summary(timed_out=True)
    
    def poll(self):
        """非阻塞轮询一次所有任务状态"""
        statuses = {}
        for name, task in self.tasks.items():
            task.check_done()
            statuses[name] = {
                "status": task.status,
                "pid": task.pid,
                "elapsed": f"{time.time() - task.start_time:.0f}s" if task.start_time else "-",
            }
        return statuses
    
    def _build_summary(self, timed_out=False):
        """构建最终汇总报告"""
        summary = {
            "timed_out": timed_out,
            "tasks": {}
        }
        for name, task in self.tasks.items():
            summary["tasks"][name] = {
                "status": task.status,
                "pid": task.pid,
                "duration": f"{task.end_time - task.start_time:.0f}s" if task.end_time and task.start_time else "N/A",
                "result_preview": task.result[:500] if task.result else None,
                "output_file": os.path.join(task.task_dir, "output.txt"),
            }
        
        summary_file = os.path.join(TEMP_DIR, "parallel_dispatch_summary.json")
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        summary["summary_file"] = summary_file
        
        return summary


def quick_dispatch(skills, project_dir=".", extra_context="", use_codex=False, role_cards=None):
    """快速分发多个skill的便捷函数"""
    pd = ParallelDispatcher(project_dir=project_dir)
    role_cards = role_cards or {}
    for skill in skills:
        name = skill.replace("gstack-", "")
        pd.add_skill(name, skill, extra_context=extra_context, use_codex=use_codex, role_card=role_cards.get(name) or role_cards.get(skill))
    
    report = pd.analyze()
    print(f"分析结果: {json.dumps(report, ensure_ascii=False, indent=2)}")
    
    pd.launch_all()
    return pd


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="gstack skills 并行编排")
    parser.add_argument('skills', nargs='+', help='要并行执行的skill名')
    parser.add_argument('--project', '-p', default='.', help='项目目录')
    parser.add_argument('--context', '-c', default='', help='额外上下文')
    parser.add_argument('--codex', action='store_true', help='授权Codex CLI')
    parser.add_argument('--timeout', type=int, default=600, help='总超时秒数')
    args = parser.parse_args()
    
    pd = quick_dispatch(args.skills, args.project, args.context, args.codex)
    result = pd.monitor(timeout=args.timeout)
    print(f"\n{'='*50}")
    print(json.dumps(result, ensure_ascii=False, indent=2))

