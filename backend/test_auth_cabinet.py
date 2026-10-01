"""授权柜业务逻辑自检：不依赖 fastapi，直接跑服务层。"""
import sys
import threading

sys.path.insert(0, ".")

from app.services.auth_cabinet import AuthCabinetService, AuthorizationError

svc = AuthCabinetService()
ok = 0
fail = 0


def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}")


print("== 1. 权限继承与直接授权 ==")
d = svc.resolve_effective(1, 1, "沥青混合料")
check("沥青摊铺一班/PROJ-0001/沥青混合料 = 直接授权领用", d["权限"] == "领用" and d["来源"] == "直接授权")
check("额度120、已领用30、剩余90", d["授权额度"] == 120 and d["已领用"] == 30 and d["剩余额度"] == 90)

d = svc.resolve_effective(1, 1, "集料")
check("显式只读压过继承", d["权限"] == "只读")

d = svc.resolve_effective(3, 3, "集料")
check("同单位未显式授权 → 工程归属继承", d["权限"] == "领用" and d["来源"] == "工程归属继承")

d = svc.resolve_effective(4, 1, "沥青混合料")
check("外协班对PROJ-0001 无权限", d["权限"] == "无权限")
d = svc.resolve_effective(2, 1, "沥青混合料")
check("桥隧中心班对PROJ-0001 无权限（不同承建单位）", d["权限"] == "无权限")

print("== 2. 预览：未授权不暴露供应商 ==")
pv = svc.preview(1, "沥青混合料", 4)  # 外协班
check("越权视角 供应商可见=False", pv["供应商可见"] is False)
check("库存行已剔除供应商", all("供应商" not in row for row in pv["stock"]))
pv2 = svc.preview(1, "沥青混合料", 1)
check("授权视角 供应商可见=True", pv2["供应商可见"] is True)
check("授权库存行含供应商", all(row.get("供应商") for row in pv2["stock"]))
check("预览返回关联任务", len(pv2["tasks"]) >= 1)

print("== 3. 越权领料必须拒绝，且不落供应商 ==")
try:
    svc.apply_requisition({"工程id": 1, "班组id": 4, "材料类别": "沥青混合料", "数量": 5, "领料人": "孟大柱"})
    check("越权领料抛异常", False)
except AuthorizationError as e:
    check("越权领料抛 403 forbidden", e.code == "forbidden" and e.http_status == 403)
    check("拒绝原因不含供应商字样", "华远" not in e.reason and "供应商" not in e.reason)
rej = [r for r in svc.list_rows("material_requisition") if r["状态"] == "已拒绝"][-1]
check("拒绝单已落台账", rej is not None)
check("拒绝单供应商为空", rej.get("供应商") == "")
check("拒绝单有越权拦截待办", any(t["授权结论"] == "拒绝领用" for t in svc.list_rows("project_todo", 工程id=1)))

print("== 4. 只读权限领料同样拒绝 ==")
try:
    svc.apply_requisition({"工程id": 1, "班组id": 1, "材料类别": "集料", "数量": 3, "领料人": "张海涛"})
    check("只读领料被拒", False)
except AuthorizationError as e:
    check("只读领料 forbidden", e.code == "forbidden")

print("== 5. 合法领料：额度+库存原子预占 ==")
before = svc.resolve_effective(1, 1, "沥青混合料")
r = svc.apply_requisition({"工程id": 1, "班组id": 1, "材料类别": "沥青混合料", "数量": 20,
                          "领料人": "张海涛", "车辆id": 1})
check("领料单已预占待出库", r["状态"] == "已预占待出库")
after = svc.resolve_effective(1, 1, "沥青混合料")
check("授权已预占 +20（0→20）", after["已预占"] == before["已预占"] + 20)
check("剩余额度 -20", after["剩余额度"] == before["剩余额度"] - 20)
stock = next(x for x in svc.list_rows("material_stock", 材料类别="沥青混合料") if x["材料编号"] == "MATE-0001")
check("库存已预占 +20", stock["已预占"] == 20)
check("装载清单生成待装车行", any(l["领料单号"] == r["领料单号"] and l["状态值"] == "待装车"
                                  for l in svc.list_rows("vehicle_load")))
