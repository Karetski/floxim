"""Run storage: IDs, the event log, derived state, the lock and the inbox."""

import datetime
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from whisperwind.clock import FakeClock, iso
from whisperwind.store import inbox
from whisperwind.store.events import CorruptLog, EventWriter, read_log
from whisperwind.store.ids import AmbiguousRun, RunNotFound, new_run_id, resolve_run
from whisperwind.store.lock import STALE_AFTER_S, LockHeld, RunLock, hostname
from whisperwind.store.rundir import RunDir
from whisperwind.store.state import active_seconds, apply, initial_state, reduce

T0 = datetime.datetime(2026, 9, 30, 14, 15, 3, tzinfo=datetime.timezone.utc)


# -- run IDs -----------------------------------------------------------------


def test_given_flow_and_time_when_run_id_created_then_it_sorts_by_start_time() -> None:
    assert new_run_id("implement-feature", T0, "7k2q") == "20260930T141503-implement-feature-7k2q"
    assert new_run_id("a", T0) < new_run_id("a", T0 + datetime.timedelta(seconds=1))


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    for run_id in [
        "20260930T100000-build-aaaa",
        "20260930T110000-deploy-bbbb",
        "20260930T120000-build-cccc",
    ]:
        (tmp_path / run_id).mkdir()
        (tmp_path / run_id / "events.jsonl").touch()
    return tmp_path


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("@last", "20260930T120000-build-cccc"),
        ("@last:deploy", "20260930T110000-deploy-bbbb"),
        ("20260930T11", "20260930T110000-deploy-bbbb"),
        ("aaaa", "20260930T100000-build-aaaa"),
        ("20260930T120000-build-cccc", "20260930T120000-build-cccc"),
    ],
)
def test_given_runs_when_argument_resolved_then_finds_the_run(
    runs: Path, arg: str, expected: str
) -> None:
    assert resolve_run(runs, arg) == expected


def test_given_runs_when_argument_is_ambiguous_or_unknown_then_says_so(runs: Path) -> None:
    with pytest.raises(AmbiguousRun):
        resolve_run(runs, "20260930T1")
    with pytest.raises(RunNotFound):
        resolve_run(runs, "zzzz")
    with pytest.raises(RunNotFound):
        resolve_run(runs, "@last:nope")


# -- event log -----------------------------------------------------------------


def _writer(path: Path, clock: FakeClock | None = None) -> EventWriter:
    return EventWriter(path, clock or FakeClock(T0))


def test_given_appended_events_when_read_then_they_come_back_in_order(tmp_path: Path) -> None:
    # Given
    log = tmp_path / "events.jsonl"
    writer = _writer(log)
    writer.append("run_created", {"flow": "f"})
    writer.append("visit_started", {"type": "shell"}, node="a", visit=1)
    writer.close()

    # When
    read = read_log(log)

    # Then
    assert [e["seq"] for e in read.events] == [1, 2]
    assert read.events[1] == {
        "v": 1,
        "seq": 2,
        "ts": "2026-09-30T14:15:03.000Z",
        "type": "visit_started",
        "branch": "main",
        "node": "a",
        "visit": 1,
        "data": {"type": "shell"},
    }
    assert not read.torn_tail


def test_given_torn_final_line_when_read_then_it_is_ignored_and_trimmed_on_next_append(
    tmp_path: Path,
) -> None:
    # Given
    log = tmp_path / "events.jsonl"
    writer = _writer(log)
    writer.append("run_created")
    writer.close()
    with open(log, "ab") as handle:
        handle.write(b'{"v":1,"seq":2,"ts":"2026')

    # When
    read = read_log(log)
    writer = _writer(log)
    writer.append("run_started")
    writer.close()

    # Then
    assert read.torn_tail and len(read.events) == 1
    assert writer.torn_tail
    assert [e["type"] for e in read_log(log).events] == ["run_created", "run_started"]


def test_given_last_event_without_newline_when_appending_then_it_is_kept(tmp_path: Path) -> None:
    # Given
    log = tmp_path / "events.jsonl"
    writer = _writer(log)
    writer.append("run_created")
    writer.close()
    log.write_bytes(log.read_bytes().rstrip(b"\n"))

    # When
    writer = _writer(log)
    writer.append("run_started")
    writer.close()

    # Then
    assert [e["seq"] for e in read_log(log).events] == [1, 2]


