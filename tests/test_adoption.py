import hashlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from app.config import ROOT
from app.keys import ReplayError
from app.modules.m8_adoption.reader import read_arrivals
from app.modules.m8_adoption.service import compute, execute, heads
from app.modules.m8_adoption.store import Ledger


@pytest.fixture(scope="module")
def dual():
    return (ROOT / "sample_data" / "arrivals.json").read_bytes()


@pytest.fixture(scope="module")
def tidy():
    return (ROOT / "sample_data" / "arrivals_clean.json").read_bytes()


@pytest.fixture(scope="module")
def night(dual):
    return compute(dual)


@pytest.fixture(scope="module")
def clean(tidy):
    return compute(tidy)


def adopted(doc, key):
    return next(row for row in doc["采信"] if row["分区"] == key)


def shelved(doc, line):
    return next(row for row in doc["搁置"] if row["源行"] == line)


# TRAP-27 到达时间不参与采信：备用先到仍采主系统
def test_primary_wins_despite_later_arrival(night):
    row = adopted(night, "orders@2026-11-21")
    assert (row["版本"], row["来源"], row["依据"], row["源行"]) == (
        "p22",
        "主系统",
        "双源一致，采信主系统",
        4,
    )
    assert shelved(night, 2)["原因码"] == "CONFIRMING"


def test_backup_first_when_primary_absent(night):
    row = adopted(night, "buyers@2026-11-21")
    assert (row["来源"], row["依据"], row["行数"]) == ("备用系统", "主系统未到，备用先行", 122)


# TRAP-28 备用重发旧版本不能盖掉现行
def test_resent_old_version_never_overwrites(night):
    assert shelved(night, 8)["原因码"] == "REDUNDANT"
    assert shelved(night, 9)["原因码"] == "CONFLICT"
    row = adopted(night, "orders@2026-11-20")
    assert (row["结论"], row["版本"], row["行数"]) == ("维持现行", "p20", 1020)


def test_superseded_backup_resend(night):
    assert shelved(night, 17)["原因码"] == "SUPERSEDED"
    assert adopted(night, "day0@2026-11-21")["版本"] == "d23"


# TRAP-29 双源同版本行数打架：不可采信新版本
def test_cross_system_row_clash_blocked(night):
    block = next(row for row in night["不可采信"] if row["分区"] == "orders@2026-11-18")
    assert block["原因码"] == "CONFLICT" and block["涉及源行"] == [12, 13]
    assert adopted(night, "orders@2026-11-18")["结论"] == "维持现行"


def test_forked_lineage_blocked(night):
    block = next(row for row in night["不可采信"] if row["分区"] == "orders@2026-11-19")
    assert block["原因码"] == "FORK"
    assert shelved(night, 10)["原因码"] == shelved(night, 11)["原因码"] == "CONFLICT"


# TRAP-30 同系统重发同版本不同内容：整组搁置
def test_same_system_resend_conflict(night):
    assert shelved(night, 19)["原因码"] == shelved(night, 20)["原因码"] == "CONFLICT"
    block = next(row for row in night["不可采信"] if row["分区"] == "ops@2026-11-21")
    assert block["原因码"] == "CONFLICT" and block["现行"] == "o21"


def test_gap_and_invalid_leave_no_read(night):
    blocks = {row["分区"]: row["原因码"] for row in night["不可采信"]}
    assert blocks["facts@2026-11-22"] == "GAP"
    assert blocks["buyers@2026-11-15"] == blocks["day0@2026-11-22"] == "NO_VALID"
    gaps = [row for row in night["不可采信"] if row["分区"] not in night["重放读数"]]
    assert len(gaps) == night["勾稽"]["无结论"] == 3


def test_duplicate_and_invalid_shelved(night):
    assert shelved(night, 3)["原因码"] == "DUPLICATE"
    assert shelved(night, 16)["原因码"] == "DUPLICATE"
    assert [r["源行"] for r in night["搁置"] if r["原因码"] == "INVALID"] == [21, 22, 23]


def test_legacy_count_and_fullwidth_rows(night):
    assert adopted(night, "buyers@2026-11-21")["行数"] == 122
    assert adopted(night, "facts@2026-11-21")["行数"] == 1022


def test_conservation_balances(night, clean):
    for doc in (night, clean):
        assert doc["勾稽"]["记录差"] == 0 and doc["勾稽"]["分区差"] == 0
        assert doc["勾稽"]["到达记录"] == doc["勾稽"]["采信记录"] + doc["勾稽"]["搁置记录"]


# TRAP-31 重试同一到达快照不新增第二份
def test_retry_never_creates_second_conclusion(client):
    a = client.post("/api/adoptions", json={"sample": "arrivals_clean"})
    b = client.post("/api/adoptions", json={"sample": "arrivals_clean"})
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json["id"] == b.json["id"] and b.json["reused"]
    assert len(client.get("/api/adoptions").json) == 1


