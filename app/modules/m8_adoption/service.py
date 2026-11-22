import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from time import perf_counter

from app.config import ADOPTION_VERSION
from app.keys import ReplayError, identifier, nonnegative_count, partition, timestamp
from app.modules.m8_adoption.reader import read_arrivals

SYSTEMS = {"primary": "主系统", "backup": "备用系统"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(raw):
    return digest(ADOPTION_VERSION.encode() + b":" + raw)[:26]


def evidence(entry):
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def heads(records):
    result = {}
    for entry in records:
        try:
            if ("rows" in entry) == ("count" in entry):
                raise ValueError("行数新旧字段必须择一")
            key = partition(entry["dataset"], entry["day"])
            value = {
                "version": identifier(entry["version"]),
                "rows": nonnegative_count(entry.get("rows", entry.get("count"))),
            }
            if key in result:
                raise ValueError("分区指针重复")
        except (ValueError, TypeError, KeyError) as exc:
            raise ReplayError("INVALID_HEAD", "现行指针无效或重复") from exc
        result[key] = value
    return result


def candidate(entry, line):
    if not isinstance(entry, dict):
        raise ValueError("记录须为对象")
    system = entry["system"]
    if system not in SYSTEMS:
        raise ValueError("来源系统未登记")
    part = partition(entry["dataset"], entry["day"])
    if ("rows" in entry) == ("count" in entry):
        raise ValueError("行数新旧字段必须择一")
    rows = nonnegative_count(entry.get("rows", entry.get("count")))
    version = identifier(entry["version"])
    previous = entry["previous"]
    if previous is not None:
        previous = identifier(previous)
    arrived = timestamp(entry["arrived_at"])
    return {
        "源行": line,
        "分区": part,
        "系统": system,
        "版本": version,
        "行数": rows,
        "前序": previous,
        "到达时间": arrived,
        "原始记录": entry,
    }


def shelved_row(cand, code, note):
    return {
        "源行": cand["源行"],
        "分区": cand.get("分区"),
        "系统": SYSTEMS.get(cand.get("系统"), cand.get("系统")),
        "版本": cand.get("版本"),
        "行数": cand.get("行数"),
        "原因码": code,
        "说明": note,
        "证据摘要": evidence(cand["原始记录"])[:16],
        "原始记录": cand["原始记录"],
    }


def invalid_row(line, entry, note):
    part = system = version = None
    if isinstance(entry, dict):
        dataset, date_text = entry.get("dataset"), entry.get("day")
        if isinstance(dataset, str) and isinstance(date_text, str):
            try:
                part = partition(dataset, date_text)
            except ValueError:
                part = None
        if isinstance(entry.get("system"), str):
            system = entry["system"]
        if isinstance(entry.get("version"), str):
            version = entry["version"]
    return {
        "源行": line,
        "分区": part,
        "系统": SYSTEMS.get(system, system),
        "版本": version,
        "行数": None,
        "原因码": "INVALID",
        "说明": note,
        "证据摘要": evidence(entry)[:16],
        "原始记录": entry,
    }


def keep_row(key, head):
    return {
        "分区": key,
        "结论": "维持现行",
        "版本": head["version"],
        "行数": head["rows"],
        "来源": "现行指针",
        "依据": "无更新版本或候选均被搁置",
        "到达时间": None,
        "源行": None,
    }


def blocked_row(key, code, note, head, lines):
    suffix = "；现行版本仍可使用" if head else "；分区无现行版本，下游需等待"
    return {
        "分区": key,
        "原因码": code,
        "说明": note + suffix,
        "现行": head["version"] if head else None,
        "涉及源行": lines,
    }


def conclude(key, head, cands, invalid_rows):
    """单分区结论。到达时间只作证据留存，采信只看版本谱系与来源权威。"""
    out = []
    alarm = None

    def flag(code, note):
        nonlocal alarm
        if alarm is None:
            alarm = (code, note)

    groups = defaultdict(list)
    for cand in cands:
        groups[(cand["系统"], cand["版本"])].append(cand)
    eligible = []
    for (system, version), copies in sorted(groups.items()):
        if len({(c["行数"], c["前序"]) for c in copies}) > 1:
            note = "同一系统重发同版本但内容不一致"
            out += [shelved_row(c, "CONFLICT", note) for c in copies]
            flag("CONFLICT", note)
        else:
            eligible += copies
    known = {c["版本"] for c in cands} | ({head["version"]} if head else set())
    rest = []
    for cand in eligible:
        if head and cand["版本"] == head["version"]:
            if cand["行数"] == head["rows"]:
                out.append(shelved_row(cand, "REDUNDANT", "与现行版本一致，无需重采"))
            else:
                note = "与现行版本同号但行数不符"
                out.append(shelved_row(cand, "CONFLICT", note))
                flag("CONFLICT", note)
        elif head and cand["前序"] is None:
            note = "声称首次到达但分区已有现行版本"
            out.append(shelved_row(cand, "CONFLICT", note))
            flag("CONFLICT", note)
        elif cand["前序"] is not None and cand["前序"] not in known:
            note = "前序版本无到达证据，谱系断档"
            out.append(shelved_row(cand, "GAP", note))
            flag("GAP", note)
        else:
            rest.append(cand)
    versions = {c["版本"] for c in rest}
    superseded = {c["前序"] for c in rest if c["前序"] in versions}
    out += [shelved_row(c, "SUPERSEDED", "同分区已有更新版本到达") for c in rest if c["版本"] in superseded]
    tips = [c for c in rest if c["版本"] not in superseded]
    lines = sorted([c["源行"] for c in cands] + [r["源行"] for r in invalid_rows])
    if not tips:
        row = keep_row(key, head) if head else None
        block = None
        if alarm:
            block = blocked_row(key, alarm[0], alarm[1], head, lines)
        elif not head:
            block = blocked_row(key, "NO_VALID", "无有效候选", head, lines)
        return row, block, out
    tip_versions = {c["版本"] for c in tips}
    if len(tip_versions) > 1:
        note = "谱系分叉，与另一最新版本互斥"
        out += [shelved_row(c, "CONFLICT", note) for c in tips]
        row = keep_row(key, head) if head else None
        return row, blocked_row(key, "FORK", "多个互斥的最新版本，无法判定采信", head, lines), out
    version = tip_versions.pop()
    if len({(c["行数"], c["前序"]) for c in tips}) > 1:
        note = "双源同版本但内容不一致"
        out += [shelved_row(c, "CONFLICT", note) for c in tips]
        row = keep_row(key, head) if head else None
        return row, blocked_row(key, "CONFLICT", "最新版本的双源内容不一致", head, lines), out
    if head and version == head["version"]:
        return keep_row(key, head), None, out
    primary = [c for c in tips if c["系统"] == "primary"]
    chosen = primary[0] if primary else tips[0]
    for cand in tips:
        if cand is not chosen:
            out.append(shelved_row(cand, "CONFIRMING", f"与采信版本一致（采信源行 {chosen['源行']}）"))
    if primary and len(tips) > 1:
        basis = "双源一致，采信主系统"
    elif primary:
        basis = "仅主系统到达"
    else:
        basis = "主系统未到，备用先行"
    row = {
        "分区": key,
        "结论": "采信新版本",
        "版本": chosen["版本"],
        "行数": chosen["行数"],
        "来源": SYSTEMS[chosen["系统"]],
        "依据": basis,
        "到达时间": chosen["到达时间"],
        "源行": chosen["源行"],
    }
    return row, None, out


def decide(current, entries):
    shelved = []
    invalid_by_part = defaultdict(list)
    pool = []
    for line, entry in enumerate(entries, 1):
        try:
            pool.append(candidate(entry, line))
        except (ValueError, TypeError, KeyError) as exc:
            row = invalid_row(line, entry, str(exc) or "记录无效")
            shelved.append(row)
            if row["分区"]:
                invalid_by_part[row["分区"]].append(row)
    seen = {}
    by_part = defaultdict(list)
    for cand in pool:
        sig = (cand["分区"], cand["系统"], cand["版本"], cand["行数"], cand["前序"])
        if sig in seen:
            shelved.append(shelved_row(cand, "DUPLICATE", f"与源行 {seen[sig]} 完全重传，内容一致"))
        else:
            seen[sig] = cand["源行"]
            by_part[cand["分区"]].append(cand)
    adopted, blocked = [], []
    touched = set(by_part) | set(invalid_by_part)
    for key in sorted(touched):
        row, block, out = conclude(
            key, current.get(key), by_part.get(key, []), invalid_by_part.get(key, [])
        )
        shelved.extend(out)
        if row:
            adopted.append(row)
        if block:
            blocked.append(block)
    shelved.sort(key=lambda r: r["源行"])
    replay_view = {
        row["分区"]: {"版本": row["版本"], "行数": row["行数"], "来源": row["来源"]} for row in adopted
    }
    rollback_view = {
        row["分区"]: (
            {"版本": current[row["分区"]]["version"], "行数": current[row["分区"]]["rows"]}
            if row["分区"] in current
            else None
        )
        for row in adopted
        if row["结论"] == "采信新版本"
    }
    return {
        "采信": adopted,
        "搁置": shelved,
        "不可采信": blocked,
        "重放读数": replay_view,
        "回滚读数": rollback_view,
        "分区触及": len(touched),
        "主系统到达": sum(1 for c in pool if c["系统"] == "primary"),
        "备用到达": sum(1 for c in pool if c["系统"] == "backup"),
    }


def compute(raw):
    began = perf_counter()
    value = read_arrivals(raw)
    current = heads(value["heads"])
    result = decide(current, value["arrivals"])
    adopted, shelved, blocked = result["采信"], result["搁置"], result["不可采信"]
    new = [row for row in adopted if row["结论"] == "采信新版本"]
    concluded = {row["分区"] for row in adopted}
    gaps = [row for row in blocked if row["分区"] not in concluded]
    check = {
        "到达记录": len(value["arrivals"]),
        "采信记录": len(new),
        "搁置记录": len(shelved),
        "记录差": len(value["arrivals"]) - len(new) - len(shelved),
        "分区触及": result["分区触及"],
        "采信新版本": len(new),
        "维持现行": len(adopted) - len(new),
        "不可采信": len(blocked),
        "无结论": len(gaps),
        "分区差": result["分区触及"] - len(adopted) - len(gaps),
        "主系统到达": result["主系统到达"],
        "备用到达": result["备用到达"],
        "冲突候选": sum(1 for row in shelved if row["原因码"] == "CONFLICT"),
    }
    audit = {
        "证据摘要": {str(line): evidence(entry) for line, entry in enumerate(value["arrivals"], 1)},
        "结论摘要": digest(
            json.dumps(
                {"采信": adopted, "搁置": shelved, "不可采信": blocked},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ),
        "搁置保留": "搁置候选的原始记录随本结论一并存档，平台不提供删除入口",
    }
    summary = {
        "编号": identity(raw),
        "规则": ADOPTION_VERSION,
        "文件摘要": digest(raw),
        "生成时间": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "时区": "UTC",
        "到达记录": len(value["arrivals"]),
        "处理毫秒": round((perf_counter() - began) * 1000),
    }
    return {
        "批次": summary,
        "勾稽": check,
        "采信": adopted,
        "搁置": shelved,
        "不可采信": blocked,
        "重放读数": result["重放读数"],
        "回滚读数": result["回滚读数"],
        "回查摘要": audit,
    }


def execute(store, raw):
    key = identity(raw)
    previous = store.get(key)
    return (previous, True) if previous else store.save(key, compute(raw))
