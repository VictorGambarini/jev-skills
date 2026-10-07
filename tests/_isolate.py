"""Keep this machine's own decision backend out of the tests.

A person who has run `jev backend add` has a default backend in ~/.config/jev/backends.json,
and every test that expects the built-in provider order would send its fake request there
instead. Importing this pins the built-in providers for the whole run; tests of backends
themselves (test_backends.py) set JEV_BACKEND and JEV_BACKENDS explicitly.
"""
import os

os.environ["JEV_BACKEND"] = "default"