def test_parallel_convergence(tmp_path, tidy):
    store = Ledger(tmp_path / "archive.sqlite")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(store, tidy), range(4)))
    assert sum(not reused for _, reused in results) == 1
    assert len(store.latest()) == 1


def test_persistent_conclusion(tmp_path, tidy):
    path = tmp_path / "archive.sqlite"
    result, _ = execute(Ledger(path), tidy)
    again = Ledger(path).get(result["批次"]["编号"])
    assert again["勾稽"]["采信新版本"] == 2 and again["批次"]["规则"] == "adopt-1.0"


# TRAP-32 搁置候选原证据随结论存档，原日志误删仍可复核
def test_shelved_evidence_preserved(client):
    key = client.post("/api/adoptions", json={"sample": "arrivals"}).json["id"]
    doc = client.get(f"/api/adoptions/{key}").json
    row = next(r for r in doc["搁置"] if r["源行"] == 9)
    assert row["原始记录"] == {
        "system": "primary",
        "dataset": "orders",
        "day": "2026-11-20",
        "version": "p20",
        "rows": 999,
        "previous": "p19",
        "arrived_at": "2026-11-22T06:50:00Z",
    }
    assert row["证据摘要"] == doc["回查摘要"]["证据摘要"]["9"][:16]
    assert len(doc["回查摘要"]["证据摘要"]) == doc["勾稽"]["到达记录"]


# 重放、回滚、回查读取同一份结论
def test_replay_rollback_audit_share_one_conclusion(client):
    key = client.post("/api/adoptions", json={"sample": "arrivals"}).json["id"]
    doc = client.get(f"/api/adoptions/{key}").json
    replay = client.get(f"/api/adoptions/{key}/replay").json
    rollback = client.get(f"/api/adoptions/{key}/rollback").json
    assert replay["批次"] == rollback["批次"] == doc["批次"]["编号"]
    assert replay["重放读数"] == doc["重放读数"]
    assert rollback["回滚读数"] == doc["回滚读数"]
    reads = {row["分区"]: row["版本"] for row in doc["采信"]}
    assert reads == {key: value["版本"] for key, value in replay["重放读数"].items()}
    assert rollback["回滚读数"]["orders@2026-11-21"] == {"版本": "p21", "行数": 1021}
    assert rollback["回滚读数"]["orders@2026-11-22"] is None


def test_upload_is_attested(client, tidy):
    run = client.post("/api/adoptions", data={"bundle": (BytesIO(tidy), "source.json")}).json
    pack = client.get(f"/api/adoptions/{run['id']}/export")
    assert "attachment" in pack.headers["Content-Disposition"]
    assert pack.json["批次"]["文件摘要"] == hashlib.sha256(tidy).hexdigest()


def test_bad_input_stores_nothing(client):
    response = client.post("/api/adoptions", data={"bundle": (BytesIO(b"{"), "broken.json")})
    assert response.status_code == 422 and response.json["code"] == "JSON"
    assert client.get("/api/adoptions").json == []


def test_truncated_input_rejected(tidy):
    with pytest.raises(ReplayError) as exc:
        read_arrivals(tidy.rstrip()[:-1])
    assert exc.value.code == "JSON"


def test_bom_tolerated(tidy):
    assert len(read_arrivals(b"\xef\xbb\xbf" + tidy)["arrivals"]) == 4


def test_head_dup_is_fatal(tidy):
    values = read_arrivals(tidy)["heads"]
    with pytest.raises(ReplayError) as exc:
        heads(values + values[:1])
    assert exc.value.code == "INVALID_HEAD"


def test_unknown_sample_rejected(client):
    response = client.post("/api/adoptions", json={"sample": "nightly"})
    assert response.status_code == 422 and response.json["code"] == "SAMPLE"


def test_bad_arrival_time_shelved():
    bundle = {
        "heads": [],
        "arrivals": [
            {
                "system": "primary",
                "dataset": "orders",
                "day": "2026-11-21",
                "version": "p22",
                "rows": 1022,
                "previous": None,
                "arrived_at": "昨天",
            }
        ],
    }
    import json

    doc = compute(json.dumps(bundle, ensure_ascii=False).encode())
    assert doc["搁置"][0]["原因码"] == "INVALID"
    assert "到达时间" in doc["搁置"][0]["说明"]
    assert doc["不可采信"][0]["原因码"] == "NO_VALID"


def test_console_page_renders(client):
    page = client.get("/console/adoption")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "采信结论" in text and "搁置候选" in text and "不可采信" in text
    key = client.post("/api/adoptions", json={"sample": "arrivals_clean"}).json["id"]
    assert client.get(f"/console/adoption?conclusion={key}").status_code == 200
    assert client.get("/api/adoptions/missing").status_code == 404
