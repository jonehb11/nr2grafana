"""Shim so older pips (<21.3) can `pip install -e .`; config lives in
pyproject.toml."""

from setuptools import setup

setup()
