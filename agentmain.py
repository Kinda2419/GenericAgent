import os, sys, threading, queue, time, json, re, random, locale
os.environ.setdefault('GA_LANG', 'zh' if any(k in (locale.getlocale()[0] or '').lower() for k in ('zh', 'chinese')) else 'en')
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
elif hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(errors='replace')
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
elif hasattr(sys.stderr, 'reconfigure'): sys.stderr.reconfigure(errors='replace')
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from llmcore import reload_mykeys, LLMSession, ToolClient, ClaudeSession, MixinSession, NativeToolClient, NativeClaudeSession, NativeOAISession
from agent_loop import agent_runner_loop
from ga import GenericAgentHandler, smart_format, get_global_memory, format_error, consume_file
from tools.auto_repair import AutoRepairController, handle_repair_command

script_dir = os.path.dirname(os.path.abspath(__file__))
def load_tool_schema(suffix=''):
    global TOOLS_SCHEMA
    with open(os.path.join(script_dir, f'assets/tools_schema{suffix}.json'), 'r', encoding='utf-8') as f:
        TS = f.read()
    TOOLS_SCHEMA = json.loads(TS if os.name == 'nt' else TS.replace('powershell', 'bash'))
load_tool_schema()

lang_suffix = '_en' if os.environ.get('GA_LANG', '') == 'en' else ''
mem_dir = os.path.join(script_dir, 'memory')
if not os.path.exists(mem_dir): os.makedirs(mem_dir)
mem_txt = os.path.join(mem_dir, 'global_mem.txt')
if not os.path.exists(mem_txt): open(mem_txt, 'w', encoding='utf-8').write('# [Global Memory - L2]\n')
mem_insight = os.path.join(mem_dir, 'global_mem_insight.txt')
if not os.path.exists(mem_insight):
    t = os.path.join(script_dir, f'assets/global_mem_insight_template{lang_suffix}.txt')
    open(mem_insight, 'w', encoding='utf-8').write(open(t, encoding='utf-8').read() if os.path.exists(t) else '')
cdp_cfg = os.path.join(script_dir, 'assets/tmwd_cdp_bridge/config.js')
if not os.path.exists(cdp_cfg):
    try:
        os.makedirs(os.path.dirname(cdp_cfg), exist_ok=True)
        open(cdp_cfg, 'w', encoding='utf-8').write(f"const TID = '__ljq_{hex(random.randint(0, 99999999))[2:8]}';")
    except Exception as e: print(f'[WARN] CDP config init failed: {e} — advanced web features (tmwebdriver) will be unavailable.')

def get_system_prompt():
    with open(os.path.join(script_dir, f'assets/sys_prompt{lang_suffix}.txt'), 'r', encoding='utf-8') as f: prompt = f.read()
    prompt += f"\nToday: {time.strftime('%Y-%m-%d %a')}\n"
    prompt += get_global_memory()
    return prompt

_RETRYABLE_LLM_TEXT_PATTERNS = (
    '!!!Error:',
    '[Error:',
    'HTTP 408',
    'HTTP 409',
    'HTTP 425',
    'HTTP 429',
    'HTTP 500',
    'HTTP 502',
    'HTTP 503',
    'HTTP 504',
    'HTTP 529',
    'Upstream service temporarily unavailable',
    '流异常中断',
)


def _looks_retryable_llm_failure(text):
    text = (text or '').lstrip()
    return text.startswith(('!!!Error:', '[Error:', '[!!! 流异常中断'))

