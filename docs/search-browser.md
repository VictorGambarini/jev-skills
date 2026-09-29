# Optional Search test-world backend

`jevkit.search_browser.SearchBrowser` is an explicit Python API for native macOS
Search/WebKit Bench. It is **not CDP**, does not replace browser-use or CUA, and
is not a new default route. No tool is registered automatically. Import it only
when a caller deliberately chooses this backend.

## Supported scope and consent

The initial backend supports **trusted synthetic localhost HTTP fixtures only**.
Pass an exact origin allowlist, a SHA-256 digest of your reviewed local executable,
and `consent=True`. The caller must hold a bounded resource-lifecycle lease.
The backend creates an ad-hoc-signed temporary app with a unique bundle ID,
`SEARCH_PROBE` world, defaults suite, scratch directory and WebKit store. It
never attaches to an installed Search, reads cookies, switches the default
browser, or enables personal browsing automation. The only consent toggles set
are `bench` and `welcomed` in that newly allocated disposable suite.

An origin allowlist is an action/check boundary, **not a network firewall**.
Page scripts, redirects and subresources could contact other hosts before an
observation detects a changed location. Do not use untrusted local proxies or
external content. Public browsing needs a separate network-isolation design and
review; do not loosen `origin()` to pretend that requirement is met.

## Pin and build

Reviewed upstream: https://github.com/driceroland/Search at
`1ec28a0d7cae049adc637c9013fd80bce06fc053`.
Build from that checkout with `swift build -c debug`, then calculate its own
SHA-256 digest. Builds can differ by toolchain, so the API requires your explicit
local digest rather than claiming a portable binary checksum. A different source
revision must be reviewed again. No upstream source patch is necessary.

Search is MIT, Copyright (c) 2026 Office Commun. Upstream also reserves its
Search name and icon for distributed forks. This repository distributes only
our protocol adapter, not a browser fork, binary or branding assets. The local
wrapper is named Automation Lab, is temporary, and is not registered or shipped.

## Action loop

1. `with SearchBrowser(binary, sha256=..., allowed_origins=[url], consent=True) as browser:`
2. `tab = browser.open(url)` creates an owned automation tab.
3. `ob = browser.observe(tab, actions)` validates a small list of caller-authored
   `id`, `op` (`click`, `type`, `submit`), CSS `selector`, optional `text`, and a
   concise synthetic `description`. Do not put secrets in descriptions or goals.
4. `choice = browser.choose(ob, goal)` sends only IDs and descriptions of checked
   targets to Jev. Typed values/selectors/raw page text stay local. `reobserve`
   and `abstain` never execute a mutation. Respect either result.
5. `browser.act(tab, ob['observation_id'], choice['selected_id'], expect=...)`
   consumes a one-shot ticket and verifies an exact effect against the live DOM.
   `expect` is either `{'url': exact_url}` or
   `{'selector': '#output', 'property': 'text', 'equals': 'changed'}` (also `value`).
   Success requires the expected state to be false before and true afterward.
6. `browser.screenshot(tab, output_directory)` saves a real native PNG; inspect
   it if visual correctness matters. Exit the context even after failure.

An element replaced by identical markup is still stale: identity, outerHTML,
value, visibility, enabled state and URL are checked in the same isolated-world
JavaScript turn as the mutation. The page cannot access that world's ticket.
Tickets are single-use and newer observations replace older ones. This is not a
guarantee that a page's semantic meaning or event handlers never change; it is
appropriate for controlled fixtures, not hostile pages.

Transport is newline JSON over a same-UID, mode-0600 Unix socket. Darwin's
LOCAL_PEERPID must match the exact owned process. Bounded reads and a response
size cap prevent indefinite waits. An ambiguous transport timeout poisons the
whole world: no retry, terminate it and start a new one. A missing effect returns
`verified: false`, never success or an automatic replay.

Password, card autocomplete and one-time-code fields cause action observation
refusal. This is defense in depth, not a general PII detector. Do not use it for
customer, credential, payment, publication, or account workflows. Unsupported:
iframes, shadow roots, uploads, browser chrome, dialogs, native menus, trusted
user gestures and cross-tab transactions. Use the existing browser/CUA fallback
where authorized, explicitly, rather than silently changing backend mid-action.

## Rendering and cleanup

Hidden WebKit tabs can have suspended requestAnimationFrame, producing a blank
canvas even when the DOM exists. Screenshots use upstream probe-only `pages`,
`select`, and `picture` to paint the owned view in its offscreen room, followed
by `shot`. This uses upstream's private WebKit occlusion hook and may break with
a future OS/WebKit update. Nothing is brought to the user's foreground. The
adapter does not patch upstream or claim generic headless support.

Close waits for its owned process, then removes only its newly allocated support
folder, bundle WebKit/cache/HTTP stores, saved state, defaults suite and scratch
wrapper. The caller also verifies its resource lease is gone. Existing personal
browser processes and files are not enumerated or modified.

## Verification

Keyless unit tests: `python3 -m unittest discover -s tests`.

Real macOS integration, **inside your bounded resource lease**:

```
python3 scripts/test_search_browser_live.py --binary /path/to/Search \
  --sha256 YOUR_BUILD_SHA256 --output /path/to/proof.json
```

Add `--live-jev` to exercise the configured Jev service with synthetic labels.
Otherwise selection is deterministic test data, clearly recorded as such. The
suite exercises form input, delayed DOM effects, submission/navigation, real
PNG snapshots, stale identity and replay rejection, missing-effect rejection,
sensitive-page and unowned-tab rejection, a real socket deadline, poison-on-
timeout, and owned cleanup. A separate offline shelf demo was also rendered in
native Search and disposable Chromium; that private artifact is not shipped
with this adapter.

## Narrow root installation

To preserve an existing root installation's other modules and skills:

```
python3 install.py --hermes-root-only --search-browser-only --check
python3 install.py --hermes-root-only --search-browser-only
```

This copies only `search_browser.py` into the existing root Hermes Jev plugin.
It does not edit skills, config, profile links, routing or other agents. Existing
profiles linked to the root share this module but do not activate it. The API
still requires explicit opt-in and disposable-world consent. No gateway restart
is needed for standalone Python calls. Full-install defaults remain unchanged.
