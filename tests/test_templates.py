from agent_circus.templates import substitute_variables


def test_substitute_variables() -> None:
    assert substitute_variables("foo", variables={}) == "foo"
    assert substitute_variables(
        {"foo": "hello $${WORKSPACE}"}, variables={"WORKSPACE": "bar"}
    ) == {"foo": "hello bar"}
    assert substitute_variables(
        ["foo", "hello $${WORKSPACE} baz"], variables={"WORKSPACE": "bar"}
    ) == ["foo", "hello bar baz"]
    assert substitute_variables(
        ["foo", "hello $$WORKSPACE baz"], variables={"WORKSPACE": "bar"}
    ) == ["foo", "hello bar baz"]
    assert substitute_variables(
        ["foo", "hello $$WORKSPACE_TYPE baz"], variables={"WORKSPACE": "bar"}
    ) == ["foo", "hello  baz"]
    assert substitute_variables(
        ["foo", "hello $WORKSPACE baz"], variables={"WORKSPACE": "bar"}
    ) == ["foo", "hello $WORKSPACE baz"]
    assert substitute_variables(
        ["foo", "hello ${WORKSPACE} baz"], variables={"WORKSPACE": "bar"}
    ) == ["foo", "hello ${WORKSPACE} baz"]
