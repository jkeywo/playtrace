"""Game and subsystem plugins for Play Trace."""

from playtrace.plugins.registry import registry, register_simulation_plugin

# Import bundled plugins for registration. Callers depend only on this registry;
# additional packages can use the playtrace.simulation_plugins entry-point group.
from playtrace.plugins import crisis_wardens as _crisis_wardens  # noqa: F401,E402

registry.discover()

__all__ = ["registry", "register_simulation_plugin"]