check("工程待办含领料预占结论", any(t["关联单据"] == r["领料单号"] and t["授权结论"] == "允许领用"
                                   for t in svc.list_rows("project_todo", 工程id=1)))

print("== 6. 超授权额度拒绝（剩余70，申请80） ==")
try:
    svc.apply_requisition({"工程id": 1, "班组id": 1, "材料类别": "沥青混合料", "数量": 80, "领料人": "张海涛"})
    check("超额领料被拒", False)
except AuthorizationError as e:
    check("超额 over_quota", e.code == "over_quota")
stock = next(x for x in svc.list_rows("material_stock", 材料类别="沥青混合料") if x["材料编号"] == "MATE-0001")
check("拒绝后库存预占不变（仍20）", stock["已预占"] == 20)

print("== 7. 并发领料不能超授权额度 ==")
results, lock = [], threading.Lock()
def worker():
    try:
        row = svc.apply_requisition({"工程id": 2, "班组id": 2, "材料类别": "支座配件",
                                    "数量": 25, "领料人": "陈伟强"})
        with lock:
            results.append(("ok", row["领料单号"]))
    except AuthorizationError as e:
        with lock:
            results.append(("reject", e.code))
threads = [threading.Thread(target=worker) for _ in range(6)]  # 6×25=150 > 额度60
for t in threads:
    t.start()
for t in threads:
    t.join()
accepted = [x for x in results if x[0] == "ok"]
rejected = [x for x in results if x[0] == "reject"]
check(f"6并发(25/单, 额度60) 仅2单成功（实际{len(accepted)}）", len(accepted) == 2)
check("其余4单全部拒绝（实际%d）" % len(rejected), len(rejected) == 4)
check("所有拒绝都是 over_quota", all(code == "over_quota" for _, code in rejected))
grant = next(x for x in svc.list_rows("auth_grant", 工程id=2) if x["材料类别"] == "支座配件")
check("授权已预占恰好=50（2单×25），不超额", grant["已预占"] == 50)
stock = next(x for x in svc.list_rows("material_stock", 材料类别="支座配件") if x["材料编号"] == "MATE-0003")
check("库存预占=50，与授权一致", stock["已预占"] == 50)

print("== 8. 出库：预占转实领，结论落台账/装载清单 ==")
rid = r["id"]
out = svc.outbound_requisition(rid)
check("领料单已出库", out["状态"] == "已出库")
after2 = svc.resolve_effective(1, 1, "沥青混合料")
check("预占清零、已领用+20", after2["已预占"] == 0 and after2["已领用"] == 50)
stock = next(x for x in svc.list_rows("material_stock", 材料类别="沥青混合料") if x["材料编号"] == "MATE-0001")
check("库存：预占0、已领用50", stock["已预占"] == 0 and stock["已领用"] == 50)
ledger = next(x for x in svc.list_rows("material") if x["材料编号"] == "MATE-0001")
check("材料台账写入授权结论与领料单号", ledger.get("关联领料单") == r["领料单号"] and "允许领用" in ledger.get("授权结论", ""))
check("台账累计出库=50", ledger.get("累计出库") == 50)
load = next(x for x in svc.list_rows("vehicle_load") if x["领料单号"] == r["领料单号"])
check("装载清单已装车且带结论", load["状态值"] == "已装车" and "允许领用" in load["授权结论"])

print("== 9. 取消预占原子释放 ==")
rc = svc.apply_requisition({"工程id": 3, "班组id": 3, "材料类别": "排水管材", "数量": 10, "领料人": "李卫东"})
svc.cancel_requisition(rc["id"])
g3 = next(x for x in svc.list_rows("auth_grant", 工程id=3) if x["材料类别"] == "排水管材")
check("取消后授权预占回滚为0", g3["已预占"] == 0)
st5 = next(x for x in svc.list_rows("material_stock", 材料类别="排水管材") if x["材料编号"] == "MATE-0005")
check("取消后库存预占回滚为0", st5["已预占"] == 0)

print("== 10. 紧急例外：以指挥审批为准 ==")
# 跨单位班组（外协）对 PROJ-0003 结构胶既无直接授权也无继承，最能体现例外审批的闸门作用
exc = svc.apply_exception({"工程id": 3, "班组id": 4, "材料类别": "结构胶",
                          "申请额度": 20, "事由": "K2+100管网坍塌紧急抢险，外协增援", "申请人": "孟大柱"})
