import pytest

from agent_gate import cost
from agent_gate import manifest as m

MANIFEST = m.loads("name: agent\nmodel: claude-sonnet-5\n")
STRICT = m.loads("name: agent\nmodel: claude-sonnet-5\nthresholds:\n  cost_increase_pct: 0.05\n")


def branch(*totals, iterations=1):
    return cost.BranchCost(
        [
            cost.TaskMeasurement(
                task_id=f"t{i}", input_tokens=t, output_tokens=0, iterations=iterations
            )
            for i, t in enumerate(totals)
        ]
    )


class TestVerdict:
    def test_flat_cost_passes(self):
        check = cost.build_check(branch(1000, 1000), branch(1000, 1000), MANIFEST)
        assert not check.blocking
        assert check.status == "pass"
        assert "no material change" in check.headline

    def test_increase_past_threshold_blocks(self):
        check = cost.build_check(branch(1000, 1000), branch(1400, 1400), MANIFEST)
        assert check.blocking
        assert check.headline == "+40% avg tokens/request"
        assert check.reason == "cost spike"

    def test_increase_under_threshold_does_not_block(self):
        check = cost.build_check(branch(1000), branch(1100), MANIFEST)
        assert not check.blocking
        assert "+10%" in check.headline

    def test_threshold_is_configurable(self):
        check = cost.build_check(branch(1000), branch(1100), STRICT)
        assert check.blocking

    def test_a_decrease_passes(self):
        check = cost.build_check(branch(1000), branch(600), MANIFEST)
        assert not check.blocking
        assert "-40%" in check.headline

    def test_per_task_movers_are_surfaced(self):
        base = cost.BranchCost([cost.TaskMeasurement("noisy", 1000), cost.TaskMeasurement("calm", 1000)])
        head = cost.BranchCost([cost.TaskMeasurement("noisy", 3000), cost.TaskMeasurement("calm", 1000)])
        check = cost.build_check(base, head, MANIFEST)
        assert any("`noisy`" in d and "+200%" in d for d in check.details)
        assert not any("`calm`" in d for d in check.details)

    def test_extra_loop_iterations_are_called_out(self):
        check = cost.build_check(
            branch(1000, iterations=1), branch(1600, iterations=3), MANIFEST
        )
        assert any("loop iterations rose" in d for d in check.details)

    def test_errors_skip_rather_than_block(self):
        head = cost.BranchCost(
            [cost.TaskMeasurement("a", errored=True, reason="timed out")]
        )
        check = cost.build_check(branch(1000), head, MANIFEST)
        assert check.status == "skip"
        assert not check.blocking
        assert any("timed out" in d for d in check.details)

    def test_dollar_estimate_appears_for_known_models(self):
        check = cost.build_check(branch(1000), branch(1000), MANIFEST)
        assert any("per request at list price" in d for d in check.details)

    def test_unknown_model_falls_back_to_tokens_only(self):
        unknown = m.loads("name: a\nmodel: some-other-model\n")
        check = cost.build_check(branch(1000), branch(1000), unknown)
        assert any("no price table entry" in d for d in check.details)


class TestMeasurement:
    def test_measures_tokens_from_the_agent(self, fake_agent):
        runner = fake_agent({"tokens": 800, "output_tokens": 200})
        result = cost.run_branch([cost.Task("t1", "hello")], runner, concurrency=1)
        assert result.avg_tokens == 1000
        assert result.avg_input == 800

    def test_zero_usage_is_treated_as_an_error(self, fake_agent):
        runner = fake_agent({"tokens": 0, "output_tokens": 0})
        result = cost.run_branch([cost.Task("t1", "hello")], runner, concurrency=1)
        assert result.errors
        assert "zero token usage" in result.errors[0].reason

    def test_repeats_are_averaged(self, fake_agent):
        runner = fake_agent({"tokens": 500, "output_tokens": 0})
        result = cost.run_branch([cost.Task("t1", "hello")], runner, repeats=3, concurrency=1)
        assert len(result.measurements) == 3
        assert result.avg_tokens == 500


class TestTaskLoading:
    def test_builtin_tasks_load(self):
        tasks = cost.load_tasks(None)
        assert len(tasks) == 5
        assert all(t.id and t.prompt for t in tasks)

    def test_duplicate_ids_rejected(self, tmp_path):
        path = tmp_path / "t.yaml"
        path.write_text("tasks:\n  - {id: a, prompt: p}\n  - {id: a, prompt: q}\n")
        with pytest.raises(cost.TaskError, match="duplicate"):
            cost.load_tasks(path)

    def test_price_lookup_tolerates_dated_aliases(self):
        assert cost._price_for("claude-sonnet-5-20260101") == (3.0, 15.0)
        assert cost._price_for("not-a-model") is None
