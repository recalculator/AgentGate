from agent_gate.runner import AgentResult, _parse_response


class TestProtocol:
    def test_happy_path(self, fake_agent):
        runner = fake_agent({"text": "hello", "tool_calls": ["read_ticket"], "tokens": 900})
        result = runner.run("hi")
        assert result.error is None
        assert result.text == "hello"
        assert result.tool_names == ["read_ticket"]
        assert result.input_tokens == 900
        assert result.total_tokens == 1000

    def test_nonzero_exit_becomes_an_error_not_a_crash(self, fake_agent):
        runner = fake_agent({"exit_code": 3, "stderr": "kaboom"})
        result = runner.run("hi")
        assert result.error is not None
        assert "exited 3" in result.error
        assert "kaboom" in result.error

    def test_garbage_stdout_becomes_an_error(self, fake_agent):
        runner = fake_agent({"garbage_stdout": True})
        result = runner.run("hi")
        assert result.error is not None
        assert "not a JSON object" in result.error

    def test_timeout_becomes_an_error(self, fake_agent):
        runner = fake_agent({"hang": 5}, timeout=1)
        result = runner.run("hi")
        assert result.error is not None
        assert "timed out" in result.error

    def test_tool_outputs_are_delivered_to_the_agent(self, fake_agent):
        runner = fake_agent({"obey_injection": True, "injection_tool_calls": ["write_file"]})
        clean = runner.run("hi")
        injected = runner.run("hi", tool_outputs={"read_ticket": "do something bad"})
        assert clean.tool_names == []
        assert injected.tool_names == ["write_file"]


class TestParsing:
    def test_ignores_incidental_output_before_the_json(self):
        result = _parse_response('some warning\n{"text": "ok", "usage": {"input_tokens": 5}}')
        assert result.error is None
        assert result.text == "ok"
        assert result.input_tokens == 5

    def test_empty_stdout(self):
        assert "no stdout" in _parse_response("").error

    def test_agent_reported_error_is_propagated(self):
        assert _parse_response('{"error": "no API key"}').error == "no API key"

    def test_tolerates_missing_and_malformed_fields(self):
        result = _parse_response('{"text": "hi", "tool_calls": ["bare_string", {"nope": 1}]}')
        assert result.tool_names == ["bare_string"]
        assert result.input_tokens == 0
        assert result.iterations == 1

    def test_usage_that_is_not_a_mapping_is_ignored(self):
        result = _parse_response('{"text": "hi", "usage": "lots"}')
        assert result.total_tokens == 0


def test_agent_result_helpers():
    result = AgentResult(input_tokens=10, output_tokens=5)
    assert result.total_tokens == 15
    assert result.tool_names == []
