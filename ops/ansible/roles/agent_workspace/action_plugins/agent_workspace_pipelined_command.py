"""Keep sensitive command arguments out of Ansible's temporary files."""

from typing import override

from ansible.errors import AnsibleActionFail
from ansible.plugins.action.command import ActionModule as CommandAction


class ActionModule(CommandAction):
    """Require pipelining before the command action can stage its arguments."""

    @override
    def _is_pipelining_enabled(
        self,
        module_style: str,
        wrap_async: bool = False,
    ) -> bool:
        if wrap_async or not super()._is_pipelining_enabled(module_style):
            msg = "Writing the reviews App cache requires Ansible pipelining"
            raise AnsibleActionFail(msg)
        return True