class GeneraticAgent:
    def __init__(self):
        os.makedirs(os.path.join(script_dir, 'temp'), exist_ok=True)
        self.lock = threading.Lock()
        self.task_dir = None
        self.history = []
        self.task_queue = queue.Queue() 
        self.is_running = False; self.stop_sig = False
        self.llm_no = 0;  self.inc_out = False
        self.handler = None; self.verbose = True
        self.repair_controller = AutoRepairController(self)
        self.load_llm_sessions()

    def load_llm_sessions(self):
        mykeys, changed = reload_mykeys()
        if not changed and hasattr(self, 'llmclients'): return
        try: oldhistory = self.llmclient.backend.history
        except: oldhistory = None
        llm_sessions = []
        for k, cfg in mykeys.items():
            if not any(x in k for x in ['api', 'config', 'cookie']): continue
            try:
                if 'native' in k and 'claude' in k: llm_sessions += [NativeToolClient(NativeClaudeSession(cfg=cfg))]
                elif 'native' in k and 'oai' in k: llm_sessions += [NativeToolClient(NativeOAISession(cfg=cfg))]
                elif 'claude' in k: llm_sessions += [ToolClient(ClaudeSession(cfg=cfg))]
                elif 'oai' in k: llm_sessions += [ToolClient(LLMSession(cfg=cfg))]
                elif 'mixin' in k: llm_sessions += [{'mixin_cfg': cfg}]
            except: pass
        for i, s in enumerate(llm_sessions):
            if isinstance(s, dict) and 'mixin_cfg' in s:
                try:
                    mixin = MixinSession(llm_sessions, s['mixin_cfg'])
                    if isinstance(mixin._sessions[0], (NativeClaudeSession, NativeOAISession)): llm_sessions[i] = NativeToolClient(mixin)
                    else: llm_sessions[i] = ToolClient(mixin)
                except Exception as e: print(f'\n\n\n[ERROR] Failed to init MixinSession with cfg {s["mixin_cfg"]}: {e}!!!\n\n')
        self.llmclients = llm_sessions
        default_llm = mykeys.get('default_llm_name', mykeys.get('default_llm'))
        if default_llm and not getattr(self, '_default_llm_applied', False):
            try:
                self.llm_no = int(default_llm)
            except (TypeError, ValueError):
                for i, client in enumerate(self.llmclients):
                    if getattr(getattr(client, 'backend', None), 'name', None) == default_llm:
                        self.llm_no = i
                        break
            self._default_llm_applied = True
        self.llmclient = self.llmclients[self.llm_no%len(self.llmclients)]
        if oldhistory: self.llmclient.backend.history = oldhistory
    
    def next_llm(self, n=-1):
        self.load_llm_sessions()
        self.llm_no = ((self.llm_no + 1) if n < 0 else n) % len(self.llmclients)
        lastc = self.llmclient
        self.llmclient = self.llmclients[self.llm_no]
        try: self.llmclient.backend.history = lastc.backend.history
        except: raise Exception('[ERROR] BAD Mixin config: Check your mykey.py')
        self.llmclient.last_tools = ''
        name = self.get_llm_name(model=True)
        if 'glm' in name or 'minimax' in name or 'kimi' in name: load_tool_schema('_cn')
        else: load_tool_schema()
    def list_llms(self): 
        self.load_llm_sessions()
        return [(i, self.get_llm_name(b), i == self.llm_no) for i, b in enumerate(self.llmclients)]
    def get_llm_name(self, b=None, model=False):
        b = self.llmclient if b is None else b
        if isinstance(b, dict): return 'BADCONFIG_MIXIN'
        if model: return b.backend.model.lower()
        return f"{type(b.backend).__name__}/{b.backend.name}"

    def abort(self):
        if not self.is_running: return
        print('Abort current task...')
        self.stop_sig = True
        if self.handler is not None: self.handler.code_stop_signal.append(1)
            
    def put_task(self, query, source="user", images=None):
        display_queue = queue.Queue()
        self.task_queue.put({"query": query, "source": source, "images": images or [], "output": display_queue})
        return display_queue

    # i know it is dangerous, but raw_query is dangerous enough it doesn't enlarge
    def _handle_slash_cmd(self, raw_query, display_queue):
        if not raw_query.startswith('/'): return raw_query
        if _sm := re.match(r'/session\.(\w+)=(.*)', raw_query.strip()):
            k, v = _sm.group(1), _sm.group(2)
            vfile = os.path.join(script_dir, 'temp', v)
            if os.path.isfile(vfile): v = open(vfile, encoding='utf-8').read().strip()
            try: v = json.loads(v)  # cover number parsing
            except (json.JSONDecodeError, ValueError): pass
            setattr(self.llmclient.backend, k, v)
            display_queue.put({'done': smart_format(f"✅ session.{k} = {repr(v)}", max_str_len=500), 'source': 'system'})
            return None
        if raw_query.strip() == '/resume':
            return r'扫temp/model_responses/下时间最近的10个文件(除本PID)，读取每个文件content后先replace("\\n","\n").replace("\\r","\r")统一为真换行，再用re.findall(r"<history>\n\[(?:USER|Agent)\].*?</history>", content, re.DOTALL)提取，取每文件最后一个匹配作为该会话内容，按mtime倒序，每个用一句话总结聊了什么让我选择；选定后再简单读该文件末尾作为聊天基础'
        if raw_query.strip().startswith('/repair'):
            subcmd = raw_query.strip()[len('/repair'):].strip()
            handled, response = handle_repair_command(subcmd or 'status')
            if handled:
                display_queue.put({'done': response, 'source': 'system'})
                return None
        return raw_query

    def run(self):
        while True:
            task = self.task_queue.get()
            raw_query, source, images, display_queue = task["query"], task["source"], task.get("images") or [], task["output"]
            try:
                raw_query = self._handle_slash_cmd(raw_query, display_queue)
                if raw_query is None:
                    continue
                self.is_running = True
                rquery = smart_format(raw_query.replace('\n', ' '), max_str_len=200)
                self.history.append(f"[USER]: {rquery}")
                self.repair_controller.reset()
                self._run_task_with_repair(raw_query, source, images, display_queue)
            except Exception as e:
                display_queue.put({'done': f"```\n{format_error(e)}\n```", 'source': source})
                print(f"Backend Error: {format_error(e)}")
            finally:
                self.is_running = self.stop_sig = False
                self.task_queue.task_done()
                if self.handler is not None: self.handler.code_stop_signal.append(1)

    def _run_task_with_repair(self, raw_query, source, images, display_queue):
        """执行任务，出错时自动修复重试"""
        llm_text_retries = 0
        max_llm_text_retries = max(0, int(os.environ.get('GA_LLM_TEXT_RETRIES', '2')))
        while True:
            sys_prompt = get_system_prompt() + getattr(self.llmclient.backend, 'extra_sys_prompt', '')
            handler = GenericAgentHandler(self, self.history, os.path.join(script_dir, 'temp'))
            if self.handler and 'key_info' in self.handler.working: 
                ki = re.sub(r'\n\[SYSTEM\] 此为.*?工作记忆[。\n]*', '', self.handler.working['key_info'])  # 去旧
                handler.working['key_info'] = ki
                handler.working['passed_sessions'] = ps = self.handler.working.get('passed_sessions', 0) + 1
                if ps > 0: handler.working['key_info'] += f'\n[SYSTEM] 此为 {ps} 个对话前设置的key_info，若已在新任务，先更新或清除工作记忆。\n'
            self.handler = handler
            # although new handler, the **full** history is in llmclient, so it is full history!
            gen = agent_runner_loop(self.llmclient, sys_prompt, raw_query, 
                                handler, TOOLS_SCHEMA, max_turns=70, verbose=self.verbose)
            try:
                full_resp = ""; last_pos = 0
                for chunk in gen:
                    if consume_file(self.task_dir, '_stop'): self.abort() 
                    if self.stop_sig: break
                    full_resp += chunk
                    if len(full_resp) - last_pos > 50 or 'LLM Running' in chunk:
                        display_queue.put({'next': full_resp[last_pos:] if self.inc_out else full_resp, 'source': source})
                        last_pos = len(full_resp)
                if self.inc_out and last_pos < len(full_resp): display_queue.put({'next': full_resp[last_pos:], 'source': source})
                if '</summary>' in full_resp: full_resp = full_resp.replace('</summary>', '</summary>\n\n')
                if '</file_content>' in full_resp: full_resp = re.sub(r'<file_content>\s*(.*?)\s*</file_content>', r'\n````\n<file_content>\n\1\n</file_content>\n````', full_resp, flags=re.DOTALL)                
                if _looks_retryable_llm_failure(full_resp) and llm_text_retries < max_llm_text_retries and not self.stop_sig:
                    llm_text_retries += 1
                    try:
                        self.next_llm()
                        next_name = self.get_llm_name()
                    except Exception as retry_err:
                        display_queue.put({'next': f'\n[LLM Retry] switch failed: {format_error(retry_err)}\n', 'source': source})
                        next_name = 'current'
                    retry_notice = f'\n[LLM Retry] retryable provider error detected; retry {llm_text_retries}/{max_llm_text_retries} with {next_name}\n'
                    display_queue.put({'next': retry_notice, 'summary': f'LLM retry {llm_text_retries}/{max_llm_text_retries}', 'source': source})
                    continue
                display_queue.put({'done': full_resp, 'source': source})
                self.history = handler.history_info
                return  # 成功完成，退出重试循环
            except Exception as e:
                error_str = format_error(e)
                print(f"Backend Error: {error_str}")
                # 自动修复尝试
                should_retry = self.repair_controller.handle_error(e, raw_query, display_queue)
                if should_retry and not self.stop_sig:
                    print(f"[AutoRepair] Retrying... (attempt {self.repair_controller.consecutive_errors})")
                    continue  # 重试整个任务循环
                else:
                    # 修复失败或用户中止，正常结束
                    display_queue.put({'done': full_resp + f'\n```\n{error_str}\n```', 'source': source})
                    if self.stop_sig:
                        print('User aborted the task.')
                    return

    


