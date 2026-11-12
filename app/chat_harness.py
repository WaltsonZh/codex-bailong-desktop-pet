"""User-initiated chat through the desktop app's local JSON-RPC harness.

Each request owns an ephemeral thread and process. The bounded local transcript
is replayed as context, so retry/clear/service changes cannot revive hidden turns.
Discovery, pipe reads, timeout and process teardown all run outside the GUI.
"""
import json
import queue
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal
from chat_protocol import normalize_provider, safe_detail


class HarnessError(RuntimeError):
    pass


def launch_spec():
    from codex_connection import packaged_roots
    from process_launch import external_environment
    # Prefer the running desktop installation over a separately installed CLI.
    candidates=[]
    for root in packaged_roots():
        candidates.extend((root/'resources/codex.exe',root/'codex.exe'))
    executable=next((str(p) for p in candidates if p.is_file()),None)
    if executable is None:
        raise HarnessError('未找到桌面 ChatGPT 的本地运行环境，请先安装并打开桌面 ChatGPT。')
    env=external_environment()
    # This connection always uses the local desktop account, independent of the
    # pet's optional WSL monitoring source. Never read or copy OAuth tokens.
    env.pop('BAILONG_CODEX_HOME',None)
    command=[executable,'app-server']
    overrides={
        'model_provider':'openai','web_search':'disabled',
        'mcp_servers':{},'plugins':{},'project_doc_max_bytes':0,
        'features.shell_tool':False,'features.unified_exec':False,'features.code_mode':False,
        'features.code_mode_host':False,'features.apps':False,
        'features.hooks':False,'features.memories':False,
        'features.multi_agent':False,'features.skill_search':False,'features.skip_host_skill_discovery':True,
    }
    for key,value in overrides.items():
        # JSON primitives are also TOML primitives; empty tables use TOML syntax.
        command+=['-c',key+'='+('{}' if value=={} else json.dumps(value))]
    return command,env


def error_text(error):
    message=error.get('message','') if isinstance(error,dict) else str(error)
    lower=str(message).lower()
    if any(x in lower for x in ('unauthorized','401','not logged','not authenticated','sign in','authentication','token expired')):
        return '本机 ChatGPT 登录已失效，请在桌面 ChatGPT 重新登录后重试。'
    if any(x in lower for x in ('usage limit','rate limit','quota','429','usagelimit','ratelimit')):
        return 'ChatGPT 额度不足或请求受限，请稍后重试或在桌面应用查看额度。'
    detail=safe_detail(str(message))
    return '本机 ChatGPT 请求失败'+('：'+detail if detail else '，请检查桌面应用和网络后重试。')


