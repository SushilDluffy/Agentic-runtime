import copy

import httpx
import pytest

from agentic_runtime import (
    ActionDecision,
    FinishDecision,
    Identity,
    PermissionDenied,
    Runtime,
    ScriptedProvider,
    SQLiteStore,
)
from examples.github_triage.app import Access, triage_agent
from examples.github_triage.domain import GitHub, SnapshotTooLarge
from examples.github_triage.fixtures import FakeGitHub, scripted

IDENTITY = Identity(user_id="developer", workspace_id="example/triage")


async def setup(tmp_path, http, *, apply=False, numbers=(101, 102, 103), permission=lambda: True):
    github = GitHub("example/triage", http)
    snapshot = await github.snapshot(numbers)
    provider = scripted(snapshot, apply=apply)
    access = Access(IDENTITY, github, numbers, allow_writes=apply, permission=permission)
    agent = triage_agent(access, provider)
    runtime = Runtime(SQLiteStore(tmp_path / "tasks.sqlite"), [agent])
    task = runtime.submit("github_triage", "Triage selected issues", IDENTITY)
    return runtime, task, agent, snapshot


async def approve(runtime, task):
    await runtime.respond(
        task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
    )
    return await runtime.run(task.id)


async def test_read_only_triage_and_untrusted_issue_content(tmp_path):
    fake = FakeGitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        runtime, task, agent, snapshot = await setup(tmp_path, http)
        task = await runtime.run(task.id)
    assert task.status == "succeeded"
    assert fake.writes == []
    assert [a.name for a in agent.actions] == ["read_issues"]
    assert task.proposal["items"][1]["missing_information"]
    assert all(method == "GET" for method, path in fake.requests)
    # Scripted behavior tests capability limits; it is not a model injection-defense evaluation.


async def test_exact_approval_and_restart_before_label_write(tmp_path):
    fake = FakeGitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        runtime, task, agent, snapshot = await setup(tmp_path, http, apply=True, numbers=(101,))
        task = await runtime.run(task.id)
        assert task.pending.kind == "approval" and fake.writes == []
        restarted = Runtime(SQLiteStore(tmp_path / "tasks.sqlite"), [agent])
        done = await approve(restarted, task)
    assert done.status == "succeeded"
    assert fake.writes == [(101, ["bug"])]
    assert {row["name"] for row in fake.issues[101]["labels"]} == {"bug", "needs-review"}


@pytest.mark.parametrize(
    "fault, expected", [("lost_response", "succeeded"), ("unknown", "blocked")]
)
async def test_ambiguous_label_write_is_never_blindly_repeated(tmp_path, fault, expected):
    fake = FakeGitHub(fault=fault)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        runtime, task, agent, snapshot = await setup(tmp_path, http, apply=True, numbers=(101,))
        task = await runtime.run(task.id)
        task = await approve(runtime, task)
    assert task.status == expected
    assert len(fake.writes) == 1
    assert task.operations[-1].state == ("applied" if fault == "lost_response" else "unknown")


async def test_stale_issue_after_approval_refuses_write(tmp_path):
    fake = FakeGitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        runtime, task, agent, snapshot = await setup(tmp_path, http, apply=True, numbers=(101,))
        task = await runtime.run(task.id)
        fake.issues[101]["body"] = "Changed by maintainer while waiting"
        task = await approve(runtime, task)
    assert task.status != "succeeded"
    assert fake.writes == []
    assert task.operations[-1].state == "failed"


async def test_permission_revoked_after_reply_refuses_dispatch(tmp_path):
    fake = FakeGitHub()
    permitted = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        runtime, task, agent, snapshot = await setup(
            tmp_path, http, apply=True, numbers=(101,), permission=lambda: permitted
        )
        task = await runtime.run(task.id)
        await runtime.respond(
            task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
        )
        permitted = False
        task = await runtime.run(task.id)
    assert task.status != "succeeded" and fake.writes == []
    assert task.operations[-1].state == "denied"


async def test_reject_and_cancel_make_no_github_writes(tmp_path):
    for decision in ("reject", "cancel"):
        fake = FakeGitHub()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
            runtime, task, agent, snapshot = await setup(tmp_path / decision, http, apply=True)
            task = await runtime.run(task.id)
            if decision == "cancel":
                runtime.cancel(task.id, IDENTITY)
            else:
                await runtime.respond(
                    task.id,
                    task.pending.id,
                    IDENTITY,
                    False,
                    input_digest=task.pending.input_digest,
                )
            task = await runtime.run(task.id)
        assert task.status != "succeeded" and fake.writes == []


async def test_out_of_scope_issue_cannot_be_modified(tmp_path):
    fake = FakeGitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        runtime, task, agent, snapshot = await setup(tmp_path, http, apply=True, numbers=(101,))
        agent.provider = ScriptedProvider(
            [
                ActionDecision(
                    action="add_labels",
                    arguments={
                        "number": 102,
                        "labels": ["bug"],
                        "source_revision": snapshot.issues[0].revision,
                    },
                )
            ]
        )
        task = await runtime.run(task.id)
        task = await approve(runtime, task)
    assert fake.writes == [] and task.operations[0].state == "denied"


@pytest.mark.parametrize("problem", ["missing", "duplicate", "invented_label", "changed"])
async def test_incomplete_or_stale_report_cannot_succeed(tmp_path, problem):
    fake = FakeGitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        runtime, task, agent, snapshot = await setup(tmp_path, http)
        report = copy.deepcopy(agent.provider.steps[-1].output)
        if problem == "missing":
            report["items"].pop()
        elif problem == "duplicate":
            report["items"][1] = copy.deepcopy(report["items"][0])
        elif problem == "invented_label":
            report["items"][0]["suggested_labels"] = ["critical"]
        else:
            fake.issues[101]["title"] = "Different issue content"
        agent.provider = ScriptedProvider([FinishDecision(output=report)])
        task = await runtime.run(task.id)
    assert task.status != "succeeded" and not task.checks[0].passed


async def test_large_valid_report_reaches_application_check(tmp_path):
    fake = FakeGitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        runtime, task, agent, snapshot = await setup(tmp_path, http)
        report = copy.deepcopy(agent.provider.steps[-1].output)
        for item in report["items"]:
            item["summary"] = "A detailed summary. " * 75
        agent.provider = ScriptedProvider([FinishDecision(output=report)])
        task = await runtime.run(task.id)
    assert task.status == "succeeded"
    assert "artifact_id" in task.proposal


async def test_empty_repository_and_workspace_isolation(tmp_path):
    fake = FakeGitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        runtime, task, agent, snapshot = await setup(tmp_path, http, numbers=())
        task = await runtime.run(task.id)
        with pytest.raises(PermissionDenied):
            runtime.result(task.id, Identity(user_id="developer", workspace_id="elsewhere"))
    assert task.status == "succeeded" and task.proposal == {"items": []}


async def test_pagination_never_follows_untrusted_url():
    requested = []

    def handle(request):
        requested.append(str(request.url))
        assert request.url.host == "api.github.com"
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[{"name": "bug"}],
                headers={"Link": '<https://example.invalid/steal-token>; rel="next"'},
            )
        return httpx.Response(200, json=[{"name": "documentation"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        labels = await GitHub("example/triage", http).available_labels()
    assert labels == ["bug", "documentation"] and len(requested) == 2


async def test_incomplete_pagination_fails_instead_of_claiming_complete_snapshot():
    def handle(request):
        return httpx.Response(
            200, json=[], headers={"Link": '<https://api.github.com/next>; rel="next"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(SnapshotTooLarge):
            await GitHub("example/triage", http).available_labels()
