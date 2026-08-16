from agent_gate import manifest as m
from agent_gate import permissions

BASE = """
name: agent
max_iterations: 8
tools:
  - name: read_file
    scopes: [fs:read]
  - name: lookup_order
    scopes: ["api:orders:read"]
"""


def make(text: str) -> m.Manifest:
    return m.loads(text)


def test_no_change_passes():
    report = permissions.diff(make(BASE), make(BASE))
    assert report.status == "pass"
    assert not report.blocking
    assert report.headline() == "no change"


def test_new_write_scope_blocks():
    head = BASE + """
  - name: write_file
    scopes: [fs:write]
"""
    report = permissions.diff(make(BASE), make(head))
    assert report.blocking
    assert report.added_scopes == ["fs:write"]
    assert "filesystem write access" in report.headline()
    assert report.headline().startswith("+1")


def test_shell_and_network_both_block_and_are_counted():
    head = BASE + """
  - name: run_shell
    scopes: [exec:shell]
  - name: fetch
    scopes: ["net:http"]
"""
    report = permissions.diff(make(BASE), make(head))
    assert report.blocking
    assert set(report.added_scopes) == {"exec:shell", "net:http"}
    assert report.headline().startswith("+2")


def test_new_tool_reusing_existing_scopes_warns_but_does_not_block():
    head = BASE + """
  - name: read_attachment
    scopes: [fs:read]
"""
    report = permissions.diff(make(BASE), make(head))
    assert not report.blocking
    assert report.status == "warn"
    assert report.added_tools == ["read_attachment"]
    assert "no new capabilities" in report.headline()


def test_widening_an_existing_tool_with_an_existing_scope_warns():
    base = """
name: agent
tools:
  - name: reader
    scopes: [fs:read]
  - name: writer
    scopes: [fs:write]
"""
    head = """
name: agent
tools:
  - name: reader
    scopes: [fs:read, fs:write]
  - name: writer
    scopes: [fs:write]
"""
    report = permissions.diff(make(base), make(head))
    assert not report.blocking
    assert any(f.kind == "tool_scope_widened" for f in report.findings)


def test_removing_a_capability_is_informational():
    head = """
name: agent
max_iterations: 8
tools:
  - name: read_file
    scopes: [fs:read]
"""
    report = permissions.diff(make(BASE), make(head))
    assert not report.blocking
    assert report.removed_scopes == ["api:orders:read"]
    assert "narrowed" in report.headline()


def test_new_api_scope_on_same_service_is_an_escalation():
    head = BASE.replace("api:orders:read", "api:orders:write")
    report = permissions.diff(make(BASE), make(head))
    assert report.blocking
    assert report.added_scopes == ["api:orders:write"]


class TestLoopCap:
    def test_unchanged(self):
        r = permissions.check_loop_cap(make(BASE), make(BASE))
        assert r.status == "pass"
        assert r.headline() == "unchanged (8)"

    def test_removed_blocks(self):
        head = BASE.replace("max_iterations: 8\n", "")
        r = permissions.check_loop_cap(make(BASE), make(head))
        assert r.blocking
        assert "removed (was max_iterations: 8)" in r.headline()

    def test_large_increase_blocks(self):
        head = BASE.replace("max_iterations: 8", "max_iterations: 40")
        r = permissions.check_loop_cap(make(BASE), make(head))
        assert r.blocking
        assert "8 → 40" in r.headline()

    def test_small_increase_is_within_threshold(self):
        head = BASE.replace("max_iterations: 8", "max_iterations: 10")
        r = permissions.check_loop_cap(make(BASE), make(head))
        assert not r.blocking
        assert r.status == "warn"

    def test_lowering_is_informational(self):
        head = BASE.replace("max_iterations: 8", "max_iterations: 4")
        r = permissions.check_loop_cap(make(BASE), make(head))
        assert not r.blocking

    def test_threshold_is_configurable(self):
        base = BASE + "thresholds:\n  iteration_increase_pct: 2.0\n"
        head = base.replace("max_iterations: 8", "max_iterations: 20")
        r = permissions.check_loop_cap(make(base), make(head))
        assert not r.blocking
