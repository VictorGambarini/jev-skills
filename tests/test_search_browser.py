"""Keyless safety tests. Real WebKit suite is opt-in in scripts/."""
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from jevkit.search_browser import SearchBrowser, SearchError, StaleAction, origin

ROOT = Path(__file__).resolve().parents[1]


class SearchSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.binary = Path(self.tmp.name) / 'Search'
        self.binary.write_bytes(b'unit fixture not executable')
        self.args = dict(sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                         allowed_origins=['http://127.0.0.1:9999'], consent=True)

    def make(self, **kwargs):
        return SearchBrowser(self.binary, **{**self.args, **kwargs})

    def test_consent_and_pin(self):
        for overrides in ({'consent':False}, {'sha256':'0'*64}, {'timeout':31}, {'max_actions':0}, {'allowed_origins':[]}):
            with self.subTest(overrides=overrides), self.assertRaises(SearchError):
                self.make(**overrides)

    def test_local_origins_only(self):
        for u in ('https://example.com','file:///etc/passwd','http://127.0.0.1.evil.test','http://user@localhost:99'):
            with self.subTest(url=u), self.assertRaises(SearchError):
                origin(u)
        self.assertEqual(origin('http://localhost:23/x'), ('http','localhost',23))
        with self.assertRaises(SearchError):
            self.make()._allowed('http://127.0.0.1:9998')

    def test_owned_tab_and_budget(self):
        b=self.make(max_actions=1)
        with self.assertRaises(SearchError): b._owned('unknown')
        b._budget()
        with self.assertRaises(SearchError): b._budget()

    def test_stale_ticket(self):
        b=self.make(); b.tabs.add('a')
        with self.assertRaises(StaleAction): b.act('a','stale','id',expect={'url':'http://127.0.0.1:9999'})

    def test_invalid_effect_does_not_consume(self):
        b=self.make();b.tabs.add('a');b.tickets['a']={'token':'t','actions':{'go':{}}}
        with self.assertRaises(SearchError): b.act('a','t','go',expect={'js':'danger()'})
        self.assertIn('a', b.tickets)

    def test_unknown_action_refused(self):
        b=self.make();b._page=Mock()
        for action in ({'id':'x','op':'eval','selector':'body'}, {'id':'abstain','op':'click','selector':'button'}):
            with self.assertRaises(SearchError): b.observe('a',[action])

    def test_typed_values_not_in_jev_payload(self):
        b=self.make();b._page=Mock();b._js=Mock(return_value=json.dumps({'url':'http://127.0.0.1:9999','count':1}))
        ob=b.observe('a',[{'id':'name','op':'type','selector':'#name','text':'SYNTHETIC_PRIVATE_VALUE','description':'Enter fixture name'}])
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE',json.dumps(ob))
        chooser=Mock(return_value={'selected_id':'reobserve'})
        b.choose(ob,'Fill fixture',chooser=chooser)
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE',str(chooser.call_args))
        self.assertTrue(chooser.call_args.args[0]['regions'])

    def test_ambiguous_transport_poisons_world(self):
        b=self.make();b.process=Mock();b.process.poll.return_value=None
        b.sock=Mock();b.sock.lstat.return_value=Mock(st_mode=stat.S_IFSOCK|0o600,st_uid=os.getuid())
        with patch('jevkit.search_browser.socket.socket') as factory:
            factory.return_value.__enter__.return_value.connect.side_effect=socket.timeout()
            with self.assertRaises(SearchError): b._command('tabs')
        self.assertTrue(b.poisoned)
        with self.assertRaises(SearchError): b._command('click')

    def test_insecure_socket_refused(self):
        b=self.make();b.process=Mock();b.process.poll.return_value=None;b.sock=Mock()
        for mode, uid in ((stat.S_IFLNK|0o600,os.getuid()),(stat.S_IFSOCK|0o666,os.getuid()),(stat.S_IFSOCK|0o600,os.getuid()+1)):
            b.sock.lstat.return_value=Mock(st_mode=mode,st_uid=uid)
            with self.assertRaises(SearchError): b._command('tabs')

    def test_wrong_peer_refused(self):
        b=self.make();b.process=Mock(pid=123);b.process.poll.return_value=None;b.sock=Mock()
        b.sock.lstat.return_value=Mock(st_mode=stat.S_IFSOCK|0o600,st_uid=os.getuid())
        with patch('jevkit.search_browser.socket.socket') as factory:
            factory.return_value.__enter__.return_value.getsockopt.return_value=(124).to_bytes(4,sys.byteorder)
            with self.assertRaises(SearchError): b._command('tabs')

    def test_scoped_installer_preserves_everything_else(self):
        home=Path(self.tmp.name)/'home';home.mkdir();(home/'config.yaml').write_text('plugins: {}')
        folder=home/'plugins/hermes-jev/jevkit';folder.mkdir(parents=True)
        (folder/'__init__.py').write_text('# existing')
        (folder/'file_evidence.py').write_text('# unpromoted local work')
        skill=home/'skills/untouched/SKILL.md';skill.parent.mkdir(parents=True);skill.write_text('existing')
        cmd=[sys.executable,str(ROOT/'install.py'),'--hermes-root-only','--search-browser-only','--hermes-home',str(home)]
        subprocess.run(cmd+['--check'],check=True,capture_output=True)
        self.assertFalse((folder/'search_browser.py').exists())
        subprocess.run(cmd,check=True,capture_output=True)
        self.assertEqual((folder/'search_browser.py').read_bytes(),(ROOT/'jevkit/search_browser.py').read_bytes())
        self.assertEqual((folder/'file_evidence.py').read_text(),'# unpromoted local work')
        self.assertEqual(skill.read_text(),'existing')
        self.assertFalse((home/'bin').exists())
        self.assertEqual((home/'config.yaml').read_text(),'plugins: {}')


if __name__=='__main__':unittest.main()
