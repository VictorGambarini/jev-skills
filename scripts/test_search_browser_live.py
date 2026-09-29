#!/usr/bin/env python3
"""Explicit opt-in real Search test. Run inside your resource lifecycle lease.

python3 scripts/test_search_browser_live.py --binary /path/to/Search --sha256 HASH --output /path/to/proof.json
Optional --live-jev asks the configured Jev selector on synthetic fixture labels.
Without it, action selection is deterministic test data, NOT a live Jev claim.
"""
import argparse
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevkit.search_browser import SearchBrowser, SearchError, StaleAction

HTML = b'''<!doctype html><title>Synthetic form</title>
<form id="form" action="/result"><input id="name" name="name"><button>Submit</button></form>
<button id="change" onclick="document.querySelector('#output').textContent='changed'">Change</button>
<button id="later" onclick="setTimeout(()=>document.querySelector('#output').textContent='later',100)">Later</button>
<p id="output">initial</p>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<h1 id="result">Hello fixture</h1>' if self.path.startswith('/result') else HTML
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary', required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--live-jev', action='store_true')
    args = p.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:' + str(server.server_port)
    browser = SearchBrowser(args.binary, sha256=args.sha256, allowed_origins=[url], consent=True, timeout=4)
    report = {'live_jev': args.live_jev, 'receipts': [], 'build_sha256': args.sha256}

    def choose(ob, goal, action_id):
        if args.live_jev:
            result = browser.choose(ob, goal)
            report.setdefault('choices', []).append(result)
            assert result['selected_id'] == action_id, result
        return action_id

    def act(tab, action, expect, goal):
        action['description'] = goal
        ob = browser.observe(tab, [action])
        selected = choose(ob, goal, action['id'])
        receipt = browser.act(tab, ob['observation_id'], selected, expect=expect)
        report['receipts'].append(receipt)
        assert receipt['verified'], receipt
        return ob

    try:
        with browser:
            tab = browser.open(url + '/form')
            ob = act(tab, {'id':'type-name','op':'type','selector':'#name','text':'fixture'},
                     {'selector':'#name','property':'value','equals':'fixture'}, 'Type fixture name in the observed local form textbox')
            try:
                browser.act(tab, ob['observation_id'], 'type-name', expect={'url':url})
                raise AssertionError('ticket replay accepted')
            except StaleAction:
                report['replay_rejected'] = True
            ob = browser.observe(tab, [{'id':'change','op':'click','selector':'#change'}])
            browser._js(tab, "document.querySelector('#change').outerHTML=document.querySelector('#change').outerHTML; true")
            try:
                browser.act(tab, ob['observation_id'], 'change', expect={'selector':'#output','property':'text','equals':'changed'})
                raise AssertionError('identical-markup replacement accepted')
            except StaleAction:
                report['replacement_rejected'] = True
            act(tab, {'id':'later','op':'click','selector':'#later'},
                {'selector':'#output','property':'text','equals':'later'}, 'Click the observed Later button to update synthetic output')
            ob = browser.observe(tab, [{'id':'change','op':'click','selector':'#change','description':'Click the observed Change button'}])
            choose(ob, 'Click Change to update the synthetic output', 'change')
            receipt = browser.act(tab, ob['observation_id'], 'change', expect={'selector':'#output','property':'text','equals':'never'})
            assert not receipt['verified']
            report['missing_effect_not_verified'] = receipt
            act(tab, {'id':'submit','op':'submit','selector':'#form'},
                {'url':url+'/result?name=fixture'}, 'Submit the observed synthetic form to navigate to result')
            report['screenshot'] = browser.screenshot(tab, args.output.parent)
            try:
                browser.open('https://example.com')
                raise AssertionError('public origin accepted')
            except SearchError:
                report['public_origin_rejected'] = True
            try:
                browser.observe('unowned', [{'id':'x','op':'click','selector':'button'}])
                raise AssertionError('unowned tab accepted')
            except SearchError:
                report['unowned_tab_rejected'] = True
            browser._js(tab, "document.body.innerHTML='<input type=password>'; true")
            try:
                browser.observe(tab, [{'id':'x','op':'type','selector':'input','text':'synthetic'}])
                raise AssertionError('sensitive page accepted')
            except SearchError:
                report['sensitive_page_rejected'] = True
            browser.timeout = .05
            try:
                browser._js(tab, '(() => { const until=Date.now()+500; while(Date.now()<until){}; return true; })()')
                raise AssertionError('deadline not enforced')
            except SearchError:
                assert browser.poisoned
                report['real_transport_timeout_poisoned'] = True
            report['passed'] = True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        report['cleanup'] = browser.cleanup_receipt
        args.output.write_text(json.dumps(report, indent=2))
    assert report['cleanup'] is not None
    assert all(report['cleanup'][key] for key in ('pid_exited','owned_paths_absent','socket_absent'))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
