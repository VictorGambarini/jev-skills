"""Opt-in Search/WebKit bench backend, NOT a CDP replacement.

Only launches a fresh, consented test world from an explicitly pinned local build.
Only synthetic loopback sites are supported. An origin allowlist is an action
boundary, not a network firewall: use trusted fixtures without external resources.
No existing browser, cookie store, credential, or default routing is adopted.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit


class SearchError(RuntimeError):
    pass


class StaleAction(SearchError):
    pass


def origin(url):
    u = urlsplit(url)
    if u.scheme != 'http' or u.hostname not in ('127.0.0.1', 'localhost', '::1') or u.username or u.password:
        raise SearchError('Only explicit synthetic loopback HTTP fixtures are supported')
    return (u.scheme, u.hostname, u.port or 80)


class SearchBrowser:
    """Context-managed, single-owner browser. Caller must provide a resource lease.

    ``consent=True`` means permission to start THIS disposable automation world,
    not permission to attach to personal browsing or perform outbound actions.
    Mutations are one-shot tickets. Jev only selects caller-prevalidated IDs.
    """
    def __init__(self, binary, *, sha256, allowed_origins, consent=False, timeout: float=8, max_actions=30):
        if consent is not True:
            raise SearchError('Explicit disposable-world consent is required')
        self.binary = Path(binary).resolve(strict=True)
        if not re.fullmatch(r'[a-f0-9]{64}', sha256) or hashlib.sha256(self.binary.read_bytes()).hexdigest() != sha256:
            raise SearchError('Pinned build digest mismatch')
        if not allowed_origins or not 0 < timeout <= 30 or not 1 <= max_actions <= 200:
            raise SearchError('Explicit origins and bounded time/action budgets required')
        self.origins = {origin(u) for u in allowed_origins}
        self.timeout = timeout
        self.remaining = max_actions
        self.world = 'jev-' + uuid.uuid4().hex[:16]
        self.bundle = 'local.jev.search.' + self.world
        self.suite = 'com.officecommun.search.test.' + self.world
        self.support = Path.home() / 'Library/Application Support' / ('Search (' + self.world + ')')
        self.sock = self.support / 'bench.sock'
        self.process = None
        self.runtime = None
        self.tabs = set()
        self.tickets = {}
        self.poisoned = False
        self.started = False
        self.cleanup_receipt = None

    def __enter__(self):
        if sys.platform != 'darwin':
            raise SearchError('Search requires macOS; use the existing browser fallback')
        if self.started:
            raise SearchError('World cannot be reused')
        self.started = True
        try:
            self.runtime = Path(tempfile.mkdtemp(prefix=self.world + '-'))
            app = self.runtime / 'Automation Lab.app'
            exe = app / 'Contents/MacOS/Search'
            exe.parent.mkdir(parents=True)
            shutil.copy2(self.binary, exe)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': self.bundle, 'CFBundleName': 'Automation Lab',
                'CFBundleExecutable': 'Search', 'CFBundlePackageType': 'APPL', 'LSUIElement': True}))
            subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True, capture_output=True, timeout=20)
            for key in ('bench', 'welcomed'):
                subprocess.run(['defaults', 'write', self.suite, key, '-bool', 'true'], check=True, capture_output=True, timeout=5)
            tmp = self.runtime / 'tmp'
            tmp.mkdir(mode=0o700)
            env = dict(os.environ, SEARCH_PROBE=self.world, TMPDIR=str(tmp))
            env.pop('SEARCH_MEASURE', None)
            self.process = subprocess.Popen([str(exe)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            end = time.monotonic() + 20
            while not self.sock.exists():
                if self.process.poll() is not None or time.monotonic() > end:
                    raise SearchError('Owned Search did not start')
                time.sleep(.05)
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def _command(self, verb, **fields):
        if not self.process or self.process.poll() is not None or self.poisoned:
            raise SearchError('Owned world unavailable; ambiguous timeout requires new world')
        s = self.sock.lstat()
        if not stat.S_ISSOCK(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
            raise SearchError('Socket must be same-uid, non-symlink, mode 0600')
        deadline = time.monotonic() + self.timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.sock))
                # Darwin LOCAL_PEERPID pins the socket peer to our exact child,
                # beyond upstream's own same-uid credential check.
                peer = struct.unpack('i', client.getsockopt(0, 2, 4))[0]
                if peer != self.process.pid:
                    raise SearchError('Socket peer is not the owned process')
                client.sendall(json.dumps({'do': verb, **fields}).encode() + b'\n')
                chunks = bytearray()
                while b'\n' not in chunks:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError('Search deadline exceeded')
                    client.settimeout(left)
                    data = client.recv(65536)
                    if not data:
                        raise SearchError('Truncated response')
                    chunks.extend(data)
                    if len(chunks) > 2_000_000:
                        raise SearchError('Response size limit')
                result = json.loads(chunks.split(b'\n', 1)[0])
                if not isinstance(result, dict) or 'error' in result:
                    raise SearchError(str(result.get('error')) if isinstance(result, dict) else 'Invalid response')
                return result
        except (TimeoutError, OSError, ValueError) as exc:
            self.poisoned = True
            raise SearchError('Ambiguous transport failure; do not retry mutation') from exc

    def _allowed(self, url):
        if origin(url) not in self.origins:
            raise SearchError('Page left the allowed origin; stop, do not retry')

    def _owned(self, tab):
        if tab not in self.tabs:
            raise SearchError('Unknown or unowned tab')

    def _js(self, tab, code):
        self._owned(tab)
        return self._command('eval', id=tab, world='search', js=code)['value']

    def _page(self, tab):
        data = json.loads(self._js(tab, 'JSON.stringify({url:location.href,title:document.title})'))
        self._allowed(data['url'])
        return data

    def open(self, url):
        self._allowed(url)
        self._budget()
        data = self._command('open', url=url)
        if data.get('bench') is not True or not re.fullmatch('[a-f0-9]{8}', data.get('id', '')):
            raise SearchError('Expected owned automation tab')
        tab = data['id']
        self.tabs.add(tab)
        self._command('wait', id=tab, seconds=max(.1, self.timeout - .2))
        self._page(tab)
        return tab

    def _budget(self):
        if self.remaining <= 0:
            raise SearchError('Action budget exhausted')
        self.remaining -= 1

    def observe(self, tab, actions):
        """Actions: id, op click/type/submit, CSS selector, optional text.

        Never send text values or raw page text to Jev. Only synthetic fixtures.
        No secret entry, credentials, arbitrary JS, iframe or shadow-root targets.
        """
        self._page(tab)
        if not actions or len(actions) > 30:
            raise SearchError('Need 1..30 prevalidated actions')
        ids = set()
        for a in actions:
            if a.get('op') not in ('click', 'type', 'submit') or not re.fullmatch('[a-zA-Z0-9_-]{1,60}', a.get('id', '')):
                raise SearchError('Invalid prevalidated action')
            if a['id'] in ids or a['id'] in ('reobserve', 'abstain') or not a.get('selector'):
                raise SearchError('Duplicate/reserved action ID or missing selector')
            if len(a.get('text', '')) > 4096:
                raise SearchError('Value too long')
            ids.add(a['id'])
        token = uuid.uuid4().hex
        # Retain actual element identity in Search's isolated JS world. Compare
        # identity, markup, value and URL again in the SAME JS turn as mutation.
        code = '''(() => {
          if (document.querySelector('input[type=password],input[autocomplete^="cc-"],input[autocomplete="one-time-code"]')) throw Error('sensitive page');
          const spec=SPEC, rows=[];
          for(const a of spec) {
            const es=document.querySelectorAll(a.selector);
            if(es.length!==1) throw Error('target must be unique');
            const e=es[0], r=e.getBoundingClientRect();
            if(e.disabled || !r.width || !r.height || getComputedStyle(e).visibility==='hidden') throw Error('target unavailable');
            if(a.op==='type' && !(e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement)) throw Error('not text input');
            if(a.op==='type' && e instanceof HTMLInputElement && !['text','search','email','url','tel'].includes(e.type)) throw Error('unsupported input');
            if(a.op==='submit' && !(e instanceof HTMLFormElement)) throw Error('not form');
            rows.push({a,e,html:e.outerHTML,value:e.value});
          }
          globalThis.__jevTicket={token:TOKEN,url:location.href,rows};
          return JSON.stringify({url:location.href,count:rows.length});
        })()'''.replace('TOKEN', json.dumps(token)).replace('SPEC', json.dumps(actions))
        info = json.loads(self._js(tab, code))
        self._allowed(info['url'])
        self.tickets[tab] = {'token': token, 'actions': {a['id']: dict(a) for a in actions}, 'url': info['url']}
        return {'observation_id': token, 'regions': [{'id': a['id'], 'role': 'textbox' if a['op']=='type' else ('form' if a['op']=='submit' else 'button'), 'label': a.get('description', a['id'])[:300], 'interactive': True} for a in actions], 'candidates': [{'id': a['id'], 'description': a.get('description', a['id'])} for a in actions] + [{'id': 'reobserve', 'description': 'Observe again'}, {'id': 'abstain', 'description': 'Stop'}]}

    def choose(self, observation, goal, *, chooser=None):
        if chooser is None:
            from .choose import choose as chooser
        return chooser({'schema': 'jev.action_choice_request_v1', 'goal': goal,
                        'regions': [], **observation}, timeout=self.timeout)

    def act(self, tab, observation_id, selected_id, *, expect):
        """Consume once; return verified only for the independently observed effect.

        expect = {selector, property: text|value, equals: str} OR {url: str}.
        No blind retry after a failed/timed out effect. Reobserve to act again.
        """
        self._owned(tab)
        ticket = self.tickets.get(tab)
        if not ticket or ticket['token'] != observation_id or selected_id not in ticket['actions']:
            raise StaleAction('Unknown, stale, or already consumed action ticket')
        if not isinstance(expect, dict) or not ((set(expect) == {'url'}) or (set(expect) == {'selector', 'property', 'equals'} and expect['property'] in ('text', 'value'))):
            raise SearchError('Explicit exact effect predicate required')
        if 'url' in expect:
            self._allowed(expect['url'])
        before = self._effect(tab, expect)
        self._budget()
        del self.tickets[tab]
        code = '''(() => {
          const t=globalThis.__jevTicket; globalThis.__jevTicket=null;
          if(!t || t.token!==TOKEN || location.href!==t.url) return 'stale';
          const row=t.rows.find(x=>x.a.id===ID); if(!row) return 'stale';
          const {a,e,html,value}=row, matches=document.querySelectorAll(a.selector);
          const r=e.getBoundingClientRect();
          if(matches.length!==1 || matches[0]!==e || !e.isConnected || e.outerHTML!==html || e.value!==value || e.disabled || !r.width || !r.height || getComputedStyle(e).visibility==='hidden') return 'stale';
          if(document.querySelector('input[type=password],input[autocomplete^="cc-"],input[autocomplete="one-time-code"]')) return 'sensitive';
          if(a.op==='click') e.click();
          else if(a.op==='type') { const p=e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(p,'value').set.call(e,a.text||''); e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true})); }
          else e.requestSubmit();
          return 'executed';
        })()'''.replace('TOKEN', json.dumps(observation_id)).replace('ID', json.dumps(selected_id))
        said = self._js(tab, code)
        if said == 'stale':
            raise StaleAction('DOM, URL, or target identity changed; reobserve')
        if said != 'executed':
            raise SearchError('Action refused: ' + str(said))
        end = time.monotonic() + self.timeout
        after = None
        while time.monotonic() < end:
            after = self._effect(tab, expect)
            if after['matches'] and not before['matches']:
                break
            time.sleep(.05)
        return {'observation_id': observation_id, 'selected_id': selected_id, 'executed': True,
                'verified': bool(after and after['matches'] and not before['matches']),
                'before': before, 'after': after, 'retry_safe': False}

    def _effect(self, tab, expect):
        page = self._page(tab)
        if 'url' in expect:
            actual = page['url']
            target = expect['url']
        else:
            code = '''(() => { const es=document.querySelectorAll(SEL); if(es.length!==1)return null; const e=es[0]; return PROP==='value'?e.value:e.textContent; })()'''.replace('SEL', json.dumps(expect['selector'])).replace('PROP', json.dumps(expect['property']))
            actual = self._js(tab, code)
            target = expect['equals']
        return {'matches': actual == target, 'actual': actual, 'url': page['url']}

    def screenshot(self, tab, directory):
        self._page(tab)
        out = Path(directory).resolve(strict=True) / ('search-' + uuid.uuid4().hex + '.png')
        # Offscreen painting is upstream's native probe-only surface. No taking
        # over the user's foreground window and no patched Search source.
        self._command('pages', on=True)
        self._command('select', id=tab)
        self._command('picture', path=str(out), settle=.5)
        data = self._command('shot', id=tab, path=str(out))
        if not out.is_file() or out.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
            raise SearchError('Screenshot missing/invalid')
        return data

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                self._command('quit')
                self.process.wait(timeout=5)
            except (SearchError, subprocess.TimeoutExpired, OSError):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        owned = [self.support]
        library = Path.home() / 'Library'
        owned += [library / name / self.bundle for name in ('WebKit', 'Caches', 'HTTPStorages')]
        owned += [library / 'Saved Application State' / (self.bundle + '.savedState')]
        if self.runtime:
            owned.append(self.runtime)
        # These exact random names were allocated by this object, never globbed.
        for path in owned:
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        if self.started:
            subprocess.run(['defaults', 'delete', self.suite], capture_output=True, timeout=5)
        self.tabs.clear()
        self.tickets.clear()
        self.cleanup_receipt = {'pid_exited': self.process is None or self.process.poll() is not None,
                                'owned_paths_absent': all(not p.exists() for p in owned),
                                'socket_absent': not self.sock.exists(), 'world': self.world}
