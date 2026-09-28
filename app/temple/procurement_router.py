from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.procurement import ProcurementService
from app.temple.procurement_schemas import (
    ChangeOrderCreate,
    ChangeOrderDecision,
    ChangeOrderResubmit,
    ChangeOrderSignature,
    ChangeOrderSubmit,
    DraftLineRemove,
    DraftLineUpsert,
    PackageAction,
    ProcurementPackageCreate,
    ReceiptCreate,
)

router = APIRouter(prefix="/api/temple/operations", tags=["采购包与变更单"])


def service() -> ProcurementService:
    return ProcurementService()


@router.post("/procurement/packages", status_code=201)
def create_procurement_package(payload: ProcurementPackageCreate):
    return service().create_package(payload.model_dump())


@router.get("/procurement/packages")
def list_procurement_packages(temple_code: str | None = None, state: str | None = None):
    return {"items": service().list_packages(temple_code, state)}


@router.get("/procurement/packages/{package_id}")
def procurement_package_detail(package_id: int):
    return service().package_detail(package_id)


@router.put("/procurement/packages/{package_id}/lines")
def upsert_draft_line(package_id: int, payload: DraftLineUpsert):
    return service().upsert_draft_line(package_id, payload.model_dump())


@router.post("/procurement/packages/{package_id}/lines/{material_code}/remove")
def remove_draft_line(package_id: int, material_code: str, payload: DraftLineRemove):
    return service().remove_draft_line(package_id, material_code, payload.actor)


@router.post("/procurement/packages/{package_id}/freeze")
def freeze_procurement_package(package_id: int, payload: PackageAction):
    return service().freeze_package(package_id, payload.actor)


@router.get("/procurement/packages/{package_id}/versions")
def list_procurement_versions(package_id: int):
    return {"items": service().list_versions(package_id)}


@router.get("/procurement/package_versions/{version_id}")
def procurement_version_detail(version_id: int):
    return service().version_detail(version_id)


@router.post("/procurement/packages/{package_id}/receipts", status_code=201)
def record_procurement_receipt(package_id: int, payload: ReceiptCreate):
    return service().record_receipt(package_id, payload.model_dump())


@router.get("/procurement/packages/{package_id}/receipts")
def list_procurement_receipts(package_id: int):
    return {"items": service().list_receipts(package_id)}


@router.post("/procurement/packages/{package_id}/change_orders", status_code=201)
def create_change_order(package_id: int, payload: ChangeOrderCreate):
    return service().create_change_order(package_id, payload.model_dump())


@router.get("/procurement/change_orders")
def list_change_orders(package_id: int | None = None, state: str | None = Query(default=None)):
    return {"items": service().list_change_orders(package_id, state)}


@router.get("/procurement/change_orders/{change_order_id}")
def change_order_detail(change_order_id: int):
    return service().change_order_detail(change_order_id)


@router.post("/procurement/change_orders/{change_order_id}/signatures", status_code=201)
def add_change_order_signature(change_order_id: int, payload: ChangeOrderSignature):
    return service().add_signature(change_order_id, payload.model_dump())


@router.post("/procurement/change_orders/{change_order_id}/submit")
def submit_change_order(change_order_id: int, payload: ChangeOrderSubmit):
    return service().submit_change_order(change_order_id, payload.model_dump())


@router.post("/procurement/change_orders/{change_order_id}/reject")
def reject_change_order(change_order_id: int, payload: ChangeOrderDecision):
    return service().reject_change_order(change_order_id, payload.actor, payload.note)


@router.post("/procurement/change_orders/{change_order_id}/withdraw")
def withdraw_change_order(change_order_id: int, payload: ChangeOrderDecision):
    return service().withdraw_change_order(change_order_id, payload.actor, payload.note)


@router.post("/procurement/change_orders/{change_order_id}/approve")
def approve_change_order(change_order_id: int, payload: ChangeOrderDecision):
    return service().approve_change_order(change_order_id, payload.actor, payload.note)


@router.post("/procurement/change_orders/{change_order_id}/resubmit", status_code=201)
def resubmit_change_order(change_order_id: int, payload: ChangeOrderResubmit):
    return service().resubmit_change_order(change_order_id, payload.model_dump())
