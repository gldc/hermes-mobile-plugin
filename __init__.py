"""hermes-mobile — hermes-agent plugin entry point.

Install into ``~/.hermes/plugins/hermes-mobile/`` (this repo's root is
the plugin directory: ``plugin.yaml`` + this ``__init__.py``), then
activate with ``hermes plugins enable hermes-mobile`` (user plugins are
opt-in).

hermes' PluginManager imports this file as a package
(``hermes_plugins.<slug>``) and calls ``register(ctx)`` once per host
process (CLI, gateway, dashboard web server).
"""

from __future__ import annotations


def register(ctx, _store=None) -> None:
    """Plugin entry — delegates to :func:`hermes_mobile.plugin.register_all`.

    ``_store`` is a test seam (injectable DeviceStore); hermes always
    calls this with the single positional ``ctx``.
    """
    try:
        from .hermes_mobile.plugin import register_all
    except ImportError:
        # Direct/package-less import contexts (e.g. tests importing the
        # file standalone) fall back to the absolute import.
        from hermes_mobile.plugin import register_all

    register_all(ctx, store=_store)
