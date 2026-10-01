"""授权柜端到端口径自检（只用标准库，直接驱动 service，不依赖 FastAPI）。

覆盖：
- 实时预览（受影响任务/库存/可行性）
- 默认继承工程归属、显式授权、只读拒绝（且不暴露供应商）
- 权限缓存与库存预占原子更新；多线程并发领料不超授权额度/可发库存
- 紧急例外：非指挥不能批准、指挥批准后放行
- 发料：预占转实发，库存/待办/车辆装载清单收敛
- 项目转组：后续继承切换，但历史领用快照不改写
"""
from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, ".")

from app.services.grant import grant_service, PERM_ISSUE, PERM_NONE  # noqa: E402
from app.store import store  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}{(' :: ' + detail) if detail and not condition else ''}")
    if not condition:
        failures.append(name)


def stock_of(code: str):
    m = store.find_by("material", "材料编号", code)
    return int(m["库存数量"]), int(m["预占数量"]), int(m["库存数量"]) - int(m["预占数量"])


# 1. 实时预览 ----------------------------------------------------------------
prev = grant_service.preview("PROJ-0001", "", "沥青类", "城东养护所", 40)
check("预览-路段回落工程登记", prev["施工路段"] == "城东快速路")
pave_tasks = [t for t in prev["受影响任务"] if t["模块"] == "路面病害"]
check("预览-匹配到城东快速路病害任务", len(pave_tasks) == 2, str([t["编号"] for t in prev["受影响任务"]]))
check("预览-沥青类库存可发=库存-预占", prev["库存汇总"]["可发数量"] == prev["库存汇总"]["库存数量"] - prev["库存汇总"]["预占数量"])
check("预览-40吨额度60且库存充足可领用", prev["可行性"]["结论"] == "可领用")

prev_big = grant_service.preview("PROJ-0001", "", "沥青类", "城东养护所", 1000)
check("预览-超量申请判定不可领用", prev_big["可行性"]["结论"] == "暂不可领用")

# 预览不暴露供应商
check("预览-库存清单不含供应商", all("供应商" not in item for item in prev["库存"]))

# 2. 默认继承 / 只读拒绝 / 供应商遮蔽 ---------------------------------------
d_owner = grant_service.evaluate("PROJ-0003", "跨河大桥", "桥梁构件类", "桥梁养护所")
check("继承-归属单位默认领用", d_owner["权限"] == PERM_ISSUE and d_owner["是否继承"] is True)
d_other = grant_service.evaluate("PROJ-0003", "跨河大桥", "桥梁构件类", "外来单位")
check("继承-非归属单位无权限", d_other["权限"] == PERM_NONE)

req, msg, allowed = grant_service.apply_requisition({
    "工程编号": "PROJ-0001", "材料编号": "MATE-0001",
    "领用单位": "外协抢修三队", "领用人": "赵队", "申请数量": 5, "用途": "越权测试",
})
check("越权-只读单位被拒绝", allowed is False and req["状态"] == "已拒绝")
check("越权-拒绝单据不含供应商/材料名", "供应商" not in req and not req.get("材料名称"))

scoped = {r["材料编号"]: r for r in grant_service.scoped_materials("PROJ-0001", "外协抢修三队", "沥青类")}
check("越权-只读视图遮蔽供应商", scoped["MATE-0001"]["供应商"] == "***（无权查看）")
scoped_ok = {r["材料编号"]: r for r in grant_service.scoped_materials("PROJ-0001", "城东养护所", "沥青类")}
check("权限内-领用单位可见供应商", scoped_ok["MATE-0001"]["供应商"] == "东海沥青股份有限公司")

# 3. 原子预占 + 并发不超额 ---------------------------------------------------
on_hand, reserved, available = stock_of("MATE-0002")  # AC-13, 80吨, 无预占
# 给一个干净维度：用交安材料 MATE-0009 波形护栏 300 片 + 新授权额度
target = store.find_by("material", "材料编号", "MATE-0009")
before = (int(target["库存数量"]), int(target["预占数量"]))

grant_service.upsert_grant({
    "工程编号": "PROJ-0002", "施工路段": "沿江路", "材料类别": "交安材料类",
    "领用单位": "并发测试队", "权限": "领用", "授权额度": 100, "授权人": "测试",
})
d = grant_service.evaluate("PROJ-0002", "沿江路", "交安材料类", "并发测试队")
check("并发-新授权额度100", d["授权额度"] == 100 and d["剩余额度"] == 100)

results: list[bool] = []
lock = threading.Lock()


def demand():
    entry, message, ok = grant_service.apply_requisition({
        "工程编号": "PROJ-0002", "材料编号": "MATE-0009",
        "领用单位": "并发测试队", "申请数量": 20, "用途": "并发压测",
    })
    with lock:
        results.append(ok)


with ThreadPoolExecutor(max_workers=12) as pool:
    list(pool.map(lambda _: demand(), range(12)))

accepted = sum(1 for ok in results if ok)
d_after = grant_service.evaluate("PROJ-0002", "沿江路", "交安材料类", "并发测试队")
print(f"   并发12单每单20片：放行 {accepted} 单，额度剩余 {d_after['剩余额度']}")
check("并发-仅5单在100额度内放行", accepted == 5, f"accepted={accepted}")
check("并发-预占额度恰为100", d_after["预占额度"] == 100 and d_after["剩余额度"] == 0)
after = stock_of("MATE-0009")
check("并发-库存预占=100且未超可发", after[1] - before[1] == 100 and after[2] >= 0, str(after))

