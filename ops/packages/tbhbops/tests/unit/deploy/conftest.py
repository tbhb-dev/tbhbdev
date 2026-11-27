"""Shared deployment unit fixtures."""

from ...deploy_fakes import engine
from ...test_deploy_config import deploy_service
from ...test_deploy_filesystem import trusted_tree

__all__ = ["deploy_service", "engine", "trusted_tree"]
