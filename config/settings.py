"""Backward-compat shim — imports from core/config/settings.py"""
from core.config.settings import *  # noqa: F401,F403
from core.config.settings import PROVIDER, MODELS, AZURE_AUTH_MODE, TEMPERATURE, MAX_TOKENS  # noqa: F401
