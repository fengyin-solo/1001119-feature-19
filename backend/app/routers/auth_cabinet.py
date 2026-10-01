"""项目领用授权柜接口。

- GET  /meta                       下拉元数据（工程/班组/类别/车辆）
- GET  /preview                    实时预览：授权结论 + 会影响的任务与库存
- GET  /cache                      权限缓存快照（版本、事务序号）
- GET  /grants、/exceptions、/requisitions、/todos、/loads、/stock  各台账查询
- POST /grants                     按 工程×路段×类别 授予领用/只读
- POST /grants/{id}/revoke         停用授权
- POST /exceptions                 紧急抢险申请临时例外
- POST /exceptions/{id}/approve|reject  指挥审批（以指挥审批为准）
- POST /requisitions               领料（越权拒绝、额度与库存原子预占）
- POST /requisitions/{id}/outbound 确认出库，结论落台账/待办/装载清单
- POST /requisitions/{id}/cancel   取消并释放预占
- POST /projects/{id}/transfer     工程转组（历史领用关系不变）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.auth_cabinet import AuthorizationError, AuthCabinetService

router = APIRouter(prefix="/api/auth-cabinet", tags=["项目领用授权柜"])

service = AuthCabinetService()


class ValuesPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


def _call(action, *args, **kwargs):
    """统一把业务拒绝翻译成 HTTP 响应，保证拒绝原因可读且不含敏感信息。"""
    try:
        return action(*args, **kwargs)
    except AuthorizationError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.reason})


@router.get("/meta")
def meta() -> dict[str, Any]:
    """授权配置页需要的工程、班组、材料类别、车辆选项。"""
    return service.meta()


@router.get("/preview")
def preview(
    project_id: int = Query(..., description="工程 id"),
    category: str = Query(..., description="材料类别"),
    team_id: int = Query(..., description="领料班组 id"),
) -> dict[str, Any]:
    """实时预览：按当前三维选择给出授权结论，并列出会影响的任务与库存。

    无领用权限时库存与历史单据中的「供应商」字段会被剔除。
    """
    return _call(service.preview, project_id, category, team_id)


@router.get("/cache")
def cache_snapshot(
    project_id: int | None = None,
    team_id: int | None = None,
) -> dict[str, Any]:
    """权限缓存快照：附带缓存版本与事务序号，便于核对原子更新。"""
    return service.cache_snapshot(project_id=project_id, team_id=team_id)


@router.get("/grants")
def list_grants(
    project_id: int | None = None,
    status: str | None = Query(default=None, description="生效中、已停用"),
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if project_id is not None:
        filters["工程id"] = project_id
    if status:
        filters["状态"] = status
    return {"items": service.list_rows("auth_grant", **filters)}


@router.post("/grants")
def create_grant(payload: ValuesPayload) -> dict[str, Any]:
    """按工程、施工路段、材料类别给班组授予领用或只读权限。"""
    grant = _call(service.create_grant, payload.values)
    return {"ok": True, "message": "授权已生效并同步工程待办", "entry": grant}


@router.post("/grants/{grant_id}/revoke")
def revoke_grant(grant_id: int, payload: ValuesPayload | None = None) -> dict[str, Any]:
    """停用一条生效中的授权；仍有预占时拒绝停用。"""
    operator = (payload.values.get("操作人") if payload else "") or ""
    grant = _call(service.revoke_grant, grant_id, operator)
    return {"ok": True, "message": "授权已停用", "entry": grant}


@router.get("/exceptions")
def list_exceptions(status: str | None = Query(default=None, description="待指挥审批、已批准、已驳回")) -> dict[str, Any]:
    filters = {"状态": status} if status else {}
    return {"items": service.list_rows("auth_exception", **filters)}


@router.post("/exceptions")
def apply_exception(payload: ValuesPayload) -> dict[str, Any]:
    """紧急抢险可申请临时例外；提交后进入「待指挥审批」，申请通过前不产生任何授权。"""
    exception = _call(service.apply_exception, payload.values)
    return {"ok": True, "message": "临时例外已提交，等待指挥审批", "entry": exception}


@router.post("/exceptions/{exception_id}/approve")
def approve_exception(exception_id: int, payload: ValuesPayload | None = None) -> dict[str, Any]:
    values = payload.values if payload else {}
    exception = _call(service.decide_exception, exception_id, True, values)
    return {"ok": True, "message": "指挥已批准，临时授权生效", "entry": exception}


@router.post("/exceptions/{exception_id}/reject")
def reject_exception(exception_id: int, payload: ValuesPayload | None = None) -> dict[str, Any]:
    values = payload.values if payload else {}
    exception = _call(service.decide_exception, exception_id, False, values)
    return {"ok": True, "message": "指挥已驳回，不产生授权", "entry": exception}


@router.get("/stock")
def list_stock(
    category: str | None = Query(default=None, description="按材料类别过滤"),
    team_id: int | None = Query(default=None, description="以班组视角查看，未授权类别自动脱敏供应商"),
) -> dict[str, Any]:
    """库存台账；班组视角下未获领用授权的类别不返回供应商。"""
    filters = {"材料类别": category} if category else {}
    items = service.list_rows("material_stock", **filters)
    if team_id is not None:
        # 库存不区分工程：班组在任一工程下对该类别有领用授权，即可见供应商
        snapshot = service.cache_snapshot(team_id=team_id)
        categories_allowed = {
            item["材料类别"] for item in snapshot["items"] if item["权限"] == "领用"
        }
        items = [
            row if row.get("材料类别") in categories_allowed
            else {k: v for k, v in row.items() if k != "供应商"}
            for row in items
        ]
    return {"items": items}


@router.get("/requisitions")
def list_requisitions(
    project_id: int | None = None,
    team_id: int | None = Query(default=None, description="以班组视角查看，未授权类别脱敏供应商"),
    status: str | None = Query(default=None, description="已预占待出库、已出库、已拒绝、已取消"),
) -> dict[str, Any]:
    """领料单台账（授权结论随流程落这里）。"""
    filters: dict[str, Any] = {}
    if project_id is not None:
        filters["工程id"] = project_id
    if status:
        filters["状态"] = status
    items = service.list_rows("material_requisition", viewer_team_id=team_id, **filters)
    return {"items": items}


@router.post("/requisitions")
def apply_requisition(payload: ValuesPayload) -> dict[str, Any]:
    """领料申请：越权拒绝且不暴露供应商；校验通过后额度与库存原子预占。

    拒绝时同样会留一张「已拒绝」领料单和工程待办，但其中不含任何供应商信息。
    """
    try:
        requisition = service.apply_requisition(payload.values)
    except AuthorizationError as exc:
        # 拒绝单已在服务内落表；响应体只回拒绝原因与代码，不回供应商
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": exc.reason, "供应商可见": False},
        )
    return {"ok": True, "message": "授权通过，额度与库存已原子预占，待出库", "entry": requisition}


@router.post("/requisitions/{requisition_id}/outbound")
def outbound_requisition(requisition_id: int) -> dict[str, Any]:
    """仓库确认出库：预占转实领，授权结论同步写入材料台账与车辆装载清单。"""
    requisition = _call(service.outbound_requisition, requisition_id)
    return {"ok": True, "message": "已出库，授权结论已落入材料台账与车辆装载清单", "entry": requisition}


@router.post("/requisitions/{requisition_id}/cancel")
def cancel_requisition(requisition_id: int) -> dict[str, Any]:
    """取消预占：授权额度与库存预占原子释放。"""
    requisition = _call(service.cancel_requisition, requisition_id)
    return {"ok": True, "message": "领料已取消，预占已释放", "entry": requisition}


@router.get("/todos")
def list_todos(
    project_id: int | None = None,
    status: str | None = Query(default=None, description="待处理、已处理"),
) -> dict[str, Any]:
    """工程待办：授权生效、例外审批、越权拦截、出库确认都会落在这里。"""
    filters: dict[str, Any] = {}
    if project_id is not None:
        filters["工程id"] = project_id
    if status:
        filters["状态值"] = status
    return {"items": service.list_rows("project_todo", **filters)}


@router.post("/todos/{todo_id}/resolve")
def resolve_todo(todo_id: int, payload: ValuesPayload | None = None) -> dict[str, Any]:
    operator = (payload.values.get("处理人") if payload else "") or ""
    todo = _call(service.resolve_todo, todo_id, operator)
    return {"ok": True, "message": "待办已处理", "entry": todo}


@router.get("/loads")
def list_loads(
    vehicle_code: str | None = Query(default=None, alias="vehicle"),
    project_id: int | None = None,
) -> dict[str, Any]:
    """车辆装载清单：授权结论随领料流程落到单车。"""
    filters: dict[str, Any] = {}
    if vehicle_code:
        filters["车辆编号"] = vehicle_code
    if project_id is not None:
        filters["工程id"] = project_id
    return {"items": service.list_rows("vehicle_load", **filters)}


@router.post("/projects/{project_id}/transfer")
def transfer_project(project_id: int, payload: ValuesPayload) -> dict[str, Any]:
    """工程转组：重算默认继承权限；历史领用关系保持快照不变。"""
    result = _call(service.transfer_project, project_id, payload.values)
    return {"ok": True, "message": "工程已转组，权限缓存已重算，历史领用关系不变", "entry": result}
