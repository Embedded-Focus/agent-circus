from agent_circus.templates import substitute_variables_str


def test_substitute_variables_str() -> None:
    assert substitute_variables_str("foo", variables={}) == "foo"
    assert (
        substitute_variables_str("hello $${WORKSPACE}", variables={"WORKSPACE": "bar"})
        == "hello bar"
    )
    assert (
        substitute_variables_str(
            "hello $${WORKSPACE} baz", variables={"WORKSPACE": "bar"}
        )
        == "hello bar baz"
    )
    assert (
        substitute_variables_str(
            "hello $$WORKSPACE baz", variables={"WORKSPACE": "bar"}
        )
        == "hello bar baz"
    )
    assert (
        substitute_variables_str(
            "hello $$WORKSPACE_TYPE baz", variables={"WORKSPACE": "bar"}
        )
        == "hello  baz"
    )
    assert (
        substitute_variables_str("hello $WORKSPACE baz", variables={"WORKSPACE": "bar"})
        == "hello $WORKSPACE baz"
    )
    assert (
        substitute_variables_str(
            "hello ${WORKSPACE} baz", variables={"WORKSPACE": "bar"}
        )
        == "hello ${WORKSPACE} baz"
    )
