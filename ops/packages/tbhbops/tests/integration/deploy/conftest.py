"""Shared deployment integration fixtures."""

from ...deploy_receiver import receiver, retry_receiver
from ...test_deploy_config import deploy_service
from ...test_deploy_filesystem import trusted_tree

__all__ = ["deploy_service", "receiver", "retry_receiver", "trusted_tree"]
