"""Execute the release workflow's real CI gate without network or wall-clock waits."""

import io
import json
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SHA = "a" * 40
WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"


def gate_source():
    workflow = WORKFLOW.read_text()
    step = workflow.split(
        "      - name: Require the complete CI matrix for this exact commit\n", 1
    )[1]
    script = step.split("          python - <<'PY'\n", 1)[1].split("          PY\n", 1)[
        0
    ]
    return compile(textwrap.dedent(script), str(WORKFLOW) + ":CI-gate", "exec")


def run_record(**changes):
    return {
        "id": 100,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
    } | changes


class Gate:
    def __init__(self, monkeypatch, pages):
        self.monkeypatch, self.pages = monkeypatch, pages
        self.requests, self.sleeps, self.now = [], [], 0

    def open(self, request, *, timeout):
        self.requests.append(request)
        assert timeout == 30
        assert request.get_method() == "GET"
        assert request.full_url == (
            "https://api.github.test/repos/example/workspool/actions/runs"
            f"?head_sha={SHA}&per_page=100"
        )
        assert request.get_header("Authorization") == "Bearer fixture-token"
        page = self.pages[min(len(self.requests) - 1, len(self.pages) - 1)]
        if isinstance(page, Exception):
            raise page
        return io.BytesIO(json.dumps({"workflow_runs": page}).encode())

    def sleep(self, seconds):
        assert seconds == 15
        self.sleeps.append(seconds)
        self.now += seconds

    def execute(self):
        with self.monkeypatch.context() as patch:
            for name, value in {
                "GITHUB_SHA": SHA,
                "GITHUB_API_URL": "https://api.github.test",
                "GITHUB_REPOSITORY": "example/workspool",
                "GH_TOKEN": "fixture-token",
            }.items():
                patch.setenv(name, value)
            patch.setattr(urllib.request, "urlopen", self.open)
            patch.setattr(time, "monotonic", lambda: self.now)
            patch.setattr(time, "sleep", self.sleep)
            exec(gate_source(), {"__name__": "__main__"})  # noqa: S102 - test the actual checked-in gate


def test_matching_complete_ci_authorizes_exact_commit(monkeypatch, capsys):
    gate = Gate(monkeypatch, [[run_record()]])
    gate.execute()
    assert len(gate.requests) == 1 and gate.sleeps == []
    assert "Complete CI matrix passed for " + SHA in capsys.readouterr().out


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", None])
def test_completed_unsuccessful_ci_refuses_immediately(monkeypatch, conclusion):
    gate = Gate(monkeypatch, [[run_record(conclusion=conclusion)]])
    with pytest.raises(SystemExit, match="Refusing publication: CI concluded"):
        gate.execute()
    assert len(gate.requests) == 1 and gate.sleeps == []


@pytest.mark.parametrize(
    "changes",
    [
        {"path": ".github/workflows/release.yml"},
        {"head_sha": "b" * 40},
        {"event": "pull_request"},
        {"head_branch": "feature"},
    ],
)
def test_unrelated_success_cannot_authorize_publication(monkeypatch, changes):
    gate = Gate(monkeypatch, [[run_record(**changes)]])
    with pytest.raises(SystemExit, match="Timed out waiting for the exact commit"):
        gate.execute()
    assert len(gate.requests) == 60 and gate.now == 900


def test_pending_ci_waits_for_eventual_success(monkeypatch):
    gate = Gate(
        monkeypatch,
        [
            [run_record(status="queued", conclusion=None)],
            [run_record(status="in_progress", conclusion=None)],
            [run_record()],
        ],
    )
    gate.execute()
    assert len(gate.requests) == 3 and gate.sleeps == [15, 15]


@pytest.mark.parametrize(
    "page", [[], [run_record(status="in_progress", conclusion=None)]]
)
def test_absent_or_permanently_pending_ci_times_out(monkeypatch, page):
    gate = Gate(monkeypatch, [page])
    with pytest.raises(SystemExit, match="no release was published"):
        gate.execute()
    assert len(gate.requests) == len(gate.sleeps) == 60
    assert gate.now == 900


def test_newest_matching_failure_overrides_older_success(monkeypatch):
    gate = Gate(monkeypatch, [[run_record(), run_record(id=101, conclusion="failure")]])
    with pytest.raises(SystemExit, match="CI concluded failure"):
        gate.execute()
    assert gate.sleeps == []


def test_older_success_cannot_bypass_newer_pending_run(monkeypatch):
    gate = Gate(
        monkeypatch,
        [
            [run_record(), run_record(id=101, status="in_progress", conclusion=None)],
            [run_record(), run_record(id=101)],
        ],
    )
    gate.execute()
    assert len(gate.requests) == 2 and gate.sleeps == [15]


def test_unrelated_newer_failure_does_not_override_matching_success(monkeypatch):
    gate = Gate(
        monkeypatch,
        [[run_record(), run_record(id=101, head_sha="b" * 40, conclusion="failure")]],
    )
    gate.execute()
    assert gate.sleeps == []


def test_api_failure_fails_closed(monkeypatch):
    error = urllib.error.HTTPError(
        "https://api.github.test", 403, "Forbidden", {}, None
    )
    gate = Gate(monkeypatch, [error])
    with pytest.raises(urllib.error.HTTPError):
        gate.execute()
    assert len(gate.requests) == 1
