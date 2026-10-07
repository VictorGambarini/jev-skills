"""Keep this machine's own decision backend and stored keys out of the tests.

A person who has run `jev backend add` has a default backend in ~/.config/jev/backends.json,
and every test that expects the built-in provider order would send its fake request there
instead. Importing this pins the built-in providers for the whole run; tests of backends
themselves (test_backends.py) set JEV_BACKEND and JEV_BACKENDS explicitly.

A stored provider key does the same thing one level down: with an OpenRouter key in
~/.config/jev/credentials-openrouter, a test written for a machine with no key at all sent
its fake TypeSafe request down the OpenRouter path. So the run also gets an empty config
folder of its own, and the provider keys a shell may carry are dropped; a test that needs a
key sets one itself.
"""
import os
import tempfile

os.environ["JEV_BACKEND"] = "default"
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="jev-tests-config-")
for name in ("OPENROUTER_API_KEY", "VENICE_API_KEY", "OPENCODE_ZEN_API_KEY", "JEV_PROVIDER"):
    os.environ.pop(name, None)
