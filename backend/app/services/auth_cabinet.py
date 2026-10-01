"""项目领用授权柜业务规则。

维度：工程 × 施工路段 × 材料类别，给班组授予「领用 / 只读」权限。

权限优先级（高 → 低）：
1. 紧急例外：经指挥审批后生成、带有效期的临时授权；
2. 直接授权：授权柜里显式配置的规则（显式只读会压过默认继承的领用）；
3. 工程归属继承：班组归属单位与工程承建单位一致时，默认按继承额度领用；
4. 无权限：任何领料请求一律拒绝，且错误信息与库存视图都不返回供应商。

并发口径：领料校验、额度预占、库存预占、权限缓存版本推进全部在同一把锁内
完成，保证「权限缓存与库存预占原子更新，并发领料不能超授权额度」。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from app.store import store

# 工程归属继承时，每个「班组 × 工程 × 材料类别」默认可领用的额度
DEFAULT_INHERIT_QUOTA = 100
# 紧急例外默认有效期（小时），到期自动失效
DEFAULT_EXCEPTION_HOURS = 24

GRANT_TABLE = "auth_grant"
EXCEPTION_TABLE = "auth_exception"
STOCK_TABLE = "material_stock"
REQUISITION_TABLE = "material_requisition"
TODO_TABLE = "project_todo"
LOAD_TABLE = "vehicle_load"
LEDGER_TABLE = "material"

# 敏感字段：越权场景下必须从任何返回内容里剔除
SENSITIVE_FIELDS = ("供应商",)


class AuthorizationError(Exception):
    """业务拒绝原因。reason 面向用户，禁止包含供应商等敏感信息。"""

    def __init__(self, reason: str, *, code: str = "forbidden", http_status: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.http_status = http_status


class AuthCabinetService:
    def __init__(self) -> None:
        # 一把锁保护：授权规则、权限缓存、额度预占、库存预占
        self._lock = threading.RLock()
        self._cache_version = 0
        # 每次预占/出库/取消都 +1，表示缓存与库存的原子事务序号
        self._cache_txn = 0
        # 继承权限的预占/领用计数：(班组id, 工程id, 材料类别) -> 计数
        self._inherit_usage: dict[tuple[int, int, str], dict[str, int]] = {}
        # 权限缓存：(班组id, 工程id, 材料类别) -> {"kind": ..., "grant": 规则行}
        self._cache: dict[tuple[int, int, str], dict[str, Any]] = {}
        self.rebuild_cache()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @staticmethod
    def _now() -> datetime:
        return datetime.now()

    @staticmethod
    def _fmt(moment: datetime) -> str:
        return moment.strftime("%Y-%m-%d %H:%M:%S")

    def _strip_sensitive(self, row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key not in SENSITIVE_FIELDS}

    def _find_project(self, project_id: int) -> dict[str, Any]:
        project = store.find("project", project_id)
        if project is None:
            raise AuthorizationError(f"工程 {project_id} 不存在或已归档", code="not_found", http_status=404)
        return project

    def _find_team(self, team_id: int) -> dict[str, Any]:
        team = store.find("team", team_id)
        if team is None:
            raise AuthorizationError(f"班组 {team_id} 不存在", code="not_found", http_status=404)
        return team

    def _active_grants(self) -> list[dict[str, Any]]:
        grants: list[dict[str, Any]] = []
        for grant in store.rows(GRANT_TABLE):
            if grant.get("状态") != "生效中":
                continue
            expires = str(grant.get("有效期止") or "").strip()
            if expires:
                try:
                    if datetime.strptime(expires, "%Y-%m-%d %H:%M:%S") < self._now():
                        continue  # 紧急例外到期：静默失效，不参与授权
                except ValueError:
                    continue
            grants.append(grant)
        return grants

    def _add_todo(
        self,
        project: dict[str, Any],
        title: str,
        *,
        todo_type: str,
        ref: str,
        conclusion: str,
    ) -> dict[str, Any]:
        row = {
            "id": store.next_id(TODO_TABLE),
            "status": "待处理", "pending": True, "abnormal": False,
            "工程id": project["id"],
            "工程编号": project.get("工程编号"),
            "工程名称": project.get("工程名称"),
            "标题": title,
            "类型": todo_type,
            "关联单据": ref,
            "授权结论": conclusion,
            "状态值": "待处理",
            "创建时间": self._fmt(self._now()),
            "处理时间": "",
            "处理人": "",
        }
        store.rows(TODO_TABLE).append(row)
        return row

    def _stock_rows(self, category: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(STOCK_TABLE)
        if category:
            rows = [row for row in rows if row.get("材料类别") == category]
        return rows

    def _available_stock(self, stock: dict[str, Any]) -> int:
        return int(stock.get("库存总量", 0)) - int(stock.get("已预占", 0)) - int(stock.get("已领用", 0))

    # ------------------------------------------------------------------
    # 权限缓存
    # ------------------------------------------------------------------
    def rebuild_cache(self, *, _bump_version: bool = True) -> None:
        """按「例外 → 直接授权 → 工程归属继承」重建权限缓存。

        结构变化（授权规则、例外审批、工程转组）后必须重建；
        预占类操作不改结构，只推进事务序号并刷新计数行。
        本方法是可重入的：持锁路径直接调用，独立路径自行加锁。
        """
        with self._lock:
            self._rebuild_locked(_bump_version=_bump_version)

    def _rebuild_locked(self, *, _bump_version: bool = True) -> None:
        cache: dict[tuple[int, int, str], dict[str, Any]] = {}
        teams = store.rows("team")
        team_by_name = {str(team.get("班组名称")): team for team in teams}
        projects = store.rows("project")
        categories = {str(row.get("材料类别")) for row in self._stock_rows()}

        for grant in self._active_grants():
            team = team_by_name.get(str(grant.get("授权对象")))
            if team is None:
                continue
            key = (int(team["id"]), int(grant["工程id"]), str(grant["材料类别"]))
            entry = cache.get(key)
            # 紧急例外优先级高于直接授权
            if entry is None or (entry["kind"] != "exception" and grant.get("来源") == "紧急例外"):
                cache[key] = {
                    "kind": "exception" if grant.get("来源") == "紧急例外" else "direct",
                    "grant": grant,
                }
            categories.add(str(grant.get("材料类别")))

        # 默认继承：班组归属单位 == 工程承建单位
        for team in teams:
            for project in projects:
                if team.get("归属单位") != project.get("承建单位"):
                    continue
                for category in categories:
                    key = (int(team["id"]), int(project["id"]), category)
                    cache.setdefault(key, {"kind": "inherit", "grant": None})

        self._cache = cache
        if _bump_version:
            self._cache_version += 1

    def cache_snapshot(self, *, project_id: int | None = None, team_id: int | None = None) -> dict[str, Any]:
        with self._lock:
            entries: list[dict[str, Any]] = []
            projects = {int(p["id"]): p for p in store.rows("project")}
            teams = {int(t["id"]): t for t in store.rows("team")}
            for (tid, pid, category), entry in self._cache.items():
                if project_id is not None and pid != project_id:
                    continue
                if team_id is not None and tid != team_id:
                    continue
                decision = self._resolve_locked(teams[tid], projects[pid], category)
                entries.append(decision)
            return {
                "缓存版本": self._cache_version,
                "事务序号": self._cache_txn,
                "刷新时间": self._fmt(self._now()),
                "条目数": len(entries),
                "items": entries,
            }

    # ------------------------------------------------------------------
    # 授权判定
    # ------------------------------------------------------------------
    def _resolve_locked(
        self,
        team: dict[str, Any],
        project: dict[str, Any],
        category: str,
    ) -> dict[str, Any]:
        """计算单个「班组 × 工程 × 类别」的有效权限（调用方持锁）。"""
        tid, pid = int(team["id"]), int(project["id"])
        entry = self._cache.get((tid, pid, category))
        base = {
            "班组id": tid,
            "授权对象": team.get("班组名称"),
            "工程id": pid,
            "工程编号": project.get("工程编号"),
            "工程名称": project.get("工程名称"),
            "施工路段": project.get("施工路段"),
            "材料类别": category,
            "缓存版本": self._cache_version,
        }
        if entry is not None and entry["kind"] in ("direct", "exception"):
            grant = entry["grant"]
            reserved = int(grant.get("已预占", 0))
            used = int(grant.get("已领用", 0))
            quota = int(grant.get("授权额度", 0))
            source = "紧急例外" if entry["kind"] == "exception" else "直接授权"
            return {
                **base,
                "权限": str(grant.get("权限")),
                "来源": source,
                "授权额度": quota,
                "已预占": reserved,
                "已领用": used,
                "剩余额度": quota - reserved - used,
                "关联授权": int(grant.get("id", 0)),
                "关联例外": grant.get("关联例外"),
                "有效期止": grant.get("有效期止") or "",
                "说明": f"{source}：{grant.get('授权人')} 授权"
                        + (f"，有效期至 {grant['有效期止']}" if grant.get("有效期止") else ""),
            }
        if entry is not None and entry["kind"] == "inherit":
            usage = self._inherit_usage.setdefault((tid, pid, category), {"已预占": 0, "已领用": 0})
            reserved, used = usage["已预占"], usage["已领用"]
            return {
                **base,
                "权限": "领用",
                "来源": "工程归属继承",
                "授权额度": DEFAULT_INHERIT_QUOTA,
                "已预占": reserved,
                "已领用": used,
                "剩余额度": DEFAULT_INHERIT_QUOTA - reserved - used,
                "关联授权": None,
                "关联例外": None,
                "有效期止": "",
                "说明": f"默认继承工程归属（{team.get('归属单位')} = 工程承建单位）",
            }
        return {
            **base,
            "权限": "无权限",
            "来源": "—",
            "授权额度": 0,
            "已预占": 0,
            "已领用": 0,
            "剩余额度": 0,
            "关联授权": None,
            "关联例外": None,
            "有效期止": "",
            "说明": "无任何直接授权或工程归属继承关系，领料将被拒绝",
        }

    def resolve_effective(self, team_id: int, project_id: int, category: str) -> dict[str, Any]:
        with self._lock:
            return self._resolve_locked(self._find_team(team_id), self._find_project(project_id), category)

    # ------------------------------------------------------------------
    # 元数据 / 列表
    # ------------------------------------------------------------------
    def meta(self) -> dict[str, Any]:
        with self._lock:
            return {
                "projects": [
                    {"id": p["id"], "工程编号": p.get("工程编号"), "工程名称": p.get("工程名称"),
                     "施工路段": p.get("施工路段"), "承建单位": p.get("承建单位"),
                     "状态": p.get("status")}
                    for p in store.rows("project")
                ],
                "teams": [dict(team) for team in store.rows("team")],
                "categories": sorted({str(row.get("材料类别")) for row in self._stock_rows()}),
                "vehicles": [
                    {"id": v["id"], "车辆编号": v.get("车辆编号"), "车牌号": v.get("车牌号"),
                     "所属单位": v.get("所属单位")}
                    for v in store.rows("vehicle")
                ],
            }

    def list_rows(
        self,
        table: str,
        *,
        viewer_team_id: int | None = None,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = store.rows(table)
            for key, value in filters.items():
                if value is None or value == "":
                    continue
                rows = [row for row in rows if row.get(key) == value]
            if viewer_team_id is None:
                return rows
            # 以班组视角查看时，未获「领用」授权的类别必须脱敏供应商
            team = store.find("team", int(viewer_team_id))
            if team is None or table != REQUISITION_TABLE:
                return rows
            result: list[dict[str, Any]] = []
            projects = {int(p["id"]): p for p in store.rows("project")}
            for row in rows:
                project = projects.get(int(row.get("工程id", 0)))
                if project is None:
                    result.append(row)
                    continue
                decision = self._resolve_locked(team, project, str(row.get("材料类别")))
                result.append(row if decision["权限"] == "领用" else self._strip_sensitive(row))
            return result

    # ------------------------------------------------------------------
    # 实时预览：授权会影响的任务与库存
    # ------------------------------------------------------------------
    def preview(self, project_id: int, category: str, team_id: int) -> dict[str, Any]:
        with self._lock:
            project = self._find_project(project_id)
            team = self._find_team(team_id)
            decision = self._resolve_locked(team, project, category)
            allowed = decision["权限"] != "无权限"
            stock = self._stock_rows(category)
            stock_view = [
                (row if allowed else self._strip_sensitive(row))
                for row in stock
            ]
            for row in stock_view:
                row["可用库存"] = self._available_stock(row)
            return {
                "decision": decision,
                "stock": stock_view,
                "供应商可见": allowed,
                "tasks": [
                    row for row in store.rows(TODO_TABLE)
                    if int(row.get("工程id", 0)) == project_id
                ],
                "requisitions": [
                    (row if allowed else self._strip_sensitive(row))
                    for row in store.rows(REQUISITION_TABLE)
                    if int(row.get("工程id", 0)) == project_id and row.get("材料类别") == category
                ],
                "loads": [
                    row for row in store.rows(LOAD_TABLE)
                    if int(row.get("工程id", 0)) == project_id and row.get("材料类别") == category
                ],
                "grants": [
                    row for row in store.rows(GRANT_TABLE)
                    if int(row.get("工程id", 0)) == project_id and row.get("材料类别") == category
                ],
            }

    # ------------------------------------------------------------------
    # 直接授权
    # ------------------------------------------------------------------
    def create_grant(self, values: dict[str, Any]) -> dict[str, Any]:
        project_id = int(values.get("工程id") or 0)
        team_id = int(values.get("班组id") or 0)
        category = str(values.get("材料类别") or "").strip()
        permission = str(values.get("权限") or "").strip()
        quota = int(values.get("授权额度") or 0)
        with self._lock:
            project = self._find_project(project_id)
            team = self._find_team(team_id)
            if not category:
                raise AuthorizationError("材料类别不能为空", code="invalid", http_status=400)
            if permission not in ("领用", "只读"):
                raise AuthorizationError("权限只能是「领用」或「只读」", code="invalid", http_status=400)
            if permission == "领用" and quota <= 0:
                raise AuthorizationError("领用授权必须给出大于 0 的授权额度", code="invalid", http_status=400)
            if permission == "只读":
                quota = 0
            for grant in store.rows(GRANT_TABLE):
                if (int(grant.get("工程id", 0)) == project_id
                        and grant.get("授权对象") == team.get("班组名称")
                        and grant.get("材料类别") == category
                        and grant.get("状态") == "生效中"):
                    raise AuthorizationError(
                        f"{team['班组名称']} 在该工程/类别下已有生效授权，请先停用旧授权",
                        code="conflict", http_status=409,
                    )
            grant = {
                "id": store.next_id(GRANT_TABLE),
                "status": "生效中", "pending": False, "abnormal": False,
                "工程id": project_id,
                "工程编号": project.get("工程编号"),
                "工程名称": project.get("工程名称"),
                "施工路段": project.get("施工路段"),
                "材料类别": category,
                "授权对象": team.get("班组名称"),
                "权限": permission,
                "授权额度": quota,
                "已预占": 0,
                "已领用": 0,
                "来源": "直接授权",
                "状态": "生效中",
                "授权人": str(values.get("授权人") or "材料科"),
                "授权时间": self._fmt(self._now()),
                "有效期止": "",
                "关联例外": None,
                "版本": 1,
            }
            store.rows(GRANT_TABLE).append(grant)
            self._rebuild_locked()
            self._add_todo(
                project,
                f"授权生效：{project.get('施工路段')} / {category} → {team['班组名称']} "
                f"{permission}" + (f"（额度{quota}）" if permission == "领用" else ""),
                todo_type="授权知会",
                ref=f"授权规则#{grant['id']}",
                conclusion="允许领用" if permission == "领用" else "只读",
            )
            return grant

    def revoke_grant(self, grant_id: int, operator: str = "") -> dict[str, Any]:
        with self._lock:
            grant = store.find(GRANT_TABLE, grant_id)
            if grant is None or grant.get("状态") != "生效中":
                raise AuthorizationError("生效中的授权规则不存在，无法停用", code="not_found", http_status=404)
            if int(grant.get("已预占", 0)) > 0:
                raise AuthorizationError(
                    f"该授权尚有 {grant['已预占']} 个单位预占中，请先处理对应领料单",
                    code="conflict", http_status=409,
                )
            grant["状态"] = "已停用"
            grant["status"] = "已停用"
            project = store.find("project", int(grant["工程id"]))
            self._rebuild_locked()
            if project is not None:
                self._add_todo(
                    project,
                    f"授权停用：{grant['施工路段']} / {grant['材料类别']} → {grant['授权对象']}",
                    todo_type="授权变更", ref=f"授权规则#{grant_id}", conclusion="授权已停用",
                )
            return grant

    # ------------------------------------------------------------------
    # 紧急抢险临时例外（以指挥审批为准）
    # ------------------------------------------------------------------
    def apply_exception(self, values: dict[str, Any]) -> dict[str, Any]:
        project_id = int(values.get("工程id") or 0)
        team_id = int(values.get("班组id") or 0)
        category = str(values.get("材料类别") or "").strip()
        quota = int(values.get("申请额度") or 0)
        reason = str(values.get("事由") or "").strip()
        with self._lock:
            project = self._find_project(project_id)
            team = self._find_team(team_id)
            if not category or quota <= 0 or not reason:
                raise AuthorizationError(
                    "材料类别、申请额度（>0）和抢险事由都必须填写", code="invalid", http_status=400,
                )
            seq = store.next_id(EXCEPTION_TABLE)
            exception = {
                "id": seq,
                "status": "待指挥审批", "pending": True, "abnormal": False,
                "申请单号": f"EXC-{self._now().strftime('%Y%m%d')}-{seq:02d}",
                "工程id": project_id,
                "工程编号": project.get("工程编号"),
                "工程名称": project.get("工程名称"),
                "施工路段": project.get("施工路段"),
                "材料类别": category,
                "申请班组": team.get("班组名称"),
                "班组id": team_id,
                "申请人": str(values.get("申请人") or team.get("负责人") or ""),
                "申请额度": quota,
                "事由": reason,
                "状态": "待指挥审批",
                "审批人": "",
                "审批意见": "",
                "申请时间": self._fmt(self._now()),
                "审批时间": "",
                "有效期止": "",
                "关联授权": None,
            }
            store.rows(EXCEPTION_TABLE).append(exception)
            self._add_todo(
                project,
                f"紧急抢险临时例外待指挥审批：{team['班组名称']}申请{category}"
                f"{quota}个单位，事由：{reason}",
                todo_type="例外审批",
                ref=exception["申请单号"],
                conclusion="待指挥审批",
            )
            return exception

    def decide_exception(self, exception_id: int, approve: bool, values: dict[str, Any]) -> dict[str, Any]:
        approver = str(values.get("审批人") or "值班指挥").strip()
        opinion = str(values.get("审批意见") or "").strip()
        hours = int(values.get("有效期小时") or DEFAULT_EXCEPTION_HOURS)
        with self._lock:
            exception = store.find(EXCEPTION_TABLE, exception_id)
            if exception is None or exception.get("状态") != "待指挥审批":
                raise AuthorizationError("待审批的例外申请不存在", code="not_found", http_status=404)
            project = self._find_project(int(exception["工程id"]))
            exception["审批人"] = approver
            exception["审批意见"] = opinion or ("同意紧急抢险领用" if approve else "不同意临时授权")
            exception["审批时间"] = self._fmt(self._now())
            todo_ref = exception["申请单号"]
            if not approve:
                exception["状态"] = "已驳回"
                exception["status"] = "已驳回"
                exception["pending"] = False
                self._add_todo(
                    project,
                    f"紧急例外被驳回：{exception['申请班组']} 申请 {exception['材料类别']}"
                    f"{exception['申请额度']} 个单位（指挥：{approver}）",
                    todo_type="例外审批", ref=todo_ref, conclusion="拒绝领用",
                )
                return exception

            expires = self._now() + timedelta(hours=max(hours, 1))
            grant = {
                "id": store.next_id(GRANT_TABLE),
                "status": "生效中", "pending": False, "abnormal": False,
                "工程id": project["id"],
                "工程编号": project.get("工程编号"),
                "工程名称": project.get("工程名称"),
                "施工路段": project.get("施工路段"),
                "材料类别": exception["材料类别"],
                "授权对象": exception["申请班组"],
                "权限": "领用",
                "授权额度": int(exception["申请额度"]),
                "已预占": 0,
                "已领用": 0,
                "来源": "紧急例外",
                "状态": "生效中",
                "授权人": f"指挥审批：{approver}",
                "授权时间": self._fmt(self._now()),
                "有效期止": self._fmt(expires),
                "关联例外": exception_id,
                "版本": 1,
            }
            store.rows(GRANT_TABLE).append(grant)
            exception["状态"] = "已批准"
            exception["status"] = "已批准"
            exception["pending"] = False
            exception["有效期止"] = grant["有效期止"]
            exception["关联授权"] = grant["id"]
            self._rebuild_locked()
            self._add_todo(
                project,
                f"紧急例外经指挥批准：{exception['申请班组']} 可在 {project.get('施工路段')} 领用"
                f"{exception['材料类别']}（额度{grant['授权额度']}，有效期至 {grant['有效期止']}）",
                todo_type="授权知会", ref=todo_ref, conclusion="允许领用（紧急例外）",
            )
            return exception

    # ------------------------------------------------------------------
    # 领料：校验 → 额度预占 → 库存预占（同一把锁原子完成）
    # ------------------------------------------------------------------
    def _reject_requisition(
        self,
        *,
        project: dict[str, Any],
        team: dict[str, Any],
        category: str,
        quantity: int,
        material_code: str,
        applicant: str,
        vehicle: dict[str, Any] | None,
        decision: dict[str, Any],
        reason: str,
        code: str,
    ) -> dict[str, Any]:
        """落一张拒绝单与一条工程待办；拒绝内容绝不写入供应商。"""
        seq = store.next_id(REQUISITION_TABLE)
        row = {
            "id": seq,
            "status": "已拒绝", "pending": False, "abnormal": True,
            "领料单号": f"REQU-{self._now().strftime('%Y%m%d')}-{seq:02d}",
            "工程id": project["id"],
            "工程编号": project.get("工程编号"),
            "工程名称快照": project.get("工程名称"),
            "施工路段": project.get("施工路段"),
            "承建单位快照": project.get("承建单位"),
            "材料类别": category,
            "材料编号": material_code,
            "材料名称": "",
            "规格型号": "",
            "计量单位": "",
            "数量": quantity,
            "领料班组": team.get("班组名称"),
            "领料人": applicant,
            "车辆编号": vehicle.get("车辆编号", "") if vehicle else "",
            "车牌号": vehicle.get("车牌号", "") if vehicle else "",
            "状态": "已拒绝",
            "授权结论": reason,
            "授权依据": decision.get("来源"),
            "关联授权": decision.get("关联授权"),
            "授权版本": self._cache_version,
            "供应商": "",  # 越权/超限拒绝：不暴露供应商
            "申请时间": self._fmt(self._now()),
            "出库时间": "",
            "拒绝代码": code,
        }
        store.rows(REQUISITION_TABLE).append(row)
        self._add_todo(
            project, f"领料被拒绝：{team.get('班组名称')} 申请 {category}{quantity} 个单位（{reason}）",
            todo_type="越权拦截" if code == "forbidden" else "领料异常",
            ref=row["领料单号"], conclusion="拒绝领用",
        )
        return row

    def apply_requisition(self, values: dict[str, Any]) -> dict[str, Any]:
        project_id = int(values.get("工程id") or 0)
        team_id = int(values.get("班组id") or 0)
        category = str(values.get("材料类别") or "").strip()
        quantity = int(values.get("数量") or 0)
        applicant = str(values.get("领料人") or "").strip()
        material_code = str(values.get("材料编号") or "").strip()
        vehicle_id = values.get("车辆编号") or values.get("车辆id")
        with self._lock:
            project = self._find_project(project_id)
            team = self._find_team(team_id)
            vehicle = None
            if vehicle_id not in (None, ""):
                vehicle = store.find("vehicle", int(vehicle_id))
            if not category or quantity <= 0 or not applicant:
                raise AuthorizationError("材料类别、数量（>0）和领料人必须填写", code="invalid", http_status=400)

            decision = self._resolve_locked(team, project, category)

            # 1) 越权拒绝：只读/无权限一律拦下，且不给任何供应商信息
            if decision["权限"] != "领用":
                scope = "只读权限，不能领用" if decision["权限"] == "只读" else "没有授权"
                reason = (
                    f"越权领用已拒绝：{team['班组名称']} 在工程 {project.get('工程编号')}"
                    f"（{project.get('施工路段')}）对材料类别「{category}」{scope}，"
                    "请先走直接授权或紧急抢险例外审批"
                )
                self._reject_requisition(
                    project=project, team=team, category=category, quantity=quantity,
                    material_code=material_code, applicant=applicant, vehicle=vehicle,
                    decision=decision, reason=reason, code="forbidden",
                )
                raise AuthorizationError(reason, code="forbidden", http_status=403)

            # 2) 额度校验：并发下以锁内实时计数为准，不能超授权额度
            if quantity > int(decision["剩余额度"]):
                reason = (
                    f"领料超过授权额度：{category} 授权额度 {decision['授权额度']}，"
                    f"已预占 {decision['已预占']}、已领用 {decision['已领用']}，"
                    f"仅剩 {decision['剩余额度']}，本次申请 {quantity}"
                )
                self._reject_requisition(
                    project=project, team=team, category=category, quantity=quantity,
                    material_code=material_code, applicant=applicant, vehicle=vehicle,
                    decision=decision, reason=reason, code="over_quota",
                )
                raise AuthorizationError(reason, code="over_quota", http_status=409)

            # 3) 选库存并预占（仍在同一把锁里：缓存与库存原子更新）
            candidates = self._stock_rows(category)
            target: dict[str, Any] | None = None
            if material_code:
                target = next((row for row in candidates if row.get("材料编号") == material_code), None)
                if target is None:
                    raise AuthorizationError(
                        f"材料编号 {material_code} 不属于「{category}」或不在库",
                        code="invalid", http_status=400,
                    )
            else:
                target = next((row for row in candidates if self._available_stock(row) >= quantity), None)
            if target is None or self._available_stock(target) < quantity:
                available = sum(self._available_stock(row) for row in candidates)
                reason = f"库存不足：{category} 当前可用 {available} 个单位，无法满足本次 {quantity} 个单位领料"
                self._reject_requisition(
                    project=project, team=team, category=category, quantity=quantity,
                    material_code=material_code or (target.get("材料编号", "") if target else ""),
                    applicant=applicant, vehicle=vehicle,
                    decision=decision, reason=reason, code="out_of_stock",
                )
                raise AuthorizationError(reason, code="out_of_stock", http_status=409)

            # —— 全部校验通过：预占额度 + 预占库存 + 推进缓存事务，原子提交 ——
            entry = self._cache[(int(team_id), project_id, category)]
            if entry["kind"] == "inherit":
                usage = self._inherit_usage[(int(team_id), project_id, category)]
                usage["已预占"] += quantity
                linked_grant = None
            else:
                grant = entry["grant"]
                grant["已预占"] = int(grant.get("已预占", 0)) + quantity
                linked_grant = int(grant.get("id", 0))
            target["已预占"] = int(target.get("已预占", 0)) + quantity
            self._cache_txn += 1

            seq = store.next_id(REQUISITION_TABLE)
            requisition = {
                "id": seq,
                "status": "已预占待出库", "pending": True, "abnormal": False,
                "领料单号": f"REQU-{self._now().strftime('%Y%m%d')}-{seq:02d}",
                "工程id": project["id"],
                "工程编号": project.get("工程编号"),
                "工程名称快照": project.get("工程名称"),
                "施工路段": project.get("施工路段"),
                "承建单位快照": project.get("承建单位"),
                "材料类别": category,
                "材料编号": target.get("材料编号"),
                "材料名称": target.get("材料名称"),
                "规格型号": target.get("规格型号"),
                "计量单位": target.get("计量单位"),
                "数量": quantity,
                "领料班组": team.get("班组名称"),
                "领料人": applicant,
                "车辆编号": vehicle.get("车辆编号", "") if vehicle else "",
                "车牌号": vehicle.get("车牌号", "") if vehicle else "",
                "状态": "已预占待出库",
                "授权结论": f"允许领用｜依据：{decision['来源']}（缓存v{self._cache_version}/tx{self._cache_txn}）",
                "授权依据": decision["来源"],
                "关联授权": linked_grant,
                "授权版本": self._cache_version,
                "供应商": target.get("供应商", ""),
                "申请时间": self._fmt(self._now()),
                "出库时间": "",
            }
            store.rows(REQUISITION_TABLE).append(requisition)

            load = {
                "id": store.next_id(LOAD_TABLE),
                "status": "待装车", "pending": True, "abnormal": False,
                "车辆编号": requisition["车辆编号"] or "未指定车辆",
                "车牌号": requisition["车牌号"],
                "领料单号": requisition["领料单号"],
                "工程id": project["id"],
                "工程名称": project.get("工程名称"),
                "施工路段": project.get("施工路段"),
                "材料编号": target.get("材料编号"),
                "材料名称": target.get("材料名称"),
                "材料类别": category,
                "规格型号": target.get("规格型号"),
                "计量单位": target.get("计量单位"),
                "数量": quantity,
                "授权结论": requisition["授权结论"],
                "状态值": "待装车",
                "装车时间": "",
            }
            store.rows(LOAD_TABLE).append(load)
            self._add_todo(
                project,
                f"领料已预占：{team['班组名称']} 申请 {target.get('材料名称')}{quantity}"
                f"{target.get('计量单位')}，待出库（{requisition['领料单号']}）",
                todo_type="领料预占", ref=requisition["领料单号"], conclusion="允许领用",
            )
            return requisition

    def _get_open_requisition(self, requisition_id: int) -> dict[str, Any]:
        row = store.find(REQUISITION_TABLE, requisition_id)
        if row is None:
            raise AuthorizationError(f"领料单 {requisition_id} 不存在", code="not_found", http_status=404)
        return row

    def _release_reservation_locked(self, requisition: dict[str, Any]) -> None:
        """把预占从额度计数与库存计数中同时回滚。"""
        quantity = int(requisition.get("数量", 0))
        category = str(requisition.get("材料类别"))
        team_name = str(requisition.get("领料班组"))
        team = next((t for t in store.rows("team") if t.get("班组名称") == team_name), None)
        project_id = int(requisition.get("工程id", 0))
        if team is not None:
            key = (int(team["id"]), project_id, category)
            entry = self._cache.get(key)
            if entry is not None and entry["kind"] == "inherit":
                usage = self._inherit_usage[key]
                usage["已预占"] = max(0, usage["已预占"] - quantity)
            elif entry is not None:
                grant = entry["grant"]
                grant["已预占"] = max(0, int(grant.get("已预占", 0)) - quantity)
        stock = next(
            (row for row in self._stock_rows(category) if row.get("材料编号") == requisition.get("材料编号")),
            None,
        )
        if stock is not None:
            stock["已预占"] = max(0, int(stock.get("已预占", 0)) - quantity)
        self._cache_txn += 1

    def outbound_requisition(self, requisition_id: int) -> dict[str, Any]:
        """仓库确认出库：预占转实领，并把授权结论落到材料台账与车辆装载清单。"""
        with self._lock:
            requisition = self._get_open_requisition(requisition_id)
            if requisition.get("状态") != "已预占待出库":
                raise AuthorizationError(
                    f"领料单当前状态为「{requisition.get('状态')}」，不能出库",
                    code="conflict", http_status=409,
                )
            quantity = int(requisition["数量"])
            category = str(requisition["材料类别"])
            team_name = str(requisition.get("领料班组"))
            team = next((t for t in store.rows("team") if t.get("班组名称") == team_name), None)
            project_id = int(requisition["工程id"])

            # 额度：预占 → 已领用
            if team is not None:
                entry = self._cache.get((int(team["id"]), project_id, category))
                if entry is not None and entry["kind"] == "inherit":
                    usage = self._inherit_usage[(int(team["id"]), project_id, category)]
                    usage["已预占"] = max(0, usage["已预占"] - quantity)
                    usage["已领用"] += quantity
                elif entry is not None:
                    grant = entry["grant"]
                    grant["已预占"] = max(0, int(grant.get("已预占", 0)) - quantity)
                    grant["已领用"] = int(grant.get("已领用", 0)) + quantity
                    grant["版本"] = int(grant.get("版本", 1)) + 1

            # 库存：预占 → 已领用
            stock = next(
                (row for row in self._stock_rows(category) if row.get("材料编号") == requisition["材料编号"]),
                None,
            )
            if stock is not None:
                stock["已预占"] = max(0, int(stock.get("已预占", 0)) - quantity)
                stock["已领用"] = int(stock.get("已领用", 0)) + quantity
            self._cache_txn += 1

            now_text = self._fmt(self._now())
            requisition["状态"] = "已出库"
            requisition["status"] = "已出库"
            requisition["pending"] = False
            requisition["出库时间"] = now_text

            # 授权结论落到材料台账
            ledger = next(
                (row for row in store.rows(LEDGER_TABLE) if row.get("材料编号") == requisition["材料编号"]),
                None,
            )
            if ledger is not None:
                ledger["status"] = "已领用"
                ledger["材料状态"] = "已领用"
                ledger["pending"] = False
                ledger["累计出库"] = int(ledger.get("累计出库", 0)) + quantity
                ledger["授权结论"] = requisition["授权结论"]
                ledger["关联领料单"] = requisition["领料单号"]
                ledger["最近出库"] = now_text

            # 落到车辆装载清单
            load = next(
                (row for row in store.rows(LOAD_TABLE) if row.get("领料单号") == requisition["领料单号"]),
                None,
            )
            if load is not None:
                load["状态值"] = "已装车"
                load["status"] = "已装车"
                load["pending"] = False
                load["装车时间"] = now_text

            project = store.find("project", project_id)
            if project is not None:
                self._add_todo(
                    project,
                    f"领料已出库：{requisition['材料名称']}{quantity}{requisition.get('计量单位')}"
                    f"（{requisition['领料单号']}），结论已写入台账与装载清单",
                    todo_type="出库确认", ref=requisition["领料单号"], conclusion="允许领用",
                )
            return requisition

    def cancel_requisition(self, requisition_id: int) -> dict[str, Any]:
        with self._lock:
            requisition = self._get_open_requisition(requisition_id)
            if requisition.get("状态") != "已预占待出库":
                raise AuthorizationError(
                    f"领料单当前状态为「{requisition.get('状态')}」，不能取消",
                    code="conflict", http_status=409,
                )
            self._release_reservation_locked(requisition)
            requisition["状态"] = "已取消"
            requisition["status"] = "已取消"
            requisition["pending"] = False
            for load in store.rows(LOAD_TABLE):
                if load.get("领料单号") == requisition["领料单号"] and load.get("状态值") == "待装车":
                    load["状态值"] = "已取消"
                    load["status"] = "已取消"
                    load["pending"] = False
            project = store.find("project", int(requisition["工程id"]))
            if project is not None:
                self._add_todo(
                    project,
                    f"领料取消并释放预占：{requisition.get('材料名称')}{requisition['数量']}"
                    f"{requisition.get('计量单位')}（{requisition['领料单号']}）",
                    todo_type="领料取消", ref=requisition["领料单号"], conclusion="预占已释放",
                )
            return requisition

    # ------------------------------------------------------------------
    # 工程转组：归属变更只改继承口径，历史领用关系保持快照不变
    # ------------------------------------------------------------------
    def transfer_project(self, project_id: int, values: dict[str, Any]) -> dict[str, Any]:
        new_unit = str(values.get("新承建单位") or "").strip()
        with self._lock:
            project = self._find_project(project_id)
            if not new_unit:
                raise AuthorizationError("新承建单位不能为空", code="invalid", http_status=400)
            old_unit = str(project.get("承建单位"))
            if new_unit == old_unit:
                raise AuthorizationError("新承建单位与当前一致，无需转组", code="invalid", http_status=400)

            before = self.cache_snapshot(project_id=project_id)
            project["承建单位"] = new_unit
            self._rebuild_locked()
            after = self.cache_snapshot(project_id=project_id)

            # 历史领料单：快照列保持不变，只核对没有被动过
            history = [
                row for row in store.rows(REQUISITION_TABLE)
                if int(row.get("工程id", 0)) == project_id
            ]
            untouched = all(row.get("承建单位快照") == old_unit for row in history)
            self._add_todo(
                project,
                f"工程转组：承建单位 {old_unit} → {new_unit}；继承权限已重算，"
                f"{len(history)} 条历史领用关系保持不变",
                todo_type="工程转组", ref=f"工程#{project_id}", conclusion="历史领用关系不变",
            )
            return {
                "工程id": project_id,
                "原承建单位": old_unit,
                "新承建单位": new_unit,
                "历史领料单数": len(history),
                "历史关系保持不变": untouched,
                "转组前缓存条目": before["条目数"],
                "转组后缓存条目": after["条目数"],
                "缓存版本": self._cache_version,
            }

    def resolve_todo(self, todo_id: int, operator: str = "") -> dict[str, Any]:
        with self._lock:
            todo = store.find(TODO_TABLE, todo_id)
            if todo is None:
                raise AuthorizationError(f"待办 {todo_id} 不存在", code="not_found", http_status=404)
            todo["状态值"] = "已处理"
            todo["status"] = "已处理"
            todo["pending"] = False
            todo["处理时间"] = self._fmt(self._now())
            todo["处理人"] = operator or "值班管理员"
            return todo
