from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from cf.classroom import ClassroomError, scan_contest
from cf.web import ReviewHandler

STYLE_A = """\
#include <iostream>
#include <vector>
using namespace std;

int solve(int n) {
    int total = 0;
    for (int i = 0; i < n; i++) {
        if (i % 2 == 0) {
            total += i;
        } else {
            total -= i;
        }
    }
    return total;
}

int main() {
    int n = 10;
    cout << solve(n) << endl;
    return 0;
}
"""

STYLE_B = """\
#include <iostream>
#include <vector>
using namespace std;

int Solve(int n)
{
	int Total=0;
	for(int i=0;i<n;i++)
	{
		if(i%2==0)
		{
			Total+=i;
		}
		else
		{
			Total-=i;
		}
	}
	return Total;
}

int main()
{
	int n=10;
	cout<<Solve(n)<<endl;
	return 0;
}
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_contest(root):
    write(root / "Alice" / "T1.cpp", STYLE_A)
    write(root / "Alice" / "T2.cpp", STYLE_A)
    write(root / "Alice" / "T3.cpp", STYLE_B)
    write(root / "Alice" / "T4.cpp", STYLE_B)
    write(root / "Bob" / "T1.cpp", STYLE_A.replace("solve", "compute"))


def test_scan_contest_structure_and_multi_style(tmp_path):
    contest = tmp_path / "Contest"
    make_contest(contest)
    scan = scan_contest(str(contest), store_path=str(tmp_path / "profiles.json"))
    data = scan.to_dict()

    assert data["summary"]["users"] == 2
    assert data["summary"]["submissions"] == 5
    assert [u["user"] for u in data["users"]] == ["Alice", "Bob"]
    alice = next(u for u in data["users"] if u["user"] == "Alice")
    assert {f["problem"] for f in alice["files"]} == {"T1", "T2", "T3", "T4"}
    assert alice["style"]["multi_style"] is True
    assert len(alice["groups"]) == 2
    assert alice["attention"] in {"high", "medium"}
    assert len(alice["style_features"]) == 57
    assert alice["files"][0]["id"] == "Alice/T1.cpp"
    assert data["files"][0]["id"].startswith("Alice/")


def test_duplicate_pair_and_source_api(tmp_path):
    contest = tmp_path / "Contest"
    write(contest / "Alice" / "T1.cpp", STYLE_A)
    write(contest / "Bob" / "T1.cpp", STYLE_A.replace("solve", "compute"))
    scan = scan_contest(str(contest), store_path=str(tmp_path / "profiles.json"))
    data = scan.to_dict()
    assert len(data["duplicates"]["pairs"]) == 1
    pair = data["duplicates"]["pairs"][0]
    assert pair["cross_user"] is True
    assert pair["user_a"] != pair["user_b"]
    assert pair["evidence"]
    source = scan.source("Alice/T1.cpp")
    assert source["language"] == "cpp"
    assert "int solve" in source["text"]
    assert source["encoding"] == "utf-8"


def test_save_merge_does_not_double_count(tmp_path):
    past = tmp_path / "Past"
    write(past / "Alice" / "T1.cpp", STYLE_A)
    store = str(tmp_path / "profiles.json")
    first = scan_contest(str(past), store_path=store)
    saved = first.save_profiles(["Alice"], "replace")
    assert saved[0]["added"] == 1
    assert first.to_dict()["users"][0]["saved_profile"]["exists"] is True

    current = tmp_path / "Current"
    write(current / "Alice" / "T2.cpp", STYLE_A.replace("solve", "compute"))
    second = scan_contest(str(current), store_path=store)
    before = second.to_dict()["users"][0]
    assert before["saved_profile"]["samples"] == 1
    assert before["saved_overlap"] == 0

    saved = second.save_profiles(["Alice"], "merge")
    assert saved[0]["added"] == 1
    after = second.to_dict()["users"][0]
    assert after["saved_profile"]["samples"] == 2
    assert after["saved_score"] is not None

    again = scan_contest(str(current), store_path=store)
    saved = again.save_profiles(["Alice"], "merge")
    assert saved[0]["added"] == 0
    assert again.to_dict()["users"][0]["saved_profile"]["samples"] == 2


def test_scan_rejects_missing_folder(tmp_path):
    try:
        scan_contest(str(tmp_path / "missing"), store_path=str(tmp_path / "p.json"))
    except ClassroomError as exc:
        assert "不存在" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ClassroomError")


def test_web_scan_source_and_save(tmp_path):
    contest = tmp_path / "Contest"
    make_contest(contest)
    store = str(tmp_path / "profiles.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ReviewHandler)
    httpd.daemon_threads = True
    httpd.lock = threading.RLock()
    httpd.current_scan = None
    httpd.store_path = store
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def request(path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    try:
        status, meta = request("/api/meta")
        assert status == 200
        assert "cpp" in meta["languages"]
        assert ".cpp" in meta["extensions"]

        status, result = request("/api/scan", {"root": str(contest), "store": store})
        assert status == 200
        assert result["scan"]["summary"]["users"] == 2

        status, source = request("/api/source?id=Alice%2FT1.cpp")
        assert status == 200
        assert "int solve" in source["source"]["text"]

        status, saved = request(
            "/api/save-profiles", {"users": ["Alice"], "mode": "merge"}
        )
        assert status == 200
        assert saved["saved"][0]["user"] == "Alice"
        assert saved["scan"]["users"][0]["saved_profile"]["exists"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
