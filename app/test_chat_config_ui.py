"""Isolated editor/send regression checks; never use real credentials or APIs."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QObject,Signal

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication,QWidget,QDialog
from chat_window import ProviderDialog,ChatWindow
from chat_config import CHAT_DEFAULT,load_chat_config,load_provider_key,save_chat_config
from desktop_core import atomic_json
from desktop_services import SecretStore

APP=QApplication.instance() or QApplication([])


class FakeModels(QObject):
    loaded=Signal(object)
    failed=Signal(str)
    def __init__(self,parent=None):super().__init__(parent);self.cancelled=False;self.started=False
    def start(self):self.started=True
    def cancel(self):self.cancelled=True


class ChatConfigUiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.windows=[]
    def tearDown(self):
        for window in reversed(self.windows):
            if isinstance(window,ChatWindow):window.shutdown()
            window.deleteLater()
        APP.processEvents();self.temp.cleanup()
    def seed(self):
        config=copy.deepcopy(CHAT_DEFAULT)
        config.update(active='a',providers=[dict(id='a',name='Test service',base_url='http://localhost:1234/v1',model='test-model',timeout=90)])
        atomic_json(self.root/'chat.json',config);return config
    def dialog(self):
        dialog=ProviderDialog(self.root,embedded=True);self.windows.append(dialog);return dialog
    def chat(self):
        pet=QWidget();pet.settings={};pet.manifest={};self.windows.append(pet)
        chat=ChatWindow(pet,self.root,embedded=True);self.windows.append(chat);return chat
    def test_broken_documents_open_safely_and_cannot_be_overwritten(self):
        for raw in ('[]','{"providers":','{"providers":[{"id":"x","model":6}]}','{"history_chars":"many"}'):
            with self.subTest(raw=raw):
                path=self.root/'chat.json';path.write_text(raw,encoding='utf-8');dialog=self.dialog()
                self.assertTrue(dialog.load_error);self.assertIn('原文件已保留',dialog.status.text())
                dialog.fields['base_url'].setText('http://localhost/v1');dialog.fields['model'].setText('test');dialog.save();dialog.add();dialog.remove_confirmed('x')
                self.assertEqual(path.read_text(encoding='utf-8'),raw)
                self.assertFalse((self.root/'credentials.dpapi.json').exists())
    def test_broken_credentials_block_editor_even_with_no_provider(self):
        path=self.root/'credentials.dpapi.json';path.write_text('{broken',encoding='utf-8')
        dialog=self.dialog();self.assertTrue(dialog.load_error)
        dialog.fields['base_url'].setText('http://localhost/v1');dialog.fields['model'].setText('test');dialog.save()
        self.assertEqual(path.read_text(encoding='utf-8'),'{broken');self.assertFalse((self.root/'chat.json').exists())
    def test_unreadable_config_is_not_treated_as_new_install(self):
        self.seed()
        with patch('chat_window.load_chat_config',side_effect=OSError('无法读取 chat.json')):dialog=self.dialog()
        self.assertTrue(dialog.load_error);self.assertTrue(all(not button.isEnabled() for button in dialog.mutation_buttons))
    def test_invalid_draft_never_replaces_active_saved_configuration(self):
        self.seed();before=(self.root/'chat.json').read_bytes();dialog=self.dialog();dialog.add()
        for address,model,field in (('','test','base_url'),('bad-address','test','base_url'),('http://localhost/v1',' ','model'),('https://api.example/v1/responses','test','base_url')):
            with self.subTest(address=address,model=model):
                dialog.fields['base_url'].setText(address);dialog.fields['model'].setText(model);dialog.show();APP.processEvents();dialog.save()
                self.assertEqual((self.root/'chat.json').read_bytes(),before)
                self.assertIn('配置尚未保存',dialog.status.text());self.assertNotEqual(dialog.result(),QDialog.DialogCode.Accepted)
                self.assertEqual(dialog.focusWidget(),dialog.fields[field])
    def test_missing_legacy_address_opens_and_can_be_edited(self):
        config=self.seed();config['providers'][0].pop('base_url');atomic_json(self.root/'chat.json',config)
        dialog=self.dialog();self.assertFalse(dialog.load_error);dialog.save();self.assertIn('请填写',dialog.status.text())
        dialog.fields['base_url'].setText('http://localhost/custom');dialog.save();self.assertEqual(load_chat_config(self.root)['providers'][0]['base_url'],'http://localhost/custom')
    def test_endpoint_preview_matches_base_and_full_endpoint_without_guessing_v1(self):
        dialog=self.dialog()
        for address,expected in (('http://localhost:1234','http://localhost:1234/chat/completions'),('https://api.example/custom/v2','https://api.example/custom/v2/chat/completions'),('https://api.example/v1/chat/completions/','https://api.example/v1/chat/completions')):
            dialog.fields['base_url'].setText(address);self.assertEqual(dialog.endpoint.text(),expected)
        dialog.fields['base_url'].setText('https://sk-secret@api.example/v1');self.assertNotIn('sk-secret',dialog.endpoint.text())
    @unittest.skipUnless(os.name=='nt','DPAPI uses current Windows account')
    def test_save_blank_key_keeps_secret_and_displays_only_presence(self):
        config=self.seed();SecretStore(self.root/'credentials.dpapi.json').put('a','isolated-legacy-key')
        dialog=self.dialog();self.assertIn('已保存密钥',dialog.key_status.text());self.assertEqual(dialog.fields['key'].text(),'')
        self.assertNotIn('isolated-legacy-key',dialog.key_status.text());dialog.fields['model'].setText('new-model');dialog.save()
        saved=load_chat_config(self.root);self.assertEqual(saved['providers'][0]['model'],'new-model');self.assertEqual(load_provider_key(self.root,saved['providers'][0]),'isolated-legacy-key')
    @unittest.skipUnless(os.name=='nt','DPAPI uses current Windows account')
    def test_failed_commit_preserves_usable_previous_service_and_editable_draft(self):
        config=self.seed();SecretStore(self.root/'credentials.dpapi.json').put('a','isolated-old-key');dialog=self.dialog()
        dialog.fields['model'].setText('new-model');dialog.fields['key'].setText('isolated-new-key')
        with patch('chat_config.atomic_json',side_effect=OSError('disk full')):dialog.save()
        saved=load_chat_config(self.root);self.assertEqual(saved['providers'][0]['model'],'test-model');self.assertEqual(load_provider_key(self.root,saved['providers'][0]),'isolated-old-key')
        self.assertEqual(dialog.fields['model'].text(),'new-model');self.assertEqual(dialog.fields['key'].text(),'isolated-new-key')
        dialog.save();saved=load_chat_config(self.root);self.assertEqual(load_provider_key(self.root,saved['providers'][0]),'isolated-new-key')
    @unittest.skipUnless(os.name=='nt','DPAPI uses current Windows account')
    def test_clear_is_confirmed_and_does_not_save_other_drafts_or_revive_key(self):
        config=self.seed();SecretStore(self.root/'credentials.dpapi.json').put('a','isolated-old-key');dialog=self.dialog();dialog.fields['model'].setText('pending-model')
        dialog.clear_key();dialog.confirmation.cancel();self.assertEqual(load_provider_key(self.root,config['providers'][0]),'isolated-old-key')
        dialog.clear_key();dialog.confirmation.confirm();saved=load_chat_config(self.root)
        self.assertEqual(saved['providers'][0]['model'],'test-model');self.assertEqual(load_provider_key(self.root,saved['providers'][0]),'')
        self.assertEqual(dialog.fields['model'].text(),'pending-model');dialog.save();saved=load_chat_config(self.root)
        self.assertEqual(saved['providers'][0]['model'],'pending-model');self.assertEqual(load_provider_key(self.root,saved['providers'][0]),'')
    def test_invalid_send_does_not_start_network_or_lose_input(self):
        config=self.seed();config['providers'][0]['base_url']='https://api.example/v1/responses';atomic_json(self.root/'chat.json',config)
        chat=self.chat();chat.input.setPlainText('preserve my message')
        with patch('chat_window.ChatStream') as stream:chat.send();stream.assert_not_called()
        self.assertEqual(chat.input.toPlainText(),'preserve my message');self.assertFalse(chat.busy);self.assertEqual(chat.session['messages'],[])
        self.assertIn('Chat Completions',chat.status.text())
    def test_corrupt_config_or_credentials_send_fails_safely(self):
        config=self.seed();chat=self.chat();chat.input.setPlainText('preserve my message')
        (self.root/'chat.json').write_text('[]',encoding='utf-8')
        with patch('chat_window.ChatStream') as stream:chat.send();stream.assert_not_called()
        self.assertIn('原文件和输入已保留',chat.status.text());self.assertEqual(chat.input.toPlainText(),'preserve my message')
        atomic_json(self.root/'chat.json',config);(self.root/'credentials.dpapi.json').write_text('{broken',encoding='utf-8')
        with patch('chat_window.ChatStream') as stream:chat.send();stream.assert_not_called()
        self.assertIn('原文件和输入已保留',chat.status.text());self.assertEqual(chat.input.toPlainText(),'preserve my message')
    def test_embedded_save_returns_to_chat_without_claiming_connection_was_tested(self):
        chat=self.chat();chat.input.setPlainText('draft');chat.provider();dialog=chat.provider_dialog
        dialog.fields['base_url'].setText('http://localhost/v1');dialog.fields['model'].setText('test');dialog.save()
        self.assertEqual(chat.pages.currentWidget(),chat.chat_body);self.assertEqual(chat.input.toPlainText(),'draft');self.assertIn('尚未验证连通',chat.status.text())
    def test_local_service_save_reopen_and_switch_preserves_api_draft(self):
        self.seed();dialog=self.dialog();dialog.fields['model'].setText('api-draft');dialog.add_local()
        self.assertTrue(dialog.fields['base_url'].isHidden());self.assertTrue(dialog.fields['key'].isHidden())
        self.assertIn('无需 API 密钥',dialog.connection_note.text());dialog.add_local()
        self.assertEqual(len(dialog.config['providers']),2);dialog.save()
        saved=load_chat_config(self.root);self.assertEqual(saved['providers'][0]['model'],'test-model')
        self.assertEqual(saved['providers'][1]['kind'],'chatgpt_local');self.assertEqual(saved['providers'][1]['model'],'')
        again=self.dialog();self.assertEqual(again.current['kind'],'chatgpt_local')
        dialog.select.setCurrentIndex(0);self.assertEqual(dialog.fields['model'].text(),'api-draft')
        self.assertFalse(dialog.fields['base_url'].isHidden())
    def test_local_send_uses_harness_without_reading_api_key_and_ignores_late_reply(self):
        config=self.seed();config['providers'][0].update(kind='chatgpt_local',base_url='',model='');atomic_json(self.root/'chat.json',config)
        chat=self.chat();chat.input.setPlainText('你好')
        with patch('chat_window.HarnessStream') as harness,patch('chat_window.ChatStream') as api,patch('chat_config._store',side_effect=AssertionError('must not read API secrets')):
            chat.send();api.assert_not_called();stream=harness.return_value
            self.assertTrue(chat.busy);self.assertEqual(stream.start.call_args.args[1],'')
            self.assertEqual(stream.start.call_args.args[2][-1],dict(role='user',content='你好'))
            chat.stop();chat.on_done(stream,'迟到内容');chat.on_delta(stream,'迟到内容')
        self.assertEqual(chat.session['messages'],[dict(role='user',content='你好')]);self.assertFalse(chat.busy)
    def local_dialog(self,model=''):
        config=self.seed();config['providers'][0].update(kind='chatgpt_local',model=model,base_url='')
        atomic_json(self.root/'chat.json',config)
        dialog=self.dialog();dialog.show();APP.processEvents();return dialog
    @patch('chat_window.HarnessModels',FakeModels)
    def test_local_model_menu_load_select_save_and_reopen(self):
        dialog=self.local_dialog();job=dialog.models_job
        self.assertTrue(job.started);self.assertTrue(dialog.fields['model'].isHidden())
        self.assertFalse(dialog.model_choices.isHidden());self.assertIn('正在读取',dialog.model_notice.text())
        job.loaded.emit([dict(model='local-a',name='模型 A'),dict(model='local-b',name='模型 B')])
        self.assertEqual(dialog.local_model.count(),3);self.assertEqual(dialog.local_model.itemData(0),'')
        dialog.local_model.showPopup();APP.processEvents()
        panel=dialog.local_model._panel;self.assertIsNotNone(panel)
        panel.choose(panel.list.item(2));self.assertEqual(dialog.fields['model'].text(),'local-b')
        dialog.save();saved=load_chat_config(self.root);self.assertEqual(saved['providers'][0]['model'],'local-b')
        again=self.dialog();self.assertEqual(again.fields['model'].text(),'local-b')
        self.assertEqual(again.local_model.currentData(),'local-b')
        again.local_model.setCurrentIndex(0);again.save()
        self.assertEqual(load_chat_config(self.root)['providers'][0]['model'],'')
    @patch('chat_window.HarnessModels',FakeModels)
    def test_catalog_refresh_and_failure_keep_current_choice_and_cached_list(self):
        dialog=self.local_dialog('saved-model');job=dialog.models_job
        job.loaded.emit([dict(model='available-model',name='可用模型')])
        self.assertEqual(dialog.fields['model'].text(),'saved-model');self.assertIn('未在列表中',dialog.local_model.currentText())
        dialog.local_model.setCurrentIndex(dialog.local_model.findData('available-model'))
        dialog.refresh_models();job=dialog.models_job;job.failed.emit('登录暂不可读')
        self.assertEqual(dialog.fields['model'].text(),'available-model');self.assertEqual(dialog.local_model.currentData(),'available-model')
        self.assertGreaterEqual(dialog.local_model.findData('available-model'),0);self.assertTrue(dialog.model_refresh.isEnabled())
        self.assertIn('原选择已保留',dialog.model_notice.text())
        dialog.refresh_models();self.assertIsNotNone(dialog.models_job)
    @patch('chat_window.HarnessModels',FakeModels)
    def test_hidden_or_switched_service_ignores_stale_catalog_and_preserves_draft(self):
        dialog=self.local_dialog('saved-model');old=dialog.models_job
        dialog.hide();self.assertTrue(old.cancelled);self.assertIsNone(dialog.models_job)
        old.loaded.emit([dict(model='stale',name='迟到模型')]);self.assertEqual(dialog.local_models,[])
        dialog.show();APP.processEvents();job=dialog.models_job
        dialog.add();self.assertTrue(job.cancelled);self.assertFalse(dialog.fields['model'].isHidden())
        job.failed.emit('迟到错误');self.assertNotIn('迟到错误',dialog.model_notice.text())
        dialog.select.setCurrentIndex(0);self.assertEqual(dialog.fields['model'].text(),'saved-model')
    @patch('chat_window.HarnessModels',FakeModels)
    def test_effort_uses_model_capabilities_and_saved_selection_reopens(self):
        dialog=self.local_dialog();job=dialog.models_job
        models=[dict(model='model-a',name='模型 A',efforts=['low','medium','high'],is_default=True),dict(model='model-b',name='模型 B',efforts=['low'])]
        job.loaded.emit(models)
        self.assertEqual([dialog.effort_choice.itemData(i) for i in range(dialog.effort_choice.count())],['','low','medium','high'])
        dialog.effort_choice.setCurrentIndex(dialog.effort_choice.findData('high'));self.assertEqual(dialog.effort_value,'high')
        dialog.save();self.assertEqual(load_chat_config(self.root)['providers'][0]['reasoning_effort'],'high')
        again=self.dialog();self.assertEqual(again.effort_value,'high')
        again.local_models=models;again.fill_models();again.local_model.setCurrentIndex(again.local_model.findData('model-b'))
        self.assertEqual(again.effort_value,'');self.assertIn('新模型不支持',again.effort_notice.text())
        self.assertEqual([again.effort_choice.itemData(i) for i in range(again.effort_choice.count())],['','low'])
    @patch('chat_window.HarnessModels',FakeModels)
    def test_effort_refresh_failure_and_service_switch_preserve_draft(self):
        dialog=self.local_dialog('model-a');models=[dict(model='model-a',name='模型 A',efforts=['high'])]
        dialog.models_job.loaded.emit(models);dialog.effort_choice.setCurrentIndex(dialog.effort_choice.findData('high'))
        dialog.refresh_models();dialog.models_job.failed.emit('暂时失败');self.assertEqual(dialog.effort_value,'high')
        dialog.add();self.assertTrue(dialog.effort_box.isHidden());dialog.select.setCurrentIndex(0)
        self.assertEqual(dialog.effort_value,'high')
        APP.processEvents()
        dialog.models_job.loaded.emit(models);self.assertEqual(dialog.effort_value,'high')
        dialog.refresh_models();dialog.models_job.loaded.emit([dict(model='model-a',name='模型 A',efforts=['low'])])
        self.assertEqual(dialog.effort_value,'high');self.assertIn('不支持原强度',dialog.effort_notice.text())
        before=(self.root/'chat.json').read_bytes();dialog.save();self.assertEqual((self.root/'chat.json').read_bytes(),before)
        self.assertIn('不支持所选推理强度',dialog.status.text())
        dialog.effort_choice.setCurrentIndex(0);dialog.save();self.assertEqual(load_chat_config(self.root)['providers'][0]['reasoning_effort'],'')


if __name__=='__main__':unittest.main()