class GenericAgentSession:
    """One isolated task room inside the same GA installation."""
    def __init__(self, session_id, metadata=None):
        self.session_id = session_id
        self.metadata = metadata or {}
        self.agent = GeneraticAgent()
        self.agent.peer_hint = True
        self.documents = []
        self.created_at = time.time()
        self.updated_at = self.created_at
        threading.Thread(target=self.agent.run, daemon=True).start()

    def touch(self):
        self.updated_at = time.time()

    def put_task(self, query, source="user", images=None):
        self.touch()
        return self.agent.put_task(query, source=source, images=images)

    def abort(self):
        self.touch()
        return self.agent.abort()

    def add_document(self, path, name=None):
        if not path:
            return
        self.documents.append({"path": path, "name": name or os.path.basename(path), "time": time.time()})
        self.touch()


class GenericAgentRuntime:
    """Native multi-session runtime for one GA process."""
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}

    def get_or_create(self, session_id, metadata=None):
        with self.lock:
            sess = self.sessions.get(session_id)
            if sess is None:
                sess = GenericAgentSession(session_id, metadata=metadata)
                self.sessions[session_id] = sess
            elif metadata:
                sess.metadata.update(metadata)
                sess.touch()
            return sess

    def get(self, session_id):
        with self.lock:
            return self.sessions.get(session_id)

    def abort(self, session_id):
        sess = self.get(session_id)
        if sess:
            sess.abort()
            return True
        return False

    def active_sessions(self):
        with self.lock:
            return list(self.sessions.values())

