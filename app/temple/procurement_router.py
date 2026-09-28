from __future__ import annotations

from fastapi import APIRouter

from app.temple.procurement import ProcurementService
from app.temple.procurement_schemas import (
    ChangeOrderAction,
    ChangeOrderCreate,
    ChangeOrderDecision,
    ChangeOrderResubmit,
    ChangeOrderSubmit,
    MaterialReceiptInput,
    PackageFreeze,
    ProcurementPackageCreate,
    SpecialFundCreate,
)

router = APIRouter(prefix="/api/temple/procurement", tags=["采购包与变更单管理"])


def service() -> ProcurementService:
    return ProcurementService()


@router.post("/funds", status_code=201)
def create_fund(payload: SpecialFundCreate):
    return service().create_fund(payload.model_dump())


@router.get("/funds/{code}")
def fund_detail(code: str):
    return service().fund_detail(code)


@router.post("/packages", status_code=201)
def create_package(payload: ProcurementPackageCreate):
    return service().create_package(payload.model_dump())


@router.get("/packages")
def list_packages(temple_code: str | None = None, state: str | None = None):
    return {"items": service().list_packages(temple_code, state)}


@router.get("/packages/{code}")
def package_detail(code: str):
    return service().package_detail(code)


@router.post("/packages/{code}/freeze")
def freeze_package(code: str, payload: PackageFreeze):
    return service().freeze_package(code, payload.model_dump())


@router.post("/packages/{code}/close")
def close_package(code: str, payload: ChangeOrderAction):
    return service().close_package(code, payload.actor, payload.reason)


@router.get("/packages/{code}/versions/{version_no}")
def package_version(code: str, version_no: int):
    return service().package_version(code, version_no)


@router.post("/packages/{code}/materials/{material_code}/receipts", status_code=201)
def receive_material(code: str, material_code: str, payload: MaterialReceiptInput):
    return service().receive_material(code, material_code, payload.model_dump())


@router.post("/change_orders", status_code=201)
def create_change_order(payload: ChangeOrderCreate):
    return service().create_change_order(payload.model_dump())


@router.get("/change_orders")
def list_change_orders(package_code: str | None = None, state: str | None = None):
    return {"items": service().list_change_orders(package_code, state)}


@router.get("/change_orders/{change_order_id}")
def change_order_detail(change_order_id: int):
    return service().change_order_detail(change_order_id)


@router.post("/change_orders/{change_order_id}/submit")
def submit_change_order(change_order_id: int, payload: ChangeOrderSubmit):
    return service().submit_change_order(change_order_id, payload.model_dump())


@router.post("/change_orders/{change_order_id}/approve")
def approve_change_order(change_order_id: int, payload: ChangeOrderDecision):
    return service().approve_change_order(change_order_id, payload.actor, payload.note)


@router.post("/change_orders/{change_order_id}/reject")
def reject_change_order(change_order_id: int, payload: ChangeOrderDecision):
    return service().reject_change_order(change_order_id, payload.actor, payload.note)


@router.post("/change_orders/{change_order_id}/withdraw")
def withdraw_change_order(change_order_id: int, payload: ChangeOrderAction):
    return service().withdraw_change_order(change_order_id, payload.actor, payload.reason)


@router.post("/change_orders/{change_order_id}/resubmit", status_code=201)
def resubmit_change_order(change_order_id: int, payload: ChangeOrderResubmit):
    return service().resubmit_change_order(change_order_id, payload.model_dump())
