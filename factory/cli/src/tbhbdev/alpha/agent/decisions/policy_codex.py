"""Build Codex permission profiles and network proxy settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import CodexMode, CodexPolicy, NetworkProxy, Profile

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Settings

# Codex's name for the whole filesystem, which a profile makes read-only.
READ_ROOT = ":root"
APPROVAL_POLICY = "never"


def build(
    settings: Settings,
    profile: Profile,
    writes: tuple[str, ...],
    deny: tuple[str, ...],
    *,
    full: bool,
) -> CodexPolicy:
    codex = settings.codex
    search = (
        codex.web_search_build if profile is Profile.BUILD else codex.web_search_review
    )
    proxy = network_proxy(settings)
    if full:
        return CodexPolicy(
            CodexMode.FULL_ACCESS,
            (),
            (),
            deny,
            APPROVAL_POLICY,
            search,
            None,
            codex.subagent_model,
        )
    return CodexPolicy(
        CodexMode.PROFILE,
        (READ_ROOT,),
        writes,
        deny,
        APPROVAL_POLICY,
        search,
        proxy,
        codex.subagent_model,
    )


def network_proxy(settings: Settings) -> NetworkProxy | None:
    if settings.sandbox.codex_network_proxy_opt_out is not None:
        return None
    return NetworkProxy(
        enabled=True,
        domains=dict.fromkeys(settings.sandbox.network_domains, "allow"),
        mode="limited",
        unix_sockets={},
        dangerously_allow_all_unix_sockets=False,
        allow_local_binding=False,
    )
