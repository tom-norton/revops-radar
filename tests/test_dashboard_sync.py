#!/usr/bin/env python3
"""Tests for tools/dashboard_sync.py: the board copy and the plan the session runs.

    python tests/test_dashboard_sync.py        (or: python -m pytest tests/)

What matters: the pieces put back together are exactly the bundle, each fits in one
storage document, and the plan writes every piece before it points board/meta at them and
deletes the old copy only after that.
"""

import base64
import gzip
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
import dashboard_sync as ds  # noqa: E402

NOW = datetime(2026, 11, 22, 15, 3, tzinfo=timezone.utc)


def bundle(n=400):
    return {"jobs": [{"id": f"j{i}", "title": "Revenue Operations Manager",
                      "description": os.urandom(600).hex()} for i in range(n)],
            "status": {"last_run": "x"}, "excluded": {"rows": []},
            "contacts": [{"name": "A", "company": "Acme", "chat": "1/2"}], "outreach": None,
            "sheet_error": ""}


def test_pieces_join_back_to_the_bundle_and_fit_a_document():
    b = bundle()
    chunks, raw = ds.pieces(b, size=50_000)
    assert len(chunks) > 1
    assert all(len(json.dumps({"gen": "g", "i": 0, "n": 9, "data": c})) < 256 * 1024 for c in chunks)
    back = json.loads(gzip.decompress(base64.b64decode("".join(chunks))))
    assert back == b and raw > 0


def test_default_piece_fits_the_storage_limit():
    assert ds.PIECE_CHARS + 200 < 256 * 1024
    # A batch call carries at most 1 MiB.
    assert ds.PIECES_PER_BATCH * (ds.PIECE_CHARS + 500) < 1024 * 1024


def test_plan_orders_pieces_then_meta_then_deletes():
    with tempfile.TemporaryDirectory() as work:
        chunks = ["a" * 10] * 9
        old = {"gen": "OLD", "n": 3}
        plan = ds.build_plan(work, "NEW", chunks, 123, NOW, 7, old, ["x~1", "y~2"])
        b = plan["batches"]
        sets = [w for batch in b[:-2] for w in batch]
        assert [w["doc_id"] for w in sets] == [f"NEW-{i:02d}" for i in range(9)]
        assert all(len(batch) <= ds.PIECES_PER_BATCH for batch in b[:-2])
        assert b[-2] == [dict(b[-2][0], op="set", doc_id="meta", if_version=7)]
        assert json.load(open(b[-2][0]["file_path"]))["n"] == 9
        dels = b[-1]
        assert [d["doc_id"] for d in dels] == ["OLD-00", "OLD-01", "OLD-02", "x~1", "y~2"]
        assert all(d["if_version"] == 1 and d["op"] == "delete" for d in dels)
        piece = json.load(open(sets[0]["file_path"]))
        assert piece == {"gen": "NEW", "i": 0, "n": 9, "data": "a" * 10}


def test_first_plan_creates_meta_unpinned_and_deletes_nothing():
    with tempfile.TemporaryDirectory() as work:
        plan = ds.build_plan(work, "NEW", ["a"], 1, NOW, 0, None, [])
        assert plan["batches"][-1] == [{k: v for k, v in plan["batches"][-1][0].items()}]
        assert "if_version" not in plan["batches"][-1][0]
        assert len(plan["batches"]) == 2


class Reply:
    def __init__(self, obj=None, exc=None):
        self.obj, self.exc = obj, exc

    def json(self):
        if self.exc:
            raise self.exc
        return self.obj


def test_outbox_rows_post_with_the_token_and_only_landed_ones_clear():
    sent = []

    def post(url, data=None, headers=None, timeout=None):
        body = json.loads(data)
        sent.append(body)
        return {"Acme": Reply({"ok": True}), "Dup": Reply({"ok": True, "dup": True}),
                "Bad": Reply({"ok": False, "error": "bad token"}),
                "Lost": Reply(exc=ValueError("html"))}[body["company"]]

    outbox = {f"{c}~1": {"row": {"company": c, "title": "RevOps"}} for c in ("Acme", "Dup", "Bad", "Lost")}
    outbox["empty~1"] = {"row": {}}
    done, problems = ds.post_outbox(outbox, "T", post=post)
    assert sorted(done) == ["Acme~1", "Dup~1", "empty~1"]
    assert len(problems) == 2
    assert all(b["token"] == "T" for b in sent) and len(sent) == 4


def test_firebase_gets_the_mark_keys_only():
    got = {}

    class R:
        def raise_for_status(self):
            pass

    def patch(url, data=None, headers=None, timeout=None):
        got.update(json.loads(data))
        return R()

    counts = ds.patch_firebase({"hidden": ["a"], "applied": [], "updated_at": "x",
                                "hide_notes": [{"id": "a"}]}, patch=patch)
    assert set(got) == set(ds.MARK_KEYS) and got["hidden"] == ["a"] and got["hidden_at"] == []
    assert counts["hidden"] == 1


def test_sheet_failure_keeps_lists_unset():
    def get(url, params=None, timeout=None):
        if params["action"] == "contacts":
            return Reply({"ok": True, "contacts": [{"name": "A"}]})
        return Reply({"ok": False, "error": "no tab named Companies"})
    contacts, outreach, err = ds.read_sheet("T", get=get)
    assert contacts == [{"name": "A"}] and outreach is None and "Companies" in err


def _run():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