@pytest.mark.parametrize(
    "corruption",
    [
        lambda lines: [lines[0], b"not json", lines[1]],
        lambda lines: [lines[0], lines[1].replace(b'"seq":2', b'"seq":3')],
        lambda lines: [lines[0], lines[0], lines[1]],
    ],
    ids=["bad-middle-line", "seq-gap", "duplicate-seq"],
)
def test_given_corrupted_log_when_read_then_raises_corrupt_log(
    tmp_path: Path, corruption: Any
) -> None:
    # Given
    log = tmp_path / "events.jsonl"
    writer = _writer(log)
    writer.append("run_created")
    writer.append("run_started")
    writer.close()
    lines = log.read_bytes().splitlines()
    log.write_bytes(b"\n".join(corruption(lines)) + b"\n")

    # When / Then
    with pytest.raises(CorruptLog):
        read_log(log)


# -- derived state -----------------------------------------------------------

STEP = st.sampled_from(
    ["visit_ok", "visit_fail", "route", "budget", "wait", "respond", "detach", "attach", "set"]
)


@settings(max_examples=150, deadline=None)
@given(st.lists(STEP, max_size=40))
def test_given_any_event_sequence_when_state_built_incrementally_then_it_equals_a_replay(
    steps: list[str],
) -> None:
    # Given
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "events.jsonl"
        clock = FakeClock(T0)
        state = initial_state()
        writer = EventWriter(log, clock, on_event=lambda e: apply(state, e))
        writer.append("run_created", {"flow": "f", "inputs": {}})
        writer.append("run_started", {"pid": 1, "host": "h"})
        visits: dict[str, int] = {}

        # When
        for i, step in enumerate(steps):
            clock.advance(1 + i % 3)
            node = "abc"[i % 3]
            if step in ("visit_ok", "visit_fail", "set"):
                visits[node] = visits.get(node, 0) + 1
                v = visits[node]
                writer.append(
                    "visit_started",
                    {"type": "set" if step == "set" else "shell"},
                    node=node,
                    visit=v,
                )
                outcome = "failed" if step == "visit_fail" else "succeeded"
                result = {"values": {"x": i}} if step == "set" else {"exit_code": 0}
                writer.append(
                    "visit_finished", {"outcome": outcome, "result": result}, node=node, visit=v
                )
            elif step == "route":
                writer.append("route_taken", {"from": node, "to": "b", "via": "next"})
            elif step == "budget":
                writer.append("budget_updated", {"usd_spent": i * 0.5, "tokens_spent": i * 10})
            elif step == "wait":
                writer.append("human_waiting", {"message": "ok?"}, node=node, visit=1)
                writer.append("run_waiting", {"nodes": [node]})
            elif step == "respond":
                writer.append("human_responded", {"choice": "yes"}, node=node)
            elif step == "detach":
                writer.append("runner_detached", {"reason": "signal"})
            else:
                writer.append("runner_attached", {"pid": 2, "host": "h"})
        writer.close()

        # Then
        replayed = reduce(read_log(log).events)
        assert json.loads(json.dumps(state)) == json.loads(json.dumps(replayed))
        assert replayed["last_seq"] == len(read_log(log).events)


