from pluginbench.results import ArmResult, TaskResult, Usage


def test_missing_usage_is_not_replaced_with_zero() -> None:
    arm = ArmResult.from_tasks(
        arm="treatment",
        compatibility_fingerprint="sha256:test",
        tasks={
            "a": TaskResult(
                task_id="a",
                attempts=1,
                score=1,
                passed=True,
                duration_seconds=None,
                infrastructure_errors=[],
                usage=Usage(),
            )
        },
    )

    assert arm.input_tokens is None
    assert arm.cached_tokens is None
    assert arm.output_tokens is None
    assert arm.cost_usd is None
    assert arm.known_cost_usd is None
