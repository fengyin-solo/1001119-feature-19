"""项目领用授权柜接口。

围绕养护材料，按「工程 × 施工路段 × 材料类别」给领用单位授予领用/只读权限，
提供实时预览、授权配置、紧急例外指挥审批、领料（原子预占/发料）、工程待办与
车辆装载清单查询，以及项目转组等能力。

安全口径：越权拒绝与无领用权限的材料查询都不会回传供应商信息。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.grant import grant_service

router = APIRouter(prefix="/api/grant", tags=["项目领用授权柜"])


class GrantPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class ExceptionApproval(BaseModel):
    approver: str = ""
    approver_title: str = ""
    opinion: str = ""
    approved: bool = True


class TransferPayload(BaseModel):
    new_owner: str
    operator: str = ""


@router.get("/options")
def options() -> dict[str, Any]:
    """授权柜下拉项：工程、路段（来自工程）、材料类别、可授予的权限。"""
    projects = [
        {"id": p.get("id"), "工程编号": p.get("工程编号"), "工程名称": p.get("工程名称"),
         "施工路段": p.get("施工路段"), "归属单位": p.get("归属单位"), "状态": p.get("status")}
        for p in _projects()
    ]
    categories = sorted({str(m.get("材料类别")) for m in _materials() if m.get("材料类别")})
    units = sorted({u for u in _units() if u})
    return {
        "工程": projects,
        "材料类别": categories,
        "领用单位": units,
        "权限": ["领用", "只读"],
    }


def _projects() -> list[dict[str, Any]]:
    from app.store import store
    return store.rows("project")


def _materials() -> list[dict[str, Any]]:
    from app.store import store
    return store.rows("material")


def _units() -> set[str]:
    units = {str(p.get("归属单位", "")) for p in _projects()}
    units.update(str(g.get("领用单位", "")) for g in grant_service.list_grants())
    return units


@router.get("/preview")
def preview(
    project: str = Query(..., description="工程编号"),
    section: str = Query("", description="施工路段，缺省取工程登记路段"),
    category: str = Query(..., description="材料类别"),
    unit: str = Query("", description="领用单位，传入后同时给出权限结论"),
    quantity: int = Query(0, ge=0, description="拟申请数量，用于实时判断额度/库存"),
) -> dict[str, Any]:
    """实时预览：该授权/申请会影响的任务与库存。"""
    try:
        return grant_service.preview(project, section, category, unit or None, quantity)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}


@router.get("/grants")
def list_grants(project: str | None = None, unit: str | None = None) -> dict[str, Any]:
    rows = grant_service.list_grants(project, unit)
    return {"total": len(rows), "items": rows}


@router.post("/grants", response_model=ActionResult)
def upsert_grant(payload: GrantPayload) -> ActionResult:
    entry, message = grant_service.upsert_grant(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/grants/{grant_id}/state", response_model=ActionResult)
def set_grant_state(grant_id: int, payload: GrantPayload) -> ActionResult:
    state = str(payload.values.get("状态", "")).strip()
    entry, message = grant_service.set_grant_state(grant_id, state)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/exceptions")
def list_exceptions(status: str | None = None) -> dict[str, Any]:
    rows = grant_service.list_exceptions(status)
    return {"total": len(rows), "items": rows}


@router.post("/exceptions", response_model=ActionResult)
def apply_exception(payload: GrantPayload) -> ActionResult:
    entry, message = grant_service.apply_exception(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/exceptions/{exception_id}/approval", response_model=ActionResult)
def approve_exception(exception_id: int, payload: ExceptionApproval) -> ActionResult:
    entry, message = grant_service.approve_exception(
        exception_id,
        approver=payload.approver,
        approver_title=payload.approver_title,
        opinion=payload.opinion,
        approved=payload.approved,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/requisitions")
def list_requisitions(
    project: str | None = None, unit: str | None = None, status: str | None = None
) -> dict[str, Any]:
    rows = grant_service.list_requisitions(project, unit, status)
    return {"total": len(rows), "items": rows}


@router.post("/requisitions", response_model=ActionResult)
def apply_requisition(payload: GrantPayload) -> ActionResult:
    entry, message, allowed = grant_service.apply_requisition(payload.values)
    return ActionResult(ok=allowed, message=message, entry=entry)


@router.post("/requisitions/{requisition_id}/issue", response_model=ActionResult)
def issue_requisition(requisition_id: int, payload: GrantPayload) -> ActionResult:
    plate = str(payload.values.get("车牌号", "")).strip()
    entry, message = grant_service.issue_requisition(requisition_id, plate)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/todos")
def list_todos(project: str | None = None) -> dict[str, Any]:
    rows = grant_service.list_todos(project)
    return {"total": len(rows), "items": rows}


@router.get("/loads")
def list_loads(project: str | None = None) -> dict[str, Any]:
    rows = grant_service.list_loads(project)
    return {"total": len(rows), "items": rows}


@router.get("/materials")
def scoped_materials(
    project: str | None = None, unit: str | None = None, category: str | None = None
) -> dict[str, Any]:
    """按领用单位权限查看材料；无领用权限时供应商被遮蔽。"""
    rows = grant_service.scoped_materials(project, unit, category)
    return {"total": len(rows), "items": rows}


@router.get("/decision")
def decision(
    project: str = Query(...),
    section: str = Query(""),
    category: str = Query(...),
    unit: str = Query(...),
) -> dict[str, Any]:
    """实时查询某个单位在工程×路段×类别上的权限结论（走权限缓存）。"""
    project_row = grant_service._get_project(project)  # noqa: SLF001 - 只读便捷接口
    if not section and project_row is not None:
        section = str(project_row.get("施工路段", ""))
    return grant_service.evaluate(project, section, category, unit)


@router.post("/projects/{project_id}/transfer", response_model=ActionResult)
def transfer_project(project_id: int, payload: TransferPayload) -> ActionResult:
    entry, message, frozen = grant_service.transfer_project(
        project_id, payload.new_owner, operator=payload.operator
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    result = dict(entry)
    result["冻结历史领用单数"] = frozen
    return ActionResult(ok=True, message=message, entry=result)