def test_given_waits_and_crashes_when_active_time_computed_then_they_are_excluded(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(T0)
    writer = _writer(tmp_path / "events.jsonl", clock)
    writer.append("run_created")
    writer.append("run_started", {"pid": 1, "host": "h"})
    clock.advance(10)
    writer.append("human_waiting", {}, node="ask", visit=1)
    writer.append("run_waiting", {"nodes": ["ask"]})
    clock.advance(1000)  # waiting does not count
    writer.append("human_responded", {"choice": "go"}, node="ask")
    clock.advance(5)
    writer.append("visit_finished", {"outcome": "succeeded", "result": {}}, node="ask", visit=1)
    clock.advance(500)  # the runner crashed after its last event; this gap does not count
    writer.append("runner_takeover", {"pid": 2, "host": "h"})
    clock.advance(7)

    # When
    state = reduce(writer.events)

    # Then
    assert active_seconds(state, iso(clock.now())) == 10 + 5 + 7


# -- lock -----------------------------------------------------------------------


def test_given_live_lock_when_another_runner_acquires_then_it_is_refused(tmp_path: Path) -> None:
    # Given
    clock = FakeClock(T0)
    first = RunLock(tmp_path / "lock", clock)
    first.acquire()

    # When / Then
    with pytest.raises(LockHeld):
        RunLock(tmp_path / "lock", clock).acquire()
    first.release()
    RunLock(tmp_path / "lock", clock).acquire()


def test_given_old_heartbeat_when_acquiring_then_the_stale_lock_is_taken_over(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(T0)
    (tmp_path / "lock").write_text(
        json.dumps({"pid": 1, "host": "elsewhere", "started_at": iso(T0), "heartbeat_at": iso(T0)})
    )
    clock.advance(STALE_AFTER_S + 1)

    # When
    lock = RunLock(tmp_path / "lock", clock)
    lock.acquire()

    # Then
    assert lock.took_over is not None and lock.took_over["host"] == "elsewhere"
    assert json.loads((tmp_path / "lock").read_text())["pid"] == os.getpid()


def test_given_dead_pid_on_this_host_when_acquiring_then_the_lock_is_taken_over(
    tmp_path: Path,
) -> None:
    # Given
    process = multiprocessing.get_context("spawn").Process(target=int)
    process.start()
    process.join()
    (tmp_path / "lock").write_text(
        json.dumps(
            {"pid": process.pid, "host": hostname(), "started_at": iso(T0), "heartbeat_at": iso(T0)}
        )
    )

    # When
    lock = RunLock(tmp_path / "lock", FakeClock(T0))
    lock.acquire()

    # Then
    assert lock.took_over is not None


def _race(path: str, queue: Any, done: Any) -> None:
    try:
        RunLock(Path(path), FakeClock(T0 + datetime.timedelta(seconds=STALE_AFTER_S + 5))).acquire()
        queue.put("won")
    except LockHeld:
        queue.put("lost")
    done.wait(30)  # a winner must stay alive, or its lock is stale for the next racer


def test_given_stale_lock_when_processes_race_to_take_it_then_exactly_one_wins(
    tmp_path: Path,
) -> None:
    # Given
    (tmp_path / "lock").write_text(
        json.dumps({"pid": 1, "host": "elsewhere", "started_at": iso(T0), "heartbeat_at": iso(T0)})
    )
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    done = context.Event()
    processes = [
        context.Process(target=_race, args=(str(tmp_path / "lock"), queue, done)) for _ in range(6)
    ]

    # When
    for p in processes:
        p.start()
    results = sorted(queue.get(timeout=30) for _ in processes)
    done.set()
    for p in processes:
        p.join(30)

    # Then
    assert results.count("won") == 1


# -- inbox and run directory ---------------------------------------------------------


def test_given_requests_when_posted_then_they_are_read_in_order_and_consumed(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(T0)
    inbox.post(tmp_path / "inbox", {"type": "cancel"}, clock)
    clock.advance(1)
    inbox.post(tmp_path / "inbox", {"type": "respond", "choice": "go"}, clock)

    # When
    requests = inbox.pending(tmp_path / "inbox")
    inbox.consume(requests[0][0])

    # Then
    assert [r["type"] for _, r in requests] == ["cancel", "respond"]
    assert [r["type"] for _, r in inbox.pending(tmp_path / "inbox")] == ["respond"]


def test_given_flow_with_files_outside_its_dir_when_snapshotted_then_layout_is_kept(
    tmp_path: Path,
) -> None:
    # Given
    (tmp_path / "flows").mkdir()
    (tmp_path / "shared").mkdir()
    flow = tmp_path / "flows" / "f.yaml"
    flow.write_text("x")
    shared = tmp_path / "shared" / "t.yaml"
    shared.write_text("y")
    run = RunDir.create(tmp_path / "runs", "r1", {"id": "r1"})

    # When
    copy = run.write_snapshot([flow, shared], flow)

    # Then
    assert copy == run.snapshot_dir() / "flows" / "f.yaml"
    assert (run.snapshot_dir() / "shared" / "t.yaml").read_text() == "y"
    assert oct(run.path.stat().st_mode & 0o777) == "0o700"


def test_given_stale_state_json_when_state_read_then_it_is_rebuilt_from_events(
    tmp_path: Path,
) -> None:
    # Given
    run = RunDir.create(tmp_path, "r1", {"id": "r1"})
    writer = _writer(run.events)
    writer.append("run_created", {"flow": "f"})
    run.write_state(reduce(writer.events))
    writer.append("run_started", {"pid": 1, "host": "h"})
    writer.close()

    # When
    state = run.read_state()

    # Then
    assert state["status"] == "running"
    assert state["last_seq"] == 2


def test_given_runs_created_in_the_same_second_when_last_resolved_then_the_newest_wins(
    tmp_path: Path,
) -> None:
    # Given: the random suffixes sort the other way round
    for run_id, created in [
        ("20260930T100000-a-zzzz", "10:00:00.100"),
        ("20260930T100000-a-aaaa", "10:00:00.900"),
    ]:
        (tmp_path / run_id).mkdir()
        (tmp_path / run_id / "events.jsonl").touch()
        (tmp_path / run_id / "run.json").write_text(
            json.dumps({"created_at": f"2026-09-30T{created}Z"})
        )

    # When / Then
    assert resolve_run(tmp_path, "@last") == "20260930T100000-a-aaaa"