# 额度用尽后再来必拒
_, m_over, over_ok = grant_service.apply_requisition({
    "工程编号": "PROJ-0002", "材料编号": "MATE-0009",
    "领用单位": "并发测试队", "申请数量": 1, "用途": "超额",
})
check("并发-额度用尽拒绝", over_ok is False and "额度不足" in m_over)

# 4. 紧急例外：非指挥不能批准，指挥批准后放行 -------------------------------
# 用一个非归属单位：它原本无任何权限，临时例外是它唯一的领用通道。
emergency_unit = "外协抢险三队"
exc, _ = grant_service.apply_exception({
    "工程编号": "PROJ-0003", "施工路段": "跨河大桥", "材料类别": "应急抢险类",
    "申请单位": emergency_unit, "申请数量": 8, "事由": "桥墩冲刷应急",
})
exc_id = exc["id"]
before_d = grant_service.evaluate("PROJ-0003", "跨河大桥", "应急抢险类", emergency_unit)
check("例外-审批前无权限", before_d["权限"] == PERM_NONE)

entry_bad, bad_msg = grant_service.approve_exception(
    exc_id, approver="李科员", approver_title="材料员", opinion="同意", approved=True
)
check("例外-非指挥身份无法激活", entry_bad is None and "指挥" in bad_msg)
still = store.find("material_exception", exc_id)
check("例外-非指挥后仍待指挥审批", still["状态"] == "待指挥审批")

entry_ok, ok_msg = grant_service.approve_exception(
    exc_id, approver="王指挥", approver_title="现场总指挥", opinion="同意，抢险使用", approved=True
)
check("例外-指挥批准生效", entry_ok is not None and entry_ok["状态"] == "已批准")
after_d = grant_service.evaluate("PROJ-0003", "跨河大桥", "应急抢险类", emergency_unit)
check("例外-批准后获得临时领用", after_d["权限"] == PERM_ISSUE and after_d["例外编号"] == entry_ok["例外编号"])
r_exc, _, allowed_exc = grant_service.apply_requisition({
    "工程编号": "PROJ-0003", "材料编号": "MATE-0011",
    "领用单位": emergency_unit, "申请数量": 8, "用途": "桥墩冲刷应急",
})
check("例外-凭临时例外放行预占", allowed_exc and r_exc["紧急例外"] == entry_ok["例外编号"])
# 超例外额度
_, m_exc_over, exc_over_ok = grant_service.apply_requisition({
    "工程编号": "PROJ-0003", "材料编号": "MATE-0011",
    "领用单位": emergency_unit, "申请数量": 5, "用途": "再领",
})
check("例外-超临时额度拒绝", exc_over_ok is False and "额度不足" in m_exc_over)

# 5. 发料：预占转实发，三处落地收敛 -----------------------------------------
reserved_reqs = [r for r in store.rows("material_requisition") if r["状态"] == "已预占"]
target_req = reserved_reqs[0]
mat_code = target_req["材料编号"]
mat = store.find_by("material", "材料编号", mat_code)
oh, rs = int(mat["库存数量"]), int(mat["预占数量"])
qty = int(target_req["申请数量"])
issued, imsg = grant_service.issue_requisition(int(target_req["id"]), "苏A·X999")
check("发料-状态转已发放", issued is not None and issued["状态"] == "已发放")
mat2 = store.find_by("material", "材料编号", mat_code)
check("发料-预占转实发（库存-数量，预占-数量）",
      int(mat2["预占数量"]) == rs - qty and int(mat2["库存数量"]) == oh - qty, imsg)
load_done = [l for l in store.rows("vehicle_load") if l["领用单号"] == target_req["领用单号"]][0]
check("发料-车辆装载清单更新为已发运并记录车牌", load_done["状态"] == "已发运" and load_done["车牌号"] == "苏A·X999")
todo_done = [t for t in store.rows("project_todo") if t["关联单号"] == target_req["领用单号"]]
check("发料-工程待办收敛为已处理", todo_done and all(t["状态"] == "已处理" for t in todo_done))
ledger = [x for x in mat2["授权结论台账"] if x["领用单号"] == target_req["领用单号"]]
check("发料-材料台账结论为放行且无供应商", ledger and ledger[-1]["授权结论"] == "放行" and "供应商" not in ledger[-1])

# 6. 项目转组：历史关系不改写 -----------------------------------------------
proj = store.find("project", 1)  # PROJ-0001
code = proj["工程编号"]
history_before = [dict(r.get("历史快照", {})) for r in store.rows("material_requisition") if r["工程编号"] == code]
new_proj, tmsg, frozen = grant_service.transfer_project(1, "应急抢险中心", operator="管理员")
check("转组-归属单位更新", new_proj["归属单位"] == "应急抢险中心")
check("转组-历史领用单数量保持", frozen == len(history_before), f"frozen={frozen}")
history_after = [dict(r.get("历史快照", {})) for r in store.rows("material_requisition") if r["工程编号"] == code]
check("转组-历史快照不被改写", history_before == history_after)
d_old_owner = grant_service.evaluate(code, "城东快速路", "沥青类", "城东养护所")
d_new_owner = grant_service.evaluate(code, "城东快速路", "沥青类", "应急抢险中心")
check("转组-原归属单位不再继承", d_old_owner["权限"] != PERM_ISSUE or d_old_owner.get("授权编号"))
check("转组-新归属单位获得继承", d_new_owner["权限"] == PERM_ISSUE and d_new_owner["是否继承"] is True)

print("\n结果：", "全部通过 ✅" if not failures else f"{len(failures)} 项失败 ❌")
sys.exit(1 if failures else 0)
