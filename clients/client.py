"""Backward-compat shim — imports from core/clients/client.py"""
from core.clients.client import *  # noqa: F401,F403
from core.clients.client import get_client, token_limit_kwarg  # noqa: F401
