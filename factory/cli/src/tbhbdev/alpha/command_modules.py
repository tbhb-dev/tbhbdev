"""Static imports that include the alpha command modules in the CLI binary."""

from tbhbdev.alpha import (
    brief,
    close,
    gh_app_command,
    gh_shim,
    pre_push_guard,
    repo_bootstrap,
    review,
    run_stop_hook,
    session_context,
    verify_range,
    version_check,
    worktree_sweep,
)
from tbhbdev.alpha.agent import command as agent
from tbhbdev.alpha.guard import (
    agent_model,
    gh_config_dir,
    ops_no_lazy_stop,
    sendmessage_clock,
    state,
)
from tbhbdev.alpha.guard.gh_poll import command as guard_gh_poll
from tbhbdev.alpha.guard.gh_pr import command as guard_gh_pr
from tbhbdev.alpha.guard.op import command as guard_op
from tbhbdev.alpha.guard.scripts import command as guard_scripts
from tbhbdev.alpha.host import command as host
from tbhbdev.alpha.identity import dispatcher as identity
from tbhbdev.alpha.install import command as install
from tbhbdev.alpha.pull_request import command as pull_request
from tbhbdev.merge import command as merge
from tbhbdev.merge import remove as merge_queue_remove
from tbhbdev.monorepo import command as monorepo

__all__ = [
    "agent",
    "agent_model",
    "brief",
    "close",
    "gh_app_command",
    "gh_config_dir",
    "gh_shim",
    "guard_gh_poll",
    "guard_gh_pr",
    "guard_op",
    "guard_scripts",
    "host",
    "identity",
    "install",
    "merge",
    "merge_queue_remove",
    "monorepo",
    "ops_no_lazy_stop",
    "pre_push_guard",
    "pull_request",
    "repo_bootstrap",
    "review",
    "run_stop_hook",
    "sendmessage_clock",
    "session_context",
    "state",
    "verify_range",
    "version_check",
    "worktree_sweep",
]
