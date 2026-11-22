from app.config import MAX_BYTES
from app.keys import ReplayError
from app.modules.m8_adoption.service import execute
from app.web import arrival_fixture, conclusion, ledger
from flask import Blueprint, jsonify, request, url_for

api = Blueprint("adoption", __name__)


@api.get("/api/adoptions")
def listing():
    return jsonify(ledger().latest())


@api.post("/api/adoptions")
def create():
    args = request.get_json(silent=True) if request.is_json else request.form
    if not hasattr(args, "get"):
        raise ReplayError("JSON", "请选择样例或上传文件")
    sample = args.get("sample", "arrivals")
    source = request.files.get("bundle")
    raw = source.read(MAX_BYTES + 1) if source and source.filename else arrival_fixture(sample)
    result, reused = execute(ledger(), raw)
    key = result["批次"]["编号"]
    return jsonify(
        id=key,
        reused=reused,
        summary=result["勾稽"],
        next=url_for(
            "adoption_console", conclusion=key, sample=sample, reused="yes" if reused else None
        ),
    ), 200 if reused else 201


@api.get("/api/adoptions/<key>")
def inspection(key):
    return jsonify(conclusion(key))


@api.get("/api/adoptions/<key>/replay")
def replay(key):
    doc = conclusion(key)
    gaps = [row for row in doc["不可采信"] if row["分区"] not in doc["重放读数"]]
    return jsonify({"批次": doc["批次"]["编号"], "重放读数": doc["重放读数"], "缺口": gaps})


@api.get("/api/adoptions/<key>/rollback")
def rollback(key):
    doc = conclusion(key)
    return jsonify({"批次": doc["批次"]["编号"], "回滚读数": doc["回滚读数"]})


@api.get("/api/adoptions/<key>/export")
def export(key):
    response = jsonify(conclusion(key))
    response.headers.set("Content-Disposition", "attachment", filename=f"adoption-{key}.json")
    return response
