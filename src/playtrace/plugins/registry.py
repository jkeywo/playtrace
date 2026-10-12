"""Registration and entry-point discovery for simulation plugins."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playtrace.simulation import SimulationPlugin


class SimulationPluginRegistry:
    def __init__(self) -> None:
        self._types: dict[str, type[SimulationPlugin]] = {}
        self._projects: dict[str, str] = {}

    def register(self, plugin_type: type[SimulationPlugin]) -> type[SimulationPlugin]:
        plugin_id = plugin_type.id
        if plugin_id in self._types and self._types[plugin_id] is not plugin_type:
            raise ValueError(f"simulation plugin {plugin_id!r} is already registered")
        project_ids = tuple(getattr(plugin_type, "project_ids", ()))
        for project_id in project_ids:
            existing = self._projects.get(project_id)
            if existing and existing != plugin_id:
                raise ValueError(f"project {project_id!r} already uses plugin {existing!r}")

        # Registration is atomic: no visible index changes until every plugin ID
        # and project ownership constraint has passed validation.
        self._types[plugin_id] = plugin_type
        for project_id in project_ids:
            self._projects[project_id] = plugin_id
        return plugin_type

    def discover(self) -> None:
        for item in entry_points(group="playtrace.simulation_plugins"):
            self.register(item.load())

    def create(self, plugin_id: str) -> SimulationPlugin:
        try:
            return self._types[plugin_id]()
        except KeyError as error:
            raise KeyError(f"unknown simulation plugin: {plugin_id}") from error

    def for_project(self, project_id: str) -> SimulationPlugin:
        try:
            return self.create(self._projects[project_id])
        except KeyError as error:
            raise KeyError(f"no simulation plugin is registered for project {project_id!r}") from error

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._types))


registry = SimulationPluginRegistry()


def register_simulation_plugin(
    plugin_type: type[SimulationPlugin],
) -> type[SimulationPlugin]:
    return registry.register(plugin_type)
