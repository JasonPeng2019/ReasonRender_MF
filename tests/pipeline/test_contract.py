from rrc.contract import Config, NullRetrieval, RunContext, Task


def test_run_context_uses_frozen_query_tag_format() -> None:
    context = RunContext(arm="warm", task_id="0007")

    assert context.tag("implement") == "rrc:arm=warm;task=0007;stage=implement"


def test_null_retrieval_is_a_no_op() -> None:
    retrieval = NullRetrieval()
    task = Task(task_id="0001", text="Implement an identity function.")

    assert retrieval.retrieve(task, Config()) == []
    assert retrieval.get_template("missing") is None
