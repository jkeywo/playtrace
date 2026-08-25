from playtrace.llm import LLMTask, StaticExecutor


def test_static_executor_obeys_provider_neutral_contract() -> None:
    task = LLMTask(
        id="task:1",
        purpose="test",
        instructions="Return a bounded observation.",
        context={"fact": 1},
    )

    result = StaticExecutor("observation").execute(task)

    assert result.task_id == task.id
    assert result.executor == "static"
    assert result.content == "observation"

