"""项目领用授权柜：领用授权、权限判定、实时预览与领料流转的业务规则。

口径要点（与需求逐条对应）：
1. 授权按「工程 × 施工路段 × 材料类别」授予某个领用单位「领用 / 只读」权限及额度。
2. 配置授权时实时预览会影响的任务与库存。
3. 授权结论随流程落到：材料台账（授权结论台账）、工程待办（project_todo）、
   车辆装载清单（vehicle_load）。
4. 越权领用直接拒绝；拒绝链路刻意不携带供应商信息。
5. 没有显式授权时，默认按「工程归属单位」继承领用权限；其他单位无权限。
6. 紧急抢险可申请临时例外，但只有「指挥」审批通过才生效。
7. 历史领用在申请时形成历史快照，项目转组（归属单位变更）不改写历史领用关系。
8. 权限缓存失效/重建与库存预占、额度占用在 store.transaction 同一把锁内原子完成，
   并发领料不会超过授权额度或可用库存。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.store import store

# ---- 表名与常量 ---------------------------------------------------------

GRANT_TABLE = "material_grant"
EXCEPTION_TABLE = "material_exception"
REQUISITION_TABLE = "material_requisition"
TODO_TABLE = "project_todo"
LOAD_TABLE = "vehicle_load"

MATERIAL_TABLE = "material"
PROJECT_TABLE = "project"

PERM_ISSUE = "领用"
PERM_READONLY = "只读"
PERM_NONE = "无权限"

GRANT_PERMISSIONS = (PERM_ISSUE, PERM_READONLY)
GRANT_STATES = ("生效", "停用")

# 默认继承工程归属时授予的额度上限（仅当没有显式授权时使用）。
INHERIT_QUOTA = 100
# 临时例外默认有效时长（小时）。
EXCEPTION_TTL_HOURS = 48

# 材料类别 -> 受其影响的任务模块（模块名, 路段/桥梁字段）。
CATEGORY_TASK_SOURCES: dict[str, list[tuple[str, str]]] = {
    "沥青类": [("pavement", "所属路段")],
    "水泥混凝土类": [("pavement", "所属路段")],
    "钢筋类": [("pavement", "所属路段"), ("bearing", "所属桥梁")],
    "交安材料类": [("traffic_facility", "所属路段")],
    "桥梁构件类": [("bearing", "所属桥梁"), ("expansion", "所属桥梁"), ("bridge_info", "桥梁名称")],
    "应急抢险类": [("flood", "影响路段")],
    "融雪防冻类": [("winter", "作业路段"), ("flood", "影响路段")],
}

TASK_MODULE_LABELS = {
    "pavement": "路面病害",
    "traffic_facility": "交安设施",
    "bearing": "桥梁支座",
    "expansion": "伸缩缝",
    "bridge_info": "桥梁档案",
    "flood": "防汛应急",
    "winter": "除雪防滑",
}

# 已经终结、不再占用资源的任务/单据状态。
DONE_TASK_STATUSES = {"已修复", "已完成", "已处置", "已评定", "正常"}
ISSUED_REQUISITION = "已发放"
RESERVED_REQUISITION = "已预占"
REJECTED_REQUISITION = "已拒绝"

_SENSITIVE_FIELDS = ("供应商",)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class GrantService:
    """授权柜全部读写逻辑。所有写操作都在 store.transaction 临界区内完成。"""

    def __init__(self) -> None:
        # 权限缓存：键 (工程编号, 施工路段, 材料类别, 领用单位) -> 判定结果。
        # 任何授权/例外/归属/库存写操作都会在同一把锁内清空它。
        self._permission_cache: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    # ---- 基础读取 --------------------------------------------------------

    def _get_project(self, project_code: str) -> dict[str, Any] | None:
        return store.find_by(PROJECT_TABLE, "工程编号", project_code)

    def _get_material_by_code(self, material_code: str) -> dict[str, Any] | None:
        return store.find_by(MATERIAL_TABLE, "材料编号", material_code)

    def _list_rows(self, module: str, **filters: Any) -> list[dict[str, Any]]:
        rows = store.rows(module)
        for field, value in filters.items():
            if value in (None, ""):
                continue
            rows = [row for row in rows if str(row.get(field, "")) == str(value)]
        return rows

    def list_grants(self, project_code: str | None = None, unit: str | None = None) -> list[dict[str, Any]]:
        return self._list_rows(GRANT_TABLE, 工程编号=project_code, 领用单位=unit)

    def list_exceptions(self, status: str | None = None) -> list[dict[str, Any]]:
        return self._list_rows(EXCEPTION_TABLE, 状态=status)

    def list_requisitions(
        self, project_code: str | None = None, unit: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self._list_rows(REQUISITION_TABLE, 工程编号=project_code, 领用单位=unit)
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return rows

    def list_todos(self, project_code: str | None = None) -> list[dict[str, Any]]:
        return self._list_rows(TODO_TABLE, 工程编号=project_code)

    def list_loads(self, project_code: str | None = None) -> list[dict[str, Any]]:
        return self._list_rows(LOAD_TABLE, 工程编号=project_code)

    # ---- 额度与库存口径（单一事实来源：领用单/库存） ----------------------

    @staticmethod
    def _stock(material: dict[str, Any]) -> tuple[int, int, int]:
        on_hand = _to_int(material.get("库存数量"))
        reserved = _to_int(material.get("预占数量"))
        return on_hand, reserved, max(on_hand - reserved, 0)

    def _grant_usage(self, grant_code: str) -> tuple[int, int]:
        """按授权编号统计已用额度 / 预占额度（紧急例外单据不计入常规授权额度）。"""
        used = reserved = 0
        for req in store.rows(REQUISITION_TABLE):
            if str(req.get("授权编号", "")) != grant_code:
                continue
            qty = _to_int(req.get("申请数量"))
            if req.get("状态") == ISSUED_REQUISITION:
                used += qty
            elif req.get("状态") == RESERVED_REQUISITION:
                reserved += qty
        return used, reserved

    def _exception_usage(self, exception_code: str) -> int:
        used = 0
        for req in store.rows(REQUISITION_TABLE):
            if str(req.get("紧急例外", "")) == exception_code and req.get("状态") in (
                RESERVED_REQUISITION,
                ISSUED_REQUISITION,
            ):
                used += _to_int(req.get("申请数量"))
        return used

    def _dimension_usage(
        self, project_code: str, section: str, category: str, unit: str
    ) -> tuple[int, int]:
        """继承授权（没有授权编号）时，按维度统计已用 / 预占。"""
        used = reserved = 0
        for req in store.rows(REQUISITION_TABLE):
            if (
                str(req.get("工程编号")) == project_code
                and str(req.get("施工路段")) == section
                and str(req.get("材料类别")) == category
                and str(req.get("领用单位")) == unit
                and not req.get("授权编号")
                and not req.get("紧急例外")
            ):
                qty = _to_int(req.get("申请数量"))
                if req.get("状态") == ISSUED_REQUISITION:
                    used += qty
                elif req.get("状态") == RESERVED_REQUISITION:
                    reserved += qty
        return used, reserved

    def _sync_grant_counters(self, grant: dict[str, Any]) -> None:
        used, reserved = self._grant_usage(str(grant.get("授权编号")))
        grant["已用额度"] = used
        grant["预占额度"] = reserved

    # ---- 权限判定（带缓存） ----------------------------------------------

    def invalidate_cache(self) -> None:
        """清空权限缓存。调用方必须已持有 store.transaction。"""
        self._permission_cache.clear()

    def _active_exception(
        self, project_code: str, section: str, category: str, unit: str
    ) -> dict[str, Any] | None:
        now = datetime.now()
        for exc in store.rows(EXCEPTION_TABLE):
            if exc.get("状态") != "已批准":
                continue
            if (
                str(exc.get("工程编号")) == project_code
                and str(exc.get("施工路段")) == section
                and str(exc.get("材料类别")) == category
                and str(exc.get("申请单位")) == unit
            ):
                try:
                    expires = datetime.strptime(str(exc.get("失效时间")), "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if now <= expires:
                    return exc
        return None

    def _explicit_grant(
        self, project_code: str, section: str, category: str, unit: str
    ) -> dict[str, Any] | None:
        for grant in store.rows(GRANT_TABLE):
            if (
                grant.get("状态") == "生效"
                and str(grant.get("工程编号")) == project_code
                and str(grant.get("施工路段")) == section
                and str(grant.get("材料类别")) == category
                and str(grant.get("领用单位")) == unit
            ):
                return grant
        return None

    def evaluate(
        self, project_code: str, section: str, category: str, unit: str
    ) -> dict[str, Any]:
        """判定一个单位在「工程×路段×类别」上的权限结论（读操作也走缓存）。"""
        key = (project_code, section, category, unit)

        def compute() -> dict[str, Any]:
            project = self._get_project(project_code)
            owner = str(project.get("归属单位", "")) if project else ""

            # 1) 指挥批准、仍在有效期内的临时例外优先，临时授予领用权限。
            exc = self._active_exception(project_code, section, category, unit)
            if exc is not None:
                quota = _to_int(exc.get("申请数量"))
                used = self._exception_usage(str(exc.get("例外编号")))
                return {
                    "权限": PERM_ISSUE,
                    "来源": "紧急抢险临时例外",
                    "授权编号": "",
                    "例外编号": exc.get("例外编号"),
                    "授权依据": f"{exc.get('例外编号')}（{exc.get('审批人')}批准的临时例外）",
                    "授权额度": quota,
                    "已用额度": used,
                    "预占额度": 0,
                    "剩余额度": max(quota - used, 0),
                    "失效时间": exc.get("失效时间"),
                    "是否继承": False,
                }

            # 2) 显式授权（手工授权柜记录，优先于继承）。
            grant = self._explicit_grant(project_code, section, category, unit)
            if grant is not None:
                self._sync_grant_counters(grant)
                used, reserved = self._grant_usage(str(grant.get("授权编号")))
                quota = _to_int(grant.get("授权额度")) if grant.get("权限") == PERM_ISSUE else 0
                return {
                    "权限": grant.get("权限"),
                    "来源": grant.get("来源"),
                    "授权编号": grant.get("授权编号"),
                    "例外编号": "",
                    "授权依据": f"{grant.get('授权编号')}（{grant.get('来源')}：{grant.get('权限')}）",
                    "授权额度": quota,
                    "已用额度": used,
                    "预占额度": reserved,
                    "剩余额度": max(quota - used - reserved, 0),
                    "失效时间": grant.get("到期日期"),
                    "是否继承": False,
                }

            # 3) 默认继承工程归属：只有归属单位在自己的工程上继承领用权限。
            if owner and unit == owner:
                used, reserved = self._dimension_usage(project_code, section, category, unit)
                return {
                    "权限": PERM_ISSUE,
                    "来源": "默认继承工程归属",
                    "授权编号": "",
                    "例外编号": "",
                    "授权依据": f"{unit} 是工程 {project_code} 的归属单位，默认继承领用权限",
                    "授权额度": INHERIT_QUOTA,
                    "已用额度": used,
                    "预占额度": reserved,
                    "剩余额度": max(INHERIT_QUOTA - used - reserved, 0),
                    "失效时间": "",
                    "是否继承": True,
                }

            # 4) 其余单位一律无权限。
            return {
                "权限": PERM_NONE,
                "来源": "无授权",
                "授权编号": "",
                "例外编号": "",
                "授权依据": "未在授权柜中授予该单位本工程/路段/材料类别的任何权限",
                "授权额度": 0,
                "已用额度": 0,
                "预占额度": 0,
                "剩余额度": 0,
                "失效时间": "",
                "是否继承": False,
            }

        with store.transaction:
            cached = self._permission_cache.get(key)
            if cached is None:
                cached = compute()
                self._permission_cache[key] = cached
            return dict(cached)

    # ---- 实时预览：会影响的任务与库存 ------------------------------------

    def _category_materials(self, category: str) -> list[dict[str, Any]]:
        return [m for m in store.rows(MATERIAL_TABLE) if str(m.get("材料类别")) == category]

    def preview(
        self,
        project_code: str,
        section: str,
        category: str,
        unit: str | None = None,
        quantity: int = 0,
    ) -> dict[str, Any]:
        """配置授权或申请领料前，实时预览会影响的任务与库存。"""
        project = self._get_project(project_code)
        if project is None:
            raise ValueError(f"工程 {project_code} 不存在")
        # 路段允许显式传入，缺省回落到工程登记的施工路段。
        section = section or str(project.get("施工路段", ""))

        affected_tasks: list[dict[str, Any]] = []
        for module, location_field in CATEGORY_TASK_SOURCES.get(category, []):
            for task in store.rows(module):
                if str(task.get(location_field, "")) != section:
                    continue
                code_field = next((f for f in task if f.endswith("编号")), "")
                affected_tasks.append({
                    "模块": TASK_MODULE_LABELS.get(module, module),
                    "编号": task.get(code_field),
                    "名称/位置": task.get(location_field),
                    "状态": task.get("status"),
                    "是否待办": task.get("status") not in DONE_TASK_STATUSES,
                    "关联工程": task.get("关联工程", ""),
                })

        inventory: list[dict[str, Any]] = []
        totals = {"库存数量": 0, "预占数量": 0, "可发数量": 0}
        for material in self._category_materials(category):
            on_hand, reserved, available = self._stock(material)
            totals["库存数量"] += on_hand
            totals["预占数量"] += reserved
            totals["可发数量"] += available
            inventory.append({
                "材料编号": material.get("材料编号"),
                "材料名称": material.get("材料名称"),
                "规格型号": material.get("规格型号"),
                "计量单位": material.get("计量单位"),
                "库存数量": on_hand,
                "预占数量": reserved,
                "可发数量": available,
                "存放地点": material.get("存放地点"),
                # 预览阶段同样不向前端暴露供应商。
            })

        decision = None
        if unit:
            decision = self.evaluate(project_code, section, category, unit)
        # 申请数量下的可行性提示。
        feasible = None
        if quantity > 0:
            quota_left = decision["剩余额度"] if decision else totals["可发数量"]
            feasible = {
                "申请数量": quantity,
                "额度是否充足": (quota_left >= quantity) if decision else True,
                "库存是否充足": totals["可发数量"] >= quantity,
                "结论": (
                    "可领用"
                    if decision
                    and decision["权限"] == PERM_ISSUE
                    and quota_left >= quantity
                    and totals["可发数量"] >= quantity
                    else "暂不可领用"
                ),
            }

        return {
            "工程编号": project_code,
            "工程名称": project.get("工程名称"),
            "施工路段": section,
            "材料类别": category,
            "归属单位": project.get("归属单位"),
            "受影响任务": affected_tasks,
            "受影响任务数": len(affected_tasks),
            "库存": inventory,
            "库存汇总": totals,
            "权限结论": decision,
            "可行性": feasible,
        }

    # ---- 授权柜配置 ------------------------------------------------------

    def upsert_grant(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """新增或调整一条授权。同维度同时只保留一条生效授权。"""
        project_code = str(values.get("工程编号", "")).strip()
        section = str(values.get("施工路段", "")).strip()
        category = str(values.get("材料类别", "")).strip()
        unit = str(values.get("领用单位", "")).strip()
        permission = str(values.get("权限", "")).strip()
        quota = _to_int(values.get("授权额度"))

        missing = [
            name
            for name, value in (
                ("工程编号", project_code),
                ("施工路段", section),
                ("材料类别", category),
                ("领用单位", unit),
                ("权限", permission),
            )
            if not value
        ]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        if permission not in GRANT_PERMISSIONS:
            return None, "权限只能是「领用」或「只读」"
        if permission == PERM_ISSUE and quota <= 0:
            return None, "领用权限必须授予大于 0 的授权额度"
        project = self._get_project(project_code)
        if project is None:
            return None, f"工程 {project_code} 不存在"
        if section != str(project.get("施工路段", "")):
            return None, f"施工路段「{section}」与工程登记的「{project.get('施工路段')}」不一致"

        with store.transaction:
            existing = self._explicit_grant(project_code, section, category, unit)
            if existing is not None:
                existing["权限"] = permission
                existing["授权额度"] = quota if permission == PERM_ISSUE else 0
                existing["来源"] = "手工授权"
                existing["授权人"] = str(values.get("授权人", "")).strip() or existing.get("授权人")
                existing["到期日期"] = str(values.get("到期日期", "")).strip()
                existing["备注"] = str(values.get("备注", "")).strip()
                self._sync_grant_counters(existing)
                self.invalidate_cache()
                self._append_todo(
                    project_code,
                    f"授权 {existing.get('授权编号')} 已调整为{permission}，额度 {existing.get('授权额度')}",
                    "授权调整",
                    str(existing.get("授权编号")),
                )
                return existing, "授权已更新"

            grant_id = store.next_id(GRANT_TABLE)
            grant = {
                "id": grant_id,
                "授权编号": f"GRANT-{grant_id:04d}",
                "工程编号": project_code,
                "施工路段": section,
                "材料类别": category,
                "领用单位": unit,
                "权限": permission,
                "授权额度": quota if permission == PERM_ISSUE else 0,
                "已用额度": 0,
                "预占额度": 0,
                "来源": "手工授权",
                "状态": "生效",
                "授权人": str(values.get("授权人", "")).strip() or "值班管理员",
                "生效日期": _today(),
                "到期日期": str(values.get("到期日期", "")).strip(),
                "备注": str(values.get("备注", "")).strip(),
            }
            store.rows(GRANT_TABLE).append(grant)
            self.invalidate_cache()
            self._append_todo(
                project_code,
                f"新增授权 {grant['授权编号']}：{unit} 对 {category} 拥有{permission}权限",
                "授权配置",
                grant["授权编号"],
            )
            return grant, "授权已写入授权柜"

    def set_grant_state(self, grant_id: int, state: str) -> tuple[dict[str, Any] | None, str]:
        if state not in GRANT_STATES:
            return None, "授权状态只能是「生效」或「停用」"
        grant = store.find(GRANT_TABLE, grant_id)
        if grant is None:
            return None, f"授权 {grant_id} 不存在"
        with store.transaction:
            grant["状态"] = state
            self.invalidate_cache()
        return grant, f"授权已{state}"

    # ---- 紧急抢险临时例外 ------------------------------------------------

    def apply_exception(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        project_code = str(values.get("工程编号", "")).strip()
        section = str(values.get("施工路段", "")).strip()
        category = str(values.get("材料类别", "")).strip()
        unit = str(values.get("申请单位", "")).strip()
        quantity = _to_int(values.get("申请数量"))
        reason = str(values.get("事由", "")).strip()

        missing = [
            name
            for name, value in (
                ("工程编号", project_code),
                ("施工路段", section),
                ("材料类别", category),
                ("申请单位", unit),
                ("事由", reason),
            )
            if not value
        ]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        if quantity <= 0:
            return None, "临时例外必须填写大于 0 的申请数量"
        if self._get_project(project_code) is None:
            return None, f"工程 {project_code} 不存在"

        with store.transaction:
            exc_id = store.next_id(EXCEPTION_TABLE)
            exc = {
                "id": exc_id,
                "例外编号": f"EXC-{exc_id:04d}",
                "工程编号": project_code,
                "施工路段": section,
                "材料类别": category,
                "申请单位": unit,
                "申请数量": quantity,
                "事由": reason,
                "状态": "待指挥审批",
                "审批人": "",
                "审批职务": "",
                "审批时间": "",
                "生效时间": "",
                "失效时间": "",
                "审批意见": "",
            }
            store.rows(EXCEPTION_TABLE).append(exc)
            self._append_todo(
                project_code,
                f"{exc['例外编号']} 紧急抢险临时例外（{category} {quantity}）待指挥审批",
                "临时例外",
                exc["例外编号"],
            )
            return exc, "临时例外申请已提交，等待指挥审批"

    def approve_exception(
        self, exception_id: int, *, approver: str, approver_title: str, opinion: str, approved: bool
    ) -> tuple[dict[str, Any] | None, str]:
        """以指挥审批为准：只有职务包含「指挥」的审批人同意，例外才生效。"""
        exc = store.find(EXCEPTION_TABLE, exception_id)
        if exc is None:
            return None, f"临时例外 {exception_id} 不存在"
        if exc.get("状态") != "待指挥审批":
            return None, f"临时例外当前为「{exc.get('状态')}」，不可重复审批"

        with store.transaction:
            exc["审批人"] = approver.strip() or "值班指挥"
            exc["审批职务"] = approver_title.strip()
            exc["审批意见"] = opinion.strip()
            exc["审批时间"] = _now()
            if not approved:
                exc["状态"] = "已驳回"
                self.invalidate_cache()
                self._append_todo(
                    str(exc.get("工程编号")),
                    f"{exc.get('例外编号')} 临时例外被{exc['审批人']}驳回",
                    "临时例外",
                    str(exc.get("例外编号")),
                )
                return exc, "临时例外已驳回，领用仍按原权限执行"

            # 关键闸门：以指挥审批为准，非指挥身份不能激活临时例外。
            if "指挥" not in approver_title:
                exc["状态"] = "待指挥审批"
                return None, "仅现场总指挥可批准临时例外，已保留待指挥审批状态"

            start = datetime.now()
            exc["状态"] = "已批准"
            exc["生效时间"] = start.strftime("%Y-%m-%d %H:%M")
            exc["失效时间"] = (start + timedelta(hours=EXCEPTION_TTL_HOURS)).strftime("%Y-%m-%d %H:%M")
            self.invalidate_cache()
            self._append_todo(
                str(exc.get("工程编号")),
                f"{exc.get('例外编号')} 临时例外经{exc['审批人']}（{approver_title}）批准，"
                f"有效期至 {exc['失效时间']}",
                "临时例外",
                str(exc.get("例外编号")),
            )
            return exc, f"临时例外已生效，{exc['失效时间']} 前可在额度内领用"

    # ---- 领料：原子判定 + 预占 + 发放 ------------------------------------

    def _append_ledger(self, material: dict[str, Any], entry: dict[str, Any]) -> None:
        material.setdefault("授权结论台账", []).append(entry)

    def _append_todo(self, project_code: str, content: str, source: str, ref_code: str) -> dict[str, Any]:
        todo = {
            "id": store.next_id(TODO_TABLE),
            "工程编号": project_code,
            "事项": content,
            "来源": source,
            "关联单号": ref_code,
            "状态": "待处理",
            "时间": _now(),
        }
        store.rows(TODO_TABLE).append(todo)
        return todo

    def _resolve_todo(self, ref_code: str) -> None:
        for todo in store.rows(TODO_TABLE):
            if str(todo.get("关联单号")) == str(ref_code) and todo.get("状态") == "待处理":
                todo["状态"] = "已处理"
                todo["处理时间"] = _now()

    def _safe_material_view(self, material: dict[str, Any], can_see_supplier: bool) -> dict[str, Any]:
        # 供应商是敏感信息：有领用权限时原样返回，其余情况统一遮蔽。
        view = dict(material)
        if not can_see_supplier:
            view["供应商"] = "***（无权查看）"
        on_hand, reserved, available = self._stock(material)
        view["库存数量"] = on_hand
        view["预占数量"] = reserved
        view["可发数量"] = available
        return view

    def scoped_materials(
        self, project_code: str | None, unit: str | None, category: str | None
    ) -> list[dict[str, Any]]:
        """按领用单位权限查看材料：只读/无权限单位拿不到供应商等敏感信息。"""
        result: list[dict[str, Any]] = []
        for material in store.rows(MATERIAL_TABLE):
            mat_category = str(material.get("材料类别"))
            if category and mat_category != category:
                continue
            decision = None
            if project_code and unit:
                section = str(self._get_project(project_code).get("施工路段", "")) if self._get_project(project_code) else ""
                decision = self.evaluate(project_code, section, mat_category, unit)
            can_see = decision is None or decision["权限"] == PERM_ISSUE
            view = self._safe_material_view(material, can_see)
            if decision is not None:
                view["我的权限"] = decision["权限"]
                view["权限依据"] = decision["授权依据"]
            result.append(view)
        return result

    def apply_requisition(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """申请领料：权限与库存原子判定，通过即预占。

        返回 (单据, 说明, 是否放行)。拒绝时单据中绝不包含供应商信息。
        """
        project_code = str(values.get("工程编号", "")).strip()
        material_code = str(values.get("材料编号", "")).strip()
        unit = str(values.get("领用单位", "")).strip()
        person = str(values.get("领用人", "")).strip() or unit
        quantity = _to_int(values.get("申请数量"))
        purpose = str(values.get("用途", "")).strip()
        vehicle_plate = str(values.get("车牌号", "")).strip()

        if not project_code or not material_code or not unit:
            return None, "缺少必填字段：工程编号、材料编号、领用单位", False
        if quantity <= 0:
            return None, "申请数量必须大于 0", False
        project = self._get_project(project_code)
        if project is None:
            return None, f"工程 {project_code} 不存在", False
        material = self._get_material_by_code(material_code)
        if material is None:
            # 不向越权/异常请求回传更多材料细节。
            return None, f"材料 {material_code} 不存在或已归档", False

        section = str(project.get("施工路段", ""))
        category = str(material.get("材料类别", ""))

        # 整个“权限缓存判定 + 额度占用 + 库存预占 + 落台账/待办/装载清单”在同一临界区。
        with store.transaction:
            decision = self.evaluate(project_code, section, category, unit)

            requisition_id = store.next_id(REQUISITION_TABLE)
            snapshot = {
                "工程编号": project_code,
                "归属单位": project.get("归属单位"),
                "施工路段": section,
                "材料类别": category,
                "权限": decision["权限"],
                "授权额度": decision["授权额度"],
                "授权依据": decision["授权依据"],
                "领用单位": unit,
                "快照时间": _now(),
            }
            base = {
                "id": requisition_id,
                "领用单号": f"REQU-{requisition_id:04d}",
                "工程编号": project_code,
                "施工路段": section,
                "材料编号": material_code,
                "材料类别": category,
                "领用单位": unit,
                "领用人": person,
                "申请数量": quantity,
                "计量单位": material.get("计量单位", ""),
                "用途": purpose,
                "授权编号": decision["授权编号"],
                "紧急例外": decision["例外编号"],
                "授权依据": decision["授权依据"],
                "申请时间": _now(),
                "处理时间": "",
                "历史快照": snapshot,
            }

            # 越权 / 只读：拒绝，且不暴露供应商。
            if decision["权限"] != PERM_ISSUE:
                reason = (
                    f"{unit} 在工程 {project_code} / {section} / {category} 上"
                    f"仅有「{decision['权限']}」权限，领用已拒绝"
                )
                base.update({
                    "状态": REJECTED_REQUISITION,
                    "授权结论": "拒绝",
                    "拒绝原因": reason,
                    "材料名称": "",  # 刻意留空，避免借拒绝回传任何材料敏感字段。
                })
                store.rows(REQUISITION_TABLE).append(base)
                self._append_ledger(material, {
                    "领用单号": base["领用单号"],
                    "工程编号": project_code,
                    "施工路段": section,
                    "材料类别": category,
                    "领用单位": unit,
                    "数量": quantity,
                    "授权结论": "拒绝",
                    "授权依据": decision["授权依据"],
                    "状态": REJECTED_REQUISITION,
                    "时间": base["申请时间"],
                })
                self._append_todo(
                    project_code,
                    f"越权领用拦截：{base['领用单号']}（{unit} 申请 {category} {quantity}）已拒绝，待核查",
                    "越权拦截",
                    base["领用单号"],
                )
                self.invalidate_cache()
                return base, reason, False

            # 额度校验（缓存刚在锁内重建，结论是最新的）。
            if decision["剩余额度"] < quantity:
                reason = (
                    f"授权额度不足：{decision['授权依据']} 剩余 {decision['剩余额度']}，"
                    f"本次申请 {quantity}，已拒绝"
                )
                base.update({"状态": REJECTED_REQUISITION, "授权结论": "拒绝", "拒绝原因": reason})
                store.rows(REQUISITION_TABLE).append(base)
                self._append_todo(project_code, f"额度拦截：{base['领用单号']} {reason}", "额度拦截", base["领用单号"])
                self.invalidate_cache()
                return base, reason, False

            # 库存校验与预占（与权限缓存在同一临界区原子完成）。
            on_hand, reserved, available = self._stock(material)
            if material.get("status") == "不合格":
                reason = f"材料 {material_code} 判定不合格，禁止领用"
                base.update({"状态": REJECTED_REQUISITION, "授权结论": "拒绝", "拒绝原因": reason})
                store.rows(REQUISITION_TABLE).append(base)
                return base, reason, False
            if available < quantity:
                reason = f"可发库存不足：{category} 当前可发 {available}（库存 {on_hand}、已预占 {reserved}），申请 {quantity}"
                base.update({"状态": REJECTED_REQUISITION, "授权结论": "拒绝", "拒绝原因": reason})
                store.rows(REQUISITION_TABLE).append(base)
                self._append_todo(project_code, f"库存拦截：{base['领用单号']} {reason}", "库存拦截", base["领用单号"])
                return base, reason, False

            # 放行：原子预占库存与额度。
            material["预占数量"] = reserved + quantity
            base.update({
                "状态": RESERVED_REQUISITION,
                "授权结论": "预占待发",
                "材料名称": material.get("材料名称"),
            })
            store.rows(REQUISITION_TABLE).append(base)

            # 同步授权柜已用/预占计数（常规授权）。
            if decision["授权编号"]:
                grant = self._explicit_grant(project_code, section, category, unit)
                if grant is not None:
                    self._sync_grant_counters(grant)

            self._append_ledger(material, {
                "领用单号": base["领用单号"],
                "工程编号": project_code,
                "施工路段": section,
                "材料类别": category,
                "领用单位": unit,
                "数量": quantity,
                "授权结论": "预占待发",
                "授权依据": decision["授权依据"],
                "状态": RESERVED_REQUISITION,
                "时间": base["申请时间"],
            })
            self._append_todo(
                project_code,
                f"{base['领用单号']} {material.get('材料名称')} {quantity}{material.get('计量单位')} 已预占待发",
                "领用预占",
                base["领用单号"],
            )
            load_id = store.next_id(LOAD_TABLE)
            load = {
                "id": load_id,
                "单号": f"LOAD-{load_id:04d}",
                "领用单号": base["领用单号"],
                "车牌号": vehicle_plate or "待派车",
                "车辆类型": str(values.get("车辆类型", "")).strip() or "养护车辆",
                "工程编号": project_code,
                "施工路段": section,
                "材料编号": material_code,
                "材料名称": material.get("材料名称"),
                "数量": quantity,
                "计量单位": material.get("计量单位"),
                "状态": "待发运",
                "时间": _now(),
            }
            store.rows(LOAD_TABLE).append(load)
            self.invalidate_cache()

            # 回传不带供应商字段。
            base["库存预占后"] = {"库存数量": on_hand, "预占数量": reserved + quantity, "可发数量": available - quantity}
            return base, f"领用已授权并预占 {quantity}{material.get('计量单位')}，待发料确认", True

    def issue_requisition(self, requisition_id: int, vehicle_plate: str = "") -> tuple[dict[str, Any] | None, str]:
        """确认发料：预占转实发，扣减库存并收敛额度/待办/装载清单。"""
        req = store.find(REQUISITION_TABLE, requisition_id)
        if req is None:
            return None, f"领用单 {requisition_id} 不存在"
        if req.get("状态") != RESERVED_REQUISITION:
            return None, f"领用单当前为「{req.get('状态')}」，仅「{RESERVED_REQUISITION}」可发料"
        material = self._get_material_by_code(str(req.get("材料编号")))
        if material is None:
            return None, "对应材料已不存在"

        with store.transaction:
            qty = _to_int(req.get("申请数量"))
            on_hand, reserved, _ = self._stock(material)
            if reserved < qty:
                return None, "预占数据异常：预占数量小于本单数量，已暂停发料"
            material["预占数量"] = reserved - qty
            material["库存数量"] = on_hand - qty
            material["status"] = "在库" if on_hand - qty > 0 else "已领用"
            req["状态"] = ISSUED_REQUISITION
            req["授权结论"] = "放行"
            req["处理时间"] = _now()

            # 台账结论由预占转发放。
            for entry in material.get("授权结论台账", []):
                if entry.get("领用单号") == req.get("领用单号"):
                    entry["授权结论"] = "放行"
                    entry["状态"] = ISSUED_REQUISITION
                    entry["时间"] = req["处理时间"]

            grant_code = str(req.get("授权编号", ""))
            if grant_code:
                grant = store.find_by(GRANT_TABLE, "授权编号", grant_code)
                if grant is not None:
                    self._sync_grant_counters(grant)

            for load in store.rows(LOAD_TABLE):
                if str(load.get("领用单号")) == str(req.get("领用单号")) and load.get("状态") == "待发运":
                    if vehicle_plate:
                        load["车牌号"] = vehicle_plate
                    load["状态"] = "已发运"
                    load["时间"] = _now()
            self._resolve_todo(str(req.get("领用单号")))
            self.invalidate_cache()
            return req, f"已发料 {qty}{req.get('计量单位')}，库存与车辆装载清单已更新"

    # ---- 项目转组：历史领用关系不变 --------------------------------------

    def transfer_project(
        self, project_id: int, new_owner: str, *, operator: str = ""
    ) -> tuple[dict[str, Any] | None, str, int]:
        """工程转组：更新归属单位（影响后续继承），但冻结历史领用快照。

        返回 (工程, 说明, 保持不变的历史领用单数)。
        """
        project = store.find(PROJECT_TABLE, project_id)
        if project is None:
            return None, f"工程 {project_id} 不存在", 0
        new_owner = new_owner.strip()
        if not new_owner:
            return None, "必须指定新的归属单位", 0
        old_owner = str(project.get("归属单位", ""))
        if new_owner == old_owner:
            return project, "新归属单位与当前一致，无需转组", 0

        with store.transaction:
            project["归属单位"] = new_owner
            project["转组记录"] = project.get("转组记录", [])
            project["转组记录"].append({
                "原归属单位": old_owner,
                "新归属单位": new_owner,
                "操作人": operator or "值班管理员",
                "时间": _now(),
            })
            frozen = [
                req
                for req in store.rows(REQUISITION_TABLE)
                if str(req.get("工程编号")) == str(project.get("工程编号"))
            ]
            # 历史领用单的快照、授权依据一律不改写。
            self._append_todo(
                str(project.get("工程编号")),
                f"工程转组：归属单位由「{old_owner}」变更为「{new_owner}」；"
                f"{len(frozen)} 单历史领用关系保持原值",
                "项目转组",
                str(project.get("工程编号")),
            )
            self.invalidate_cache()
            return project, f"已转组至{new_owner}，历史领用关系不变", len(frozen)


grant_service = GrantService()