if __name__ == '__main__':
    import argparse
    from datetime import datetime
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', metavar='IODIR', help='一次性任务模式(文件IO)')
    parser.add_argument('--reflect', metavar='SCRIPT', help='反射模式：加载监控脚本，check()触发时发任务')
    parser.add_argument('--input', help='prompt')
    parser.add_argument('--llm_no', type=int)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--bg', action='store_true', help='popen, print PID, exit')
    args = parser.parse_args()

    if args.bg:
        import subprocess, platform
        cmd = [sys.executable, os.path.abspath(__file__)] + [a for a in sys.argv[1:] if a != '--bg']
        d = os.path.join(script_dir, f'temp/{args.task}'); os.makedirs(d, exist_ok=True)
        p = subprocess.Popen(cmd, cwd=script_dir,
            creationflags=0x08000000 if platform.system() == 'Windows' else 0,
            stdout=open(os.path.join(d, 'stdout.log'), 'w', encoding='utf-8'),
            stderr=open(os.path.join(d, 'stderr.log'), 'w', encoding='utf-8'))
        print(p.pid); sys.exit(0)

    agent = GeneraticAgent()
    if args.llm_no is not None:
        agent.next_llm(args.llm_no)
    agent.verbose = args.verbose
    threading.Thread(target=agent.run, daemon=True).start()

    if args.task:
        agent.task_dir = d = os.path.join(script_dir, f'temp/{args.task}'); nround = ''
        infile = os.path.join(d, 'input.txt')
        if args.input:
            os.makedirs(d, exist_ok=True)
            import glob; [os.remove(f) for f in glob.glob(os.path.join(d, 'output*.txt'))]
            with open(infile, 'w', encoding='utf-8') as f: f.write(args.input)
        with open(infile, encoding='utf-8') as f: raw = f.read()
        while True:
            dq = agent.put_task(raw, source='task')
            while 'done' not in (item := dq.get(timeout=120)): 
                if 'next' in item and random.random() < 0.95:  # 概率写一次中间结果
                    with open(f'{d}/output{nround}.txt', 'w', encoding='utf-8') as f: f.write(item.get('next', ''))
            with open(f'{d}/output{nround}.txt', 'w', encoding='utf-8') as f: f.write(item['done'] + '\n\n[ROUND END]\n')
            consume_file(d, '_stop')  # 已经成功停下来了，避免打断下次reply
            for _ in range(300):  # 等reply.txt，10分钟超时
                time.sleep(2)
                if (raw := consume_file(d, 'reply.txt')): break
            else: break
            nround = nround + 1 if isinstance(nround, int) else 1
    elif args.reflect:
        import importlib.util
        spec = importlib.util.spec_from_file_location('reflect_script', args.reflect)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        _mt = os.path.getmtime(args.reflect)
        print(f'[Reflect] loaded {args.reflect}')
        while True:
            if os.path.getmtime(args.reflect) != _mt:
                try: spec.loader.exec_module(mod); _mt = os.path.getmtime(args.reflect); print('[Reflect] reloaded')
                except Exception as e: print(f'[Reflect] reload error: {e}')
            time.sleep(getattr(mod, 'INTERVAL', 5))
            try: task = mod.check()
            except Exception as e: 
                print(f'[Reflect] check() error: {e}'); continue
            if task is None: continue
            print(f'[Reflect] triggered: {task[:80]}')
            dq = agent.put_task(task, source='reflect')
            try:
                while 'done' not in (item := dq.get(timeout=120)): pass
                result = item['done']
                print(result)
            except Exception as e:
                if getattr(mod, 'ONCE', False): raise
                print(f'[Reflect] drain error: {e}'); result = f'[ERROR] {e}'
            log_dir = os.path.join(script_dir, 'temp/reflect_logs'); os.makedirs(log_dir, exist_ok=True)
            script_name = os.path.splitext(os.path.basename(args.reflect))[0]
            open(os.path.join(log_dir, f'{script_name}_{datetime.now():%Y-%m-%d}.log'), 'a', encoding='utf-8').write(f'[{datetime.now():%m-%d %H:%M}]\n{result}\n\n')
            if (on_done := getattr(mod, 'on_done', None)):
                try: on_done(result)
                except Exception as e: print(f'[Reflect] on_done error: {e}')
            if getattr(mod, 'ONCE', False): print('[Reflect] ONCE=True, exiting.'); break
    else:
        try: import readline
        except Exception: pass
        agent.inc_out = True
        while True:
            q = input('> ').strip()
            if not q: continue
            try:
                dq = agent.put_task(q, source='user')
                while True:
                    item = dq.get()
                    if 'next' in item: print(item['next'], end='', flush=True)
                    if 'done' in item: print(); break
            except KeyboardInterrupt:
                agent.abort()
                print('\n[Interrupted]')
