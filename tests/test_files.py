import json
import threading

from sukima.files import read_json, write_json


def test_reader_never_sees_a_partial_file(tmp_path):
    """One thread rewrites a large file over and over; another reads it throughout."""
    path = tmp_path / "batch.json"
    payloads = [{"n": n, "cards": ["x" * 200] * 2000} for n in range(2)]
    write_json(path, payloads[0])
    stop = threading.Event()
    failures, reads = [], [0]

    def reader():
        while not stop.is_set():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                assert data in payloads
                reads[0] += 1
            except Exception as e:  # a partial or missing file lands here
                failures.append(repr(e))

    t = threading.Thread(target=reader)
    t.start()
    for i in range(300):
        write_json(path, payloads[i % 2])
    stop.set()
    t.join()

    assert not failures
    assert reads[0] > 20


def test_only_the_finished_file_is_left(tmp_path):
    write_json(tmp_path / "status.json", {"a": 1})
    write_json(tmp_path / "status.json", {"a": 2})
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]
    assert read_json(tmp_path / "status.json") == {"a": 2}


def test_failed_write_leaves_the_old_file_and_no_temporary(tmp_path):
    path = tmp_path / "status.json"
    write_json(path, {"a": 1})
    try:
        write_json(path, {"a": object()})
    except TypeError:
        pass
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]
    assert read_json(path) == {"a": 1}


def test_missing_file_reads_as_none(tmp_path):
    assert read_json(tmp_path / "nothing.json") is None
