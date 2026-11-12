"""Isolated JSON-RPC subprocess regressions; no account or paid endpoint."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest
from chat_harness import HarnessConnection, HarnessError, HarnessStream, launch_spec
from chat_protocol import normalize_provider, ProviderValidationError

APP=QCoreApplication.instance() or QCoreApplication([])
SERVER=r'''
import json,sys,time
from pathlib import Path
mode=sys.argv[1];audit=Path(sys.argv[2])
def send(value):print(json.dumps(value,ensure_ascii=False),flush=True)
for line in sys.stdin:
 m=json.loads(line)
 with audit.open('a',encoding='utf-8') as f:f.write(json.dumps(m)+'\n')
 method=m['method'];i=m.get('id')
 if method=='initialize':
  if mode=='timeout':time.sleep(8)
  if mode=='malformed':print('not-json',flush=True);continue
  send(dict(id=i,result={}))
 elif method=='account/read':send(dict(id=i,result=dict(account=dict(type='apikey' if mode=='auth' else 'chatgpt'))))
 elif method=='config/read':send(dict(id=i,result=dict(config=dict(model='model-b'))))
 elif method=='model/list':
  if mode=='models-effort-bad':send(dict(id=i,result=dict(data=[dict(model='model-a',supportedReasoningEfforts=[dict(reasoningEffort=7)])],nextCursor=None)));continue
  if mode=='models-bad':send(dict(id=i,result=dict(data='invalid')));continue
  if mode=='models-empty':send(dict(id=i,result=dict(data=[],nextCursor=None)));continue
  if mode=='models-cursor':send(dict(id=i,result=dict(data=[],nextCursor='repeat')));continue
  if not m['params'].get('cursor'):
   send(dict(id=i,result=dict(data=[dict(id='alias',model='model-a',displayName='模型 A',inputModalities=['text'],isDefault=True,supportedReasoningEfforts=[dict(reasoningEffort='low'),dict(reasoningEffort='high')]),dict(model='hidden',hidden=True),dict(model='audio',inputModalities=['audio'])],nextCursor='page-2')))
  else:send(dict(id=i,result=dict(data=[dict(model='model-a'),dict(id='model-b',displayName='模型 B',supportedReasoningEfforts=[dict(reasoningEffort='ultra')])],nextCursor=None)))
 elif method=='thread/start':
  if mode=='rpc-error':send(dict(id=i,error=dict(message='model not found sk-synthetic-secret')))
  else:send(dict(id=i,result=dict(thread=dict(id='thread-a'))))
 elif method=='turn/start':
  if mode=='disconnect':sys.exit(0)
  if mode=='early':
   send(dict(method='item/agentMessage/delta',params=dict(threadId='thread-a',turnId='turn-a',itemId='reply',delta='你好')))
   send(dict(method='turn/completed',params=dict(threadId='thread-a',turn=dict(id='turn-a',status='completed'))))
  send(dict(id=i,result=dict(turn=dict(id='turn-a'))))
  if mode=='early':continue
  if mode=='approval':
   send(dict(id=100,method='item/commandExecution/requestApproval',params=dict(threadId='thread-a')));continue
  send(dict(method='item/agentMessage/delta',params=dict(threadId='other',turnId='turn-a',itemId='wrong',delta='ignore')))
  send(dict(method='item/agentMessage/delta',params=dict(threadId='thread-a',turnId='old',itemId='wrong',delta='ignore')))
  if mode not in ('empty','final-only'):
   send(dict(method='item/agentMessage/delta',params=dict(threadId='thread-a',turnId='turn-a',itemId='reply',delta='你')))
   if mode=='cancel':continue
  if mode!='empty':send(dict(method='item/completed',params=dict(threadId='thread-a',turnId='turn-a',item=dict(id='reply',type='agentMessage',text='你好'))))
  if mode=='multiple':
   send(dict(method='item/agentMessage/delta',params=dict(threadId='thread-a',turnId='turn-a',itemId='reply2',delta='再见')))
   send(dict(method='item/completed',params=dict(threadId='thread-a',turnId='turn-a',item=dict(id='reply2',type='agentMessage',text='再见'))))
  status='failed' if mode=='failed' else 'completed'
  send(dict(method='turn/completed',params=dict(threadId='thread-a',turn=dict(id='turn-a',status=status,error=dict(message='usageLimitExceeded') if status=='failed' else None))))
'''


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.script=self.root/'server.py';self.script.write_text(SERVER,encoding='utf-8')
        self.audit=self.root/'requests.jsonl';self.streams=[]
    def tearDown(self):
        for stream in self.streams:
            stream.cancel();stream.worker.join(timeout=3);stream.deleteLater()
        APP.processEvents();self.temp.cleanup()
    def spec(self,mode):return ([sys.executable,str(self.script),mode,str(self.audit)],dict(os.environ,PYTHONIOENCODING='utf-8'))
    def provider(self):return normalize_provider(dict(kind='chatgpt_local',model='',timeout=5))
    def messages(self):return [dict(role='system',content='白龙角色'),dict(role='user',content='上一问'),dict(role='assistant',content='上一答'),dict(role='user',content='当前问题')]
    def requests(self):return [json.loads(x) for x in self.audit.read_text(encoding='utf-8').splitlines()]
    def run_case(self,mode):
        chunks=[];connection=HarnessConnection(threading.Event(),chunks.append)
        with patch('chat_harness.launch_spec',return_value=self.spec(mode)):
            result=connection.run(self.provider(),self.messages())
        self.assertIsNotNone(connection.process.poll())
        return result,chunks
    def test_stream_context_roles_and_ephemeral_thread(self):
        result,chunks=self.run_case('normal');self.assertEqual(result,'你好');self.assertEqual(''.join(chunks),result)
        requests=self.requests();start=next(x['params'] for x in requests if x['method']=='thread/start')
        self.assertTrue(start['ephemeral']);self.assertEqual(start['baseInstructions'],'白龙角色');self.assertNotIn('model',start)
        self.assertEqual(start['sandbox'],'read-only');self.assertEqual(start['approvalPolicy'],'never')
        self.assertNotIn('gpt-bailong-pet',start['cwd'])
        turn=next(x['params'] for x in requests if x['method']=='turn/start')
        text=turn['input'][0]['text'];self.assertIn('上一答',text);self.assertTrue(text.endswith('当前问题'));self.assertNotIn('白龙角色',text)
    def test_final_item_fallback_and_multiple_assistant_messages(self):
        for mode,expected in [('final-only','你好'),('early','你好'),('multiple','你好\n\n再见')]:
            with self.subTest(mode=mode):
                result,chunks=self.run_case(mode);self.assertEqual(result,expected);self.assertEqual(''.join(chunks),expected)
    def test_auth_protocol_disconnect_empty_failed_and_approval_errors(self):
        for mode,clue in [('auth','登录'),('malformed','协议'),('disconnect','断开'),('empty','没有返回文字'),('failed','额度'),('approval','确认'),('rpc-error','[密钥已隐藏]')]:
            with self.subTest(mode=mode),self.assertRaises(HarnessError) as caught:
                self.run_case(mode)
            self.assertIn(clue,str(caught.exception));self.assertNotIn('sk-synthetic-secret',str(caught.exception))
    def test_timeout_terminates_owned_process(self):
        connection=HarnessConnection(threading.Event(),lambda _:None);provider=self.provider();provider['timeout']=.12
        with patch('chat_harness.launch_spec',return_value=self.spec('timeout')),self.assertRaisesRegex(HarnessError,'超时'):
            connection.run(provider,self.messages())
        self.assertIsNotNone(connection.process.poll())
    def test_cancel_is_nonblocking_and_suppresses_late_signals(self):
        stream=HarnessStream();self.streams.append(stream);chunks=[];done=[];failed=[]
        stream.delta.connect(chunks.append);stream.done.connect(done.append);stream.failed.connect(failed.append)
        with patch('chat_harness.launch_spec',return_value=self.spec('cancel')):
            stream.start(self.provider(),'',self.messages());deadline=time.monotonic()+3
            while not chunks and time.monotonic()<deadline:QTest.qWait(10)
            self.assertEqual(chunks,['你']);started=time.perf_counter();stream.cancel()
            self.assertLess(time.perf_counter()-started,.05)
            deadline=time.monotonic()+3
            while stream.worker.is_alive() and time.monotonic()<deadline:QTest.qWait(10)
        self.assertFalse(stream.worker.is_alive());self.assertEqual(done,[]);self.assertEqual(failed,[])
        self.assertIn('turn/interrupt',[x['method'] for x in self.requests()])
    def test_desktop_binary_and_clean_environment_are_used(self):
        root=self.root/'desktop';(root/'resources').mkdir(parents=True);exe=root/'resources/codex.exe';exe.write_bytes(b'fixture')
        with patch('codex_connection.packaged_roots',return_value={root}),patch('process_launch.external_environment',return_value={'PATH':'clean','BAILONG_CODEX_HOME':'ignore'}):
            command,env=launch_spec()
        self.assertEqual(command[:2],[str(exe),'app-server']);self.assertEqual(env,{'PATH':'clean'})
        self.assertIn('features.hooks=false',command);self.assertIn('plugins={}',command)
        self.assertIn('features.shell_tool=false',command)
    def test_missing_desktop_does_not_silently_fall_back_to_another_service(self):
        with patch('codex_connection.packaged_roots',return_value=set()),self.assertRaisesRegex(HarnessError,'桌面 ChatGPT'):
            launch_spec()
    def test_local_model_optional_but_unknown_kind_rejected(self):
        self.assertEqual(self.provider()['model'],'')
        with self.assertRaises(ProviderValidationError):normalize_provider(dict(kind='unknown',model='x'))
    def test_catalog_uses_all_pages_filters_hidden_and_does_not_create_chat(self):
        connection=HarnessConnection(threading.Event(),lambda _:None)
        with patch('chat_harness.launch_spec',return_value=self.spec('models')):models=connection.list_models()
        self.assertEqual(models,[dict(model='model-a',name='模型 A',efforts=['low','high'],is_default=False),dict(model='model-b',name='模型 B',efforts=['ultra'],is_default=True)])
        requests=self.requests();self.assertNotIn('thread/start',[r['method'] for r in requests])
        queries=[r['params'] for r in requests if r['method']=='model/list']
        self.assertEqual(queries,[dict(limit=100,includeHidden=False),dict(limit=100,includeHidden=False,cursor='page-2')])
        self.assertIsNotNone(connection.process.poll())
    def test_explicit_selected_model_reaches_harness_thread(self):
        provider=self.provider();provider.update(model='model-b',reasoning_effort='ultra')
        with patch('chat_harness.launch_spec',return_value=self.spec('normal')):
            result=HarnessConnection(threading.Event(),lambda _:None).run(provider,self.messages())
        self.assertEqual(result,'你好')
        start=next(r['params'] for r in self.requests() if r['method']=='thread/start')
        self.assertEqual(start['model'],'model-b')
        turn=next(r['params'] for r in self.requests() if r['method']=='turn/start')
        self.assertEqual(turn['effort'],'ultra')
    def test_default_effort_is_omitted_and_invalid_values_are_rejected(self):
        with patch('chat_harness.launch_spec',return_value=self.spec('normal')):
            HarnessConnection(threading.Event(),lambda _:None).run(self.provider(),self.messages())
        turn=next(r['params'] for r in self.requests() if r['method']=='turn/start');self.assertNotIn('effort',turn)
        for invalid in (None,3,'high\n','high low'):
            with self.subTest(invalid=invalid),self.assertRaises(ProviderValidationError):normalize_provider(dict(self.provider(),reasoning_effort=invalid))
    def test_bad_empty_and_repeating_catalog_do_not_publish_partial_list(self):
        for mode,clue in [('models-bad','格式'),('models-effort-bad','强度格式'),('models-empty','尚未返回'),('models-cursor','分页'),('auth','登录')]:
            connection=HarnessConnection(threading.Event(),lambda _:None)
            with self.subTest(mode=mode),patch('chat_harness.launch_spec',return_value=self.spec(mode)),self.assertRaisesRegex(HarnessError,clue):
                connection.list_models()
            self.assertIsNotNone(connection.process.poll())


if __name__=='__main__':unittest.main()
