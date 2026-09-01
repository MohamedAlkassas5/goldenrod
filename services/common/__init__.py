"""Shared plumbing: the MCP client, the canonical SQL, and the access policy.

`.env` is applied here, at import, and only ever fills gaps — see
services/common/env.py for why this is the one place it can go.
"""

from services.common.env import load_env

load_env()