check("例外申请进入待指挥审批", exc["状态"] == "待指挥审批")
try:
    svc.apply_requisition({"工程id": 3, "班组id": 4, "材料类别": "结构胶", "数量": 5, "领料人": "孟大柱"})
    check("审批前抢险领料不得放行", False)
except AuthorizationError as e:
    check("审批前仍是 forbidden", e.code == "forbidden")
svc.decide_exception(exc["id"], True, {"审批人": "总指挥 王磊", "审批意见": "同意，限期24小时", "有效期小时": 24})
d = svc.resolve_effective(4, 3, "结构胶")
check("批准后来源=紧急例外、额度20、带有效期", d["来源"] == "紧急例外" and d["授权额度"] == 20 and d["有效期止"])
rex = svc.apply_requisition({"工程id": 3, "班组id": 4, "材料类别": "结构胶", "数量": 5, "领料人": "孟大柱", "车辆id": 3})
check("批准后抢险领料放行", rex["状态"] == "已预占待出库")
try:
    svc.apply_requisition({"工程id": 3, "班组id": 4, "材料类别": "结构胶", "数量": 20, "领料人": "孟大柱"})
    check("例外额度同样不能超（已预占5，再申20>剩15）", False)
except AuthorizationError as e:
    check("例外超额 over_quota", e.code == "over_quota")

exc2 = svc.apply_exception({"工程id": 1, "班组id": 4, "材料类别": "沥青混合料",
                           "申请额度": 9, "事由": "外协临时支援", "申请人": "孟大柱"})
svc.decide_exception(exc2["id"], False, {"审批人": "副总指挥 赵刚"})
try:
    svc.apply_requisition({"工程id": 1, "班组id": 4, "材料类别": "沥青混合料", "数量": 1, "领料人": "孟大柱"})
    check("驳回的例外不产生授权", False)
except AuthorizationError as e:
    check("驳回后仍 forbidden", e.code == "forbidden")

print("== 11. 工程转组：历史领用关系不变 ==")
hist_before = [dict(x) for x in svc.list_rows("material_requisition", 工程id=1)]
res = svc.transfer_project(1, {"新承建单位": "桥隧养护中心"})
check("转组报告历史单数保持", res["历史关系保持不变"] is True)
d = svc.resolve_effective(1, 1, "排水管材")  # 沥青班原单位=城维一处，工程已转桥隧中心
check("转组后原单位班组失去继承权限", d["权限"] == "无权限")
d = svc.resolve_effective(2, 1, "排水管材")  # 桥梁作业班=桥隧中心
check("新单位班组获得继承权限", d["权限"] == "领用" and d["来源"] == "工程归属继承")
hist_after = svc.list_rows("material_requisition", 工程id=1)
check("历史领料单快照未被改写",
      [x["承建单位快照"] for x in hist_after] == [x["承建单位快照"] for x in hist_before]
      and all(x["工程名称快照"] for x in hist_after))
check("转组产生待办知会", any(t["类型"] == "工程转组" for t in svc.list_rows("project_todo", 工程id=1)))

print("== 12. 班组视角列表脱敏 ==")
rows = svc.list_rows("material_requisition", viewer_team_id=4)
visible = [r for r in rows if r.get("材料类别") == "结构胶" and r["状态"] != "已拒绝"]
rejected = [r for r in rows if r["状态"] == "已拒绝"]
masked = [r for r in rows if r.get("材料类别") != "结构胶"]
check("外协班：仅紧急例外批准的结构胶单据可见供应商", len(visible) == 1 and all(r.get("供应商") for r in visible))
check("外协班：其余未授权类别单据供应商全部抹除", all(not r.get("供应商") for r in masked))
check("所有拒绝单恒不带供应商", all(not r.get("供应商") for r in rejected))

snap = svc.cache_snapshot()
check("缓存快照带版本/事务序号", snap["缓存版本"] >= 1 and snap["事务序号"] >= 1)

print(f"\n结果：{ok} 通过，{fail} 失败")
sys.exit(1 if fail else 0)
