from playtrace.plugins import registry
from playtrace.plugins.registry import SimulationPluginRegistry


def test_bundled_plugin_is_resolved_by_project_without_cli_hard_coding() -> None:
    plugin = registry.for_project("crisis-wardens")

    assert plugin.id == "crisis-wardens-tabletop"


def test_registry_rejects_unknown_project() -> None:
    isolated = SimulationPluginRegistry()

    try:
        isolated.for_project("unknown")
    except KeyError as error:
        assert "unknown" in str(error)
    else:
        raise AssertionError("unknown project should not resolve")


def test_registry_rejects_duplicate_id_and_project_ownership() -> None:
    isolated = SimulationPluginRegistry()

    class First:
        id = "first"
        project_ids = ("game",)

    class DuplicateId:
        id = "first"
        project_ids = ("other",)

    class DuplicateProject:
        id = "second"
        project_ids = ("game",)

    isolated.register(First)
    for plugin_type in (DuplicateId, DuplicateProject):
        try:
            isolated.register(plugin_type)
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate registration should fail")


def test_failed_registration_does_not_partially_mutate_registry() -> None:
    isolated = SimulationPluginRegistry()

    class First:
        id = "first"
        project_ids = ("claimed",)

    class PartiallyConflicting:
        id = "second"
        project_ids = ("would-have-been-added", "claimed")

    isolated.register(First)

    try:
        isolated.register(PartiallyConflicting)
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting registration should fail")

    assert isolated.plugin_ids == ("first",)
    for project_id in ("would-have-been-added", "unknown"):
        try:
            isolated.for_project(project_id)
        except KeyError:
            pass
        else:
            raise AssertionError(f"{project_id!r} should not resolve")

    try:
        isolated.create("second")
    except KeyError:
        pass
    else:
        raise AssertionError("failed plugin type should not be registered")