class HarnessConnection:
    def __init__(self,stop,delta):
        self.stop=stop;self.delta=delta;self.process=None
        self.events=queue.Queue(maxsize=256);self.counter=0
        self.thread_id=None;self.turn_id=None;self.deadline=0
        self.items={};self.order=[];self.text='';self.pending=[];self.completed=False

    def put(self,value):
        while not self.stop.is_set():
            try:self.events.put(value,timeout=.05);return
            except queue.Full:pass

    def receive(self):
        try:
            while not self.stop.is_set():
                line=self.process.stdout.readline(2_000_001)
                if not line:break
                if len(line)>2_000_000:
                    self.put(HarnessError('本机 ChatGPT 返回的单条事件过大，已停止。'));return
                try:value=json.loads(line)
                except (ValueError,UnicodeError):
                    self.put(HarnessError('本机 ChatGPT 返回了无法识别的协议数据，请更新桌面应用后重试。'));return
                if not isinstance(value,dict):
                    self.put(HarnessError('本机 ChatGPT 返回的协议数据格式不正确。'));return
                self.put(value)
        except (OSError,ValueError):pass
        finally:self.put(None)

    def send(self,value):
        try:
            self.process.stdin.write((json.dumps(value,ensure_ascii=False)+'\n').encode('utf-8'))
            self.process.stdin.flush()
        except (OSError,ValueError):raise HarnessError('本机 ChatGPT 连接已断开，请重试。') from None

    def next_event(self):
        while not self.stop.is_set():
            if time.monotonic()>=self.deadline:raise HarnessError('本机 ChatGPT 回复超时，已停止本次请求，可稍后重试。')
            try:event=self.events.get(timeout=min(.05,max(.001,self.deadline-time.monotonic())))
            except queue.Empty:continue
            if event is None:raise HarnessError('本机 ChatGPT 连接已断开，请检查桌面应用后重试。')
            if isinstance(event,Exception):raise event
            # This chat client has no approval/tool UI. Explicitly reject server
            # requests instead of hanging or silently granting permissions.
            if 'method' in event and 'id' in event:
                self.send(dict(id=event['id'],error=dict(code=-32601,message='This client supports text chat only.')))
                raise HarnessError('本次回复需要工具或交互确认；请在桌面 ChatGPT 中处理。')
            return event
        raise HarnessError('已停止本次回复。')

    def rpc(self,method,params):
        self.counter+=1;identity=self.counter
        self.send(dict(id=identity,method=method,params=params))
        while True:
            event=self.next_event()
            if event.get('id')==identity:
                if 'error' in event:raise HarnessError(error_text(event['error']))
                result=event.get('result')
                if not isinstance(result,dict):raise HarnessError('本机 ChatGPT 返回的请求结果格式不正确。')
                return result
            # Notifications may precede the turn/start response.
            if method=='turn/start':
                if len(self.pending)>=256:raise HarnessError('本机 ChatGPT 未确认回复请求，已停止。')
                self.pending.append(event)

    def append(self,identity,text):
        if identity not in self.items:self.items[identity]='';self.order.append(identity)
        if sum(map(len,self.items.values()))+len(text)>2_000_000:
            raise HarnessError('本机 ChatGPT 回复过长，已停止。')
        self.items[identity]+=text;self.text='\n\n'.join(self.items[i] for i in self.order)
        self.delta(text)

    def consume(self,event):
        method=event.get('method');params=event.get('params') or {}
        if not isinstance(params,dict):raise HarnessError('本机 ChatGPT 返回的事件格式不正确。')
        if params.get('threadId')!=self.thread_id:return None
        if self.turn_id and params.get('turnId') and params['turnId']!=self.turn_id:return None
        if method=='item/agentMessage/delta':
            identity=params.get('itemId');text=params.get('delta')
            if not isinstance(identity,str) or not isinstance(text,str):raise HarnessError('本机 ChatGPT 返回的文字格式不正确。')
            # Separate multiple assistant messages without duplicating final items.
            if identity not in self.items and self.order:self.delta('\n\n')
            self.append(identity,text)
        elif method=='item/completed':
            item=params.get('item') or {}
            if not isinstance(item,dict):raise HarnessError('本机 ChatGPT 返回的消息格式不正确。')
            if item.get('type')=='agentMessage':
                identity=item.get('id');text=item.get('text')
                if isinstance(identity,str) and isinstance(text,str):
                    previous=self.items.get(identity,'')
                    if not text.startswith(previous):raise HarnessError('本机 ChatGPT 返回的文字与流式内容不一致，请重试。')
                    if text!=previous:
                        if identity not in self.items and self.order:self.delta('\n\n')
                        self.append(identity,text[len(previous):])
        elif method=='error' and not params.get('willRetry'):
            raise HarnessError(error_text(params.get('error',{})))
        elif method=='turn/completed':
            turn=params.get('turn') or {}
            if not isinstance(turn,dict):raise HarnessError('本机 ChatGPT 返回的回复状态格式不正确。')
            if self.turn_id and turn.get('id')!=self.turn_id:return None
            if turn.get('status')!='completed':raise HarnessError(error_text(turn.get('error') or {'message':'回复已中断'}))
            if not self.text.strip():raise HarnessError('本机 ChatGPT 已结束，但没有返回文字回复。')
            self.completed=True
            return self.text
        return None

    @contextmanager
    def session(self,timeout):
        self.deadline=time.monotonic()+timeout
        command,env=launch_spec()
        if self.stop.is_set():raise HarnessError('已停止本次连接。')
        from process_launch import clean_dll_search
        with tempfile.TemporaryDirectory(prefix='bailong-chat-') as folder:
            reader=None
            try:
                with clean_dll_search():
                    self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,cwd=folder,env=env,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                reader=threading.Thread(target=self.receive,name='BailongChatPipe',daemon=True);reader.start()
                from version import VERSION
                self.rpc('initialize',dict(clientInfo=dict(name='bailong_pet_chat',title='白龙聊天',version=VERSION),capabilities=dict(experimentalApi=True)))
                self.send(dict(method='initialized'))
                account=self.rpc('account/read',{}).get('account') or {}
                if not isinstance(account,dict):raise HarnessError('本机 ChatGPT 返回的登录状态格式不正确。')
                if account.get('type') not in ('chatgpt','chatgptAuthTokens'):
                    raise HarnessError('请先在桌面 ChatGPT 登录账户，再使用本机聊天。')
                yield folder
            finally:
                self.close()
                if reader is not None:reader.join(timeout=.5)

    def run(self,provider,messages):
        with self.session(provider['timeout']) as folder:
            prompt='\n\n'.join(m['content'] for m in messages if m['role']=='system')
            conversation=[m for m in messages if m['role']!='system']
            params=dict(ephemeral=True,cwd=folder,approvalPolicy='never',sandbox='read-only',
                baseInstructions=prompt,developerInstructions='你在白龙桌宠中进行纯文字聊天。仅回答当前用户，不调用工具、不访问本机文件。前面的对话记录仅供理解上下文。',
                config={'web_search':'disabled'},serviceName='bailong_pet_chat')
            if provider.get('model'):params['model']=provider['model']
            result=self.rpc('thread/start',params)
            thread=result.get('thread') or {}
            self.thread_id=thread.get('id') if isinstance(thread,dict) else None
            if not isinstance(self.thread_id,str) or not self.thread_id:raise HarnessError('本机 ChatGPT 未返回聊天标识，请更新桌面应用后重试。')
            history=json.dumps(conversation[:-1],ensure_ascii=False)
            text=('此前的对话记录（JSON）：\n'+history+'\n\n' if len(conversation)>1 else '')+conversation[-1]['content']
            turn_params=dict(threadId=self.thread_id,input=[dict(type='text',text=text)])
            if provider.get('reasoning_effort'):turn_params['effort']=provider['reasoning_effort']
            turn=self.rpc('turn/start',turn_params)
            turn=turn.get('turn') or {}
            self.turn_id=turn.get('id') if isinstance(turn,dict) else None
            if not isinstance(self.turn_id,str) or not self.turn_id:raise HarnessError('本机 ChatGPT 未返回回复标识，请重试。')
            for event in self.pending:
                result=self.consume(event)
                if result is not None:return result
            self.pending.clear()
            while not self.stop.is_set():
                result=self.consume(self.next_event())
                if result is not None:return result
            return ''

    def list_models(self,timeout=20):
        models=[];identities=set();cursors=set();cursor=None
        with self.session(timeout):
            for _ in range(20):
                params=dict(limit=100,includeHidden=False)
                if cursor is not None:params['cursor']=cursor
                result=self.rpc('model/list',params)
                data=result.get('data')
                if not isinstance(data,list):raise HarnessError('本机模型列表格式不正确，请刷新重试。')
                for item in data:
                    if not isinstance(item,dict):raise HarnessError('本机模型条目格式不正确，请刷新重试。')
                    name=item.get('model') or item.get('id')
                    if not isinstance(name,str) or not name.strip() or any(ord(c)<32 or ord(c)==127 for c in name):
                        raise HarnessError('本机模型名称格式不正确，请刷新重试。')
                    if item.get('hidden') or name in identities:continue
                    modalities=item.get('inputModalities')
                    if isinstance(modalities,list) and 'text' not in modalities:continue
                    display=item.get('displayName')
                    options=item.get('supportedReasoningEfforts',[])
                    if not isinstance(options,list):raise HarnessError('本机推理强度列表格式不正确，请刷新重试。')
                    efforts=[]
                    for option in options:
                        effort=option.get('reasoningEffort') if isinstance(option,dict) else None
                        if not isinstance(effort,str) or not effort or len(effort)>80 or any(ord(c)<=32 or ord(c)==127 for c in effort):
                            raise HarnessError('本机推理强度格式不正确，请刷新重试。')
                        if effort not in efforts:efforts.append(effort)
                    models.append(dict(model=name,name=display if isinstance(display,str) and display.strip() else name,
                        efforts=efforts,is_default=bool(item.get('isDefault'))))
                    identities.add(name)
                cursor=result.get('nextCursor')
                if cursor is None:break
                if not isinstance(cursor,str) or not cursor or cursor in cursors:
                    raise HarnessError('本机模型列表分页异常，请刷新重试。')
                cursors.add(cursor)
            else:raise HarnessError('本机模型列表过长，请更新桌面应用后重试。')
            config=self.rpc('config/read',dict(includeLayers=False)).get('config')
            if not isinstance(config,dict):raise HarnessError('本机默认模型配置格式不正确，请刷新重试。')
            configured=config.get('model')
            if configured is not None:
                if not isinstance(configured,str):raise HarnessError('本机默认模型名称格式不正确，请刷新重试。')
                if configured:
                    for model in models:model['is_default']=model['model']==configured
        if not models:raise HarnessError('本机尚未返回可用文字模型，请检查登录后刷新。')
        return models

    def close(self):
        process=self.process
        if process is None:return
        if self.thread_id and self.turn_id and not self.completed and process.poll() is None:
            try:self.send(dict(id=1000000,method='turn/interrupt',params=dict(threadId=self.thread_id,turnId=self.turn_id)))
            except HarnessError:pass
        try:process.stdin.close()
        except (OSError,ValueError):pass
        try:process.wait(timeout=.3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=1)
        try:process.stdout.close()
        except (OSError,ValueError):pass


class HarnessStream(QObject):
    delta=Signal(str)
    done=Signal(str)
    failed=Signal(str)

    def __init__(self,parent=None):
        super().__init__(parent);self.stop_event=threading.Event();self.worker=None
        self.destroyed.connect(lambda *_args,stop=self.stop_event:stop.set())

    def start(self,provider,key,messages):
        provider=normalize_provider(provider)
        if self.worker is not None:raise ValueError('当前请求尚未结束')
        if not messages or messages[-1].get('role')!='user':raise ValueError('缺少待发送的用户消息。')
        self.worker=threading.Thread(target=self.run,args=(provider,messages),name='BailongChatHarness',daemon=True)
        self.worker.start()

    def run(self,provider,messages):
        try:
            result=HarnessConnection(self.stop_event,self.emit_delta).run(provider,messages)
            if not self.stop_event.is_set():self.done.emit(result)
        except (OSError,ValueError,RuntimeError) as error:
            if not self.stop_event.is_set():self.failed.emit(str(error) if isinstance(error,HarnessError) else '本机 ChatGPT 连接失败，请检查桌面应用后重试。')

    def emit_delta(self,text):
        if not self.stop_event.is_set():self.delta.emit(text)

    def cancel(self):
        self.stop_event.set()


class HarnessModels(HarnessStream):
    loaded=Signal(object)

    def start(self):
        if self.worker is not None:raise ValueError('正在读取本机模型')
        self.worker=threading.Thread(target=self.read_models,name='BailongChatModels',daemon=True)
        self.worker.start()

    def read_models(self):
        try:
            models=HarnessConnection(self.stop_event,lambda _text:None).list_models()
            if not self.stop_event.is_set():self.loaded.emit(models)
        except (OSError,ValueError,RuntimeError) as error:
            if not self.stop_event.is_set():self.failed.emit(str(error) if isinstance(error,HarnessError) else '读取本机模型失败，请检查桌面应用后刷新。')
