from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.database import get_connection
from app.temple.procurement import ProcurementService

FROZEN_NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


def prepare_temple(client):
    client.post(
        "/api/temple/temples",
        json={"code": "shanmen-temple", "name": "山门古寺", "temple_type": "mountain", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 100, "ventilation_capacity": 1000},
    )
    for sequence, code in enumerate(("east", "west"), start=1):
        client.post(
            "/api/temple/temples/shanmen-temple/halls",
            json={"code": code, "name": f"{code}-hall", "visit_order": sequence, "expected_visit_seconds": 600, "ventilation_capacity": 400},
        )


def package_payload(**overrides):
    payload = {
        "temple_code": "shanmen-temple",
        "code": "roof-package",
        "name": "屋面修缮采购包",
        "budget_limit": 10000,
        "required_signatures": ["project_manager", "finance", "heritage_officer"],
        "materials": [
            {"material_code": "tile-grey", "name": "青瓦", "unit": "块", "spec": "240x120", "quantity": 100, "unit_price": 20},
            {"material_code": "timber-nanmu", "name": "楠木枋", "unit": "立方米", "spec": "一等", "quantity": 5, "unit_price": 1000},
        ],
        "actor": "operator",
    }
    payload.update(overrides)
    return payload


def freeze_package(client, payload=None):
    payload = payload or package_payload()
    created = client.post("/api/temple/operations/procurement/packages", json=payload)
    assert created.status_code == 201, created.text
    package_id = created.json()["id"]
    frozen = client.post(f"/api/temple/operations/procurement/packages/{package_id}/freeze", json={"actor": "manager"})
    assert frozen.status_code == 200, frozen.text
    return frozen.json()


def sign_and_submit(client, change_order_id, *, committed=0, fund="FUND-1", actor="operator"):
    for role, signer in (("project_manager", "pm"), ("finance", "cfo"), ("heritage_officer", "officer")):
        response = client.post(
            f"/api/temple/operations/procurement/change_orders/{change_order_id}/signatures",
            json={"signer_role": role, "signer": signer},
        )
        assert response.status_code in (201, 409), response.text
        if response.status_code == 409:
            assert response.json()["error"]["message"] == "该角色已经签署"
    return client.post(
        f"/api/temple/operations/procurement/change_orders/{change_order_id}/submit",
        json={"actor": actor, "committed_fund_amount": committed, "fund_reference": fund},
    )


def test_package_freezes_budget_and_baseline_snapshot(client):
    prepare_temple(client)
    package = freeze_package(client)
    assert package["state"] == "frozen"
    assert package["budget"]["active_total"] == 7000.0
    assert package["budget"]["budget_limit"] == 10000.0
    assert package["budget"]["available_headroom"] == 3000.0
    assert package["active_version"]["version_no"] == 1
    assert {line["material_code"] for line in package["lines"]} == {"tile-grey", "timber-nanmu"}
    # 冻结后不能再改清单
    denied = client.put(
        f"/api/temple/operations/procurement/packages/{package['id']}/lines",
        json={"material_code": "tile-grey", "name": "青瓦", "unit": "块", "quantity": 999, "unit_price": 20, "actor": "operator"},
    )
    assert denied.status_code == 409
    # 超过预算上限不能冻结
    over = package_payload(code="over-budget", budget_limit=100)
    created = client.post("/api/temple/operations/procurement/packages", json=over)
    assert created.status_code == 201
    failed = client.post(f"/api/temple/operations/procurement/packages/{created.json()['id']}/freeze", json={"actor": "manager"})
    assert failed.status_code == 409
    assert failed.json()["error"]["context"]["budget_total"] == 7000.0


def test_draft_lines_can_be_edited_before_freeze(client):
    prepare_temple(client)
    created = client.post("/api/temple/operations/procurement/packages", json=package_payload(budget_limit=50000)).json()
    package_id = created["id"]
    upsert = client.put(
        f"/api/temple/operations/procurement/packages/{package_id}/lines",
        json={"material_code": "tile-grey", "name": "青瓦加厚", "unit": "块", "spec": "260x130", "quantity": 200, "unit_price": 25, "actor": "operator"},
    )
    assert upsert.status_code == 200
    tile = [line for line in upsert.json()["lines"] if line["material_code"] == "tile-grey"][0]
    assert tile["quantity"] == 200 and tile["unit_price"] == 25 and tile["line_amount"] == 5000.0
    added = client.put(
        f"/api/temple/operations/procurement/packages/{package_id}/lines",
        json={"material_code": "lime-putty", "name": "桐油灰", "unit": "千克", "quantity": 50, "unit_price": 12, "actor": "operator"},
    )
    assert added.status_code == 200
    removed = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/lines/timber-nanmu/remove",
        json={"actor": "operator"},
    )
    assert removed.status_code == 200
    assert {line["material_code"] for line in removed.json()["lines"]} == {"tile-grey", "lime-putty"}


def test_change_order_requires_signatures_fund_and_takes_effect(client):
    prepare_temple(client)
    package = freeze_package(client)
    package_id = package["id"]
    created = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/change_orders",
        json={
            "code": "co-001", "title": "增加青瓦与桐油灰", "reason": "现场发现屋面残损超出预期",
            "impact": "增加预算 1500 元，工期不变",
            "items": [
                {"change_kind": "addition", "material_code": "lime-putty", "name": "桐油灰", "unit": "千克", "quantity": 50, "unit_price": 12, "reason": "封缝需要"},
                {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": 45, "reason": "补充碎裂瓦片"},
            ],
            "actor": "operator",
        },
    )
    assert created.status_code == 201, created.text
    detail = created.json()
    # 50*12 + 45*20 = 600 + 900 = 1500
    assert detail["net_delta_amount"] == 1500.0
    assert detail["validation"]["ready_to_submit"] is False
    # 资金不足或缺少批文号不能提交
    signed = sign_and_submit(client, detail["id"], committed=1000, fund="FUND-1")
    assert signed.status_code == 409
    assert signed.json()["error"]["context"]["required_commitment"] == 1500.0
    # 签署不齐不能提交
    unsigned = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/change_orders",
        json={"code": "co-unsigned", "title": "缺签署", "items": [
            {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": 1},
        ], "actor": "operator"},
    )
    bad_submit = client.post(
        f"/api/temple/operations/procurement/change_orders/{unsigned.json()['id']}/submit",
        json={"actor": "operator"},
    )
    assert bad_submit.status_code == 409
    assert set(bad_submit.json()["error"]["context"]["missing"]) == {"project_manager", "finance", "heritage_officer"}
    # 齐备后提交 -> 待决
    submitted = sign_and_submit(client, detail["id"], committed=1500, fund="FUND-2026-001")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["state"] == "submitted"
    detail_after = client.get(f"/api/temple/operations/procurement/packages/{package_id}").json()
    assert detail_after["budget"]["pending_delta"] == 1500.0
    assert detail_after["budget"]["projected_total"] == 8500.0
    assert [item["code"] for item in detail_after["budget"]["pending_change_orders"]] == ["co-001"]
    # 批准生效生成 v2，v1 仍可查
    approved = client.post(f"/api/temple/operations/procurement/change_orders/{detail['id']}/approve", json={"actor": "director", "note": "同意"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "effective"
    package_after = client.get(f"/api/temple/operations/procurement/packages/{package_id}").json()
    assert package_after["active_version"]["version_no"] == 2
    assert package_after["budget"]["active_total"] == 8500.0
    assert package_after["budget"]["pending_delta"] == 0.0
    codes = {line["material_code"]: line for line in package_after["lines"]}
    assert codes["tile-grey"]["quantity"] == 145
    assert codes["lime-putty"]["source"]["change_order_code"] == "co-001"
    assert codes["tile-grey"]["source"]["predecessor_line"]["version_no"] == 1
    versions = client.get(f"/api/temple/operations/procurement/packages/{package_id}/versions").json()["items"]
    assert [v["version_no"] for v in versions] == [1, 2]
    assert [v["state"] for v in versions] == ["superseded", "active"]
    v1 = client.get(f"/api/temple/operations/procurement/package_versions/{versions[0]['id']}").json()
    assert {line["material_code"] for line in v1["lines"]} == {"tile-grey", "timber-nanmu"}


def test_two_parallel_change_orders_cannot_cross_budget_cap(client):
    prepare_temple(client)
    package = freeze_package(client)  # active 7000, limit 10000, headroom 3000
    package_id = package["id"]

    def make_order(code, delta):
        response = client.post(
            f"/api/temple/operations/procurement/packages/{package_id}/change_orders",
            json={"code": code, "title": code, "items": [
                {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": delta // 20, "reason": "加价"},
            ], "actor": "operator"},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    first = make_order("co-parallel-1", 2000)   # +100 块 = 2000
    second = make_order("co-parallel-2", 2000)  # +100 块 = 2000
    assert sign_and_submit(client, first, committed=2000, fund="F1").status_code == 200
    # 第二张与第一张并行：7000 + 2000(预留) + 2000 = 11000 > 10000
    blocked = sign_and_submit(client, second, committed=2000, fund="F2")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["projected_total"] == 11000.0
    # 第一张批准后，第二张基线版本过期，不能直接批准
    approved = client.post(f"/api/temple/operations/procurement/change_orders/{first}/approve", json={"actor": "director", "note": "ok"})
    assert approved.status_code == 200
    # 第二张仍为草稿（提交失败未占用），且其基线版本已失效
    second_detail = client.get(f"/api/temple/operations/procurement/change_orders/{second}").json()
    assert "基线版本已被其他变更替代" in "".join(second_detail["validation"]["issues"])


def test_pending_reduction_does_not_release_headroom(client):
    prepare_temple(client)
    package = freeze_package(client)  # active 7000, limit 10000, headroom 3000
    package_id = package["id"]

    def make_order(code, *, delta_qty, substitute=False):
        if substitute:
            items = [{"change_kind": "substitution", "material_code": "timber-nanmu",
                      "target_material_code": "timber-pine", "target_name": "松木枋", "target_unit": "立方米",
                      "target_quantity": 5, "target_unit_price": 800, "reason": "替代"}]
        else:
            items = [{"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": delta_qty, "reason": "x"}]
        response = client.post(
            f"/api/temple/operations/procurement/packages/{package_id}/change_orders",
            json={"code": code, "title": code, "items": items, "actor": "operator"},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    # 待决减支单：楠木 5000 -> 松木 4000，净 -1000
    reduction = make_order("co-cut", delta_qty=0, substitute=True)
    assert sign_and_submit(client, reduction, committed=0, fund="F-CUT").status_code == 200
    detail = client.get(f"/api/temple/operations/procurement/packages/{package_id}").json()
    assert detail["budget"]["pending_delta"] == -1000.0
    assert detail["budget"]["reserved_increase"] == 0.0
    # 增支 3000：active 7000 + 增支 3000 = 10000，刚好等于上限，应允许（减支未生效不预支其空间）
    at_limit = make_order("co-at-limit", delta_qty=150)  # 150*20=3000
    assert sign_and_submit(client, at_limit, committed=3000, fund="F-ADD").status_code == 200
    # 再增 20 就超限：7000 + 3000(预留) + 400 = 10400
    over = make_order("co-over", delta_qty=20)
    blocked = sign_and_submit(client, over, committed=400, fund="F-OVER")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["reserved_by_pending"] == 3000.0
    assert blocked.json()["error"]["context"]["projected_total"] == 10400.0


def test_accepted_quantity_cannot_be_retroactively_reduced(client):
    prepare_temple(client)
    package = freeze_package(client)
    package_id = package["id"]
    # 按 v1 验收 120 块青瓦（分两次，超过订单数量也允许？此处按订单内验收 80 块）
    first_receipt = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/receipts",
        json={"material_code": "tile-grey", "quantity": 80, "reference": "receipt-1", "actor": "site"},
    )
    assert first_receipt.status_code == 201, first_receipt.text
    second_receipt = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/receipts",
        json={"material_code": "tile-grey", "quantity": 20, "reference": "receipt-2", "actor": "site"},
    )
    assert second_receipt.status_code == 201
    # 变更试图把青瓦从 100 减到 90（已验收 100）
    created = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/change_orders",
        json={"code": "co-reduce", "title": "缩减青瓦", "items": [
            {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": -10, "reason": "估错"},
        ], "actor": "operator"},
    )
    change_order_id = created.json()["id"]
    assert sign_and_submit(client, change_order_id, committed=0, fund="FUND-X").status_code == 200
    rejected = client.post(f"/api/temple/operations/procurement/change_orders/{change_order_id}/approve", json={"actor": "director", "note": "试批"})
    assert rejected.status_code == 409
    violations = rejected.json()["error"]["context"]["violations"]
    assert violations == [{"material_code": "tile-grey", "accepted_quantity": 100.0, "target_quantity": 90.0}]
    # 移除已验收材料同样被阻止
    remove = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/change_orders",
        json={"code": "co-remove", "title": "移除青瓦", "items": [
            {"change_kind": "removal", "material_code": "tile-grey", "reason": "不用了"},
        ], "actor": "operator"},
    )
    assert sign_and_submit(client, remove.json()["id"], committed=0, fund="FUND-X2").status_code == 200
    remove_blocked = client.post(f"/api/temple/operations/procurement/change_orders/{remove.json()['id']}/approve", json={"actor": "director", "note": "xx"})
    assert remove_blocked.status_code == 409
    assert remove_blocked.json()["error"]["context"]["violations"][0]["material_code"] == "tile-grey"
    # 当前版本数量与已验收量仍然可查
    detail = client.get(f"/api/temple/operations/procurement/packages/{package_id}").json()
    tile = [line for line in detail["lines"] if line["material_code"] == "tile-grey"][0]
    assert tile["accepted_quantity"] == 100.0
    receipts = client.get(f"/api/temple/operations/procurement/packages/{package_id}/receipts").json()["items"]
    assert len(receipts) == 2


def test_substitution_preserves_lineage(client):
    prepare_temple(client)
    package = freeze_package(client)
    package_id = package["id"]
    created = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/change_orders",
        json={"code": "co-sub", "title": "楠木替代为松木枋", "reason": "楠木供应中断", "impact": "需文保确认", "items": [
            {"change_kind": "substitution", "material_code": "timber-nanmu",
             "target_material_code": "timber-songmu", "target_name": "松木枋", "target_unit": "立方米", "target_spec": "二等",
             "target_quantity": 6, "target_unit_price": 600, "reason": "等强度替代"},
        ], "actor": "operator"},
    )
    change_order_id = created.json()["id"]
    # 6*600 - 5*1000 = 3600 - 5000 = -1400
    assert created.json()["net_delta_amount"] == -1400.0
    submitted = sign_and_submit(client, change_order_id, committed=0, fund="FUND-SUB")
    assert submitted.status_code == 200
    approved = client.post(f"/api/temple/operations/procurement/change_orders/{change_order_id}/approve", json={"actor": "director", "note": "同意替代"})
    assert approved.status_code == 200
    detail = client.get(f"/api/temple/operations/procurement/packages/{package_id}").json()
    codes = {line["material_code"]: line for line in detail["lines"]}
    assert "timber-nanmu" not in codes
    songmu = codes["timber-songmu"]
    assert songmu["source"]["line_kind"] == "substitution"
    assert songmu["source"]["change_order_code"] == "co-sub"
    assert songmu["source"]["predecessor_line"]["material_code"] == "timber-nanmu"
    assert songmu["source"]["predecessor_line"]["version_no"] == 1


def test_reject_withdraw_and_resubmit_keep_relation(client):
    prepare_temple(client)
    package = freeze_package(client)
    package_id = package["id"]
    created = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/change_orders",
        json={"code": "co-rev", "title": "加价", "items": [
            {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": 10, "reason": "补量"},
        ], "actor": "operator"},
    )
    change_order_id = created.json()["id"]
    assert sign_and_submit(client, change_order_id, committed=200, fund="FUND-R").status_code == 200
    rejected = client.post(f"/api/temple/operations/procurement/change_orders/{change_order_id}/reject", json={"actor": "director", "note": "依据不足"})
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"
    # 重提形成修订链
    resubmit = client.post(
        f"/api/temple/operations/procurement/change_orders/{change_order_id}/resubmit",
        json={"code": "co-rev-r2", "reason": "补充现场照片", "items": [
            {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": 5, "reason": "复核后补量"},
        ], "actor": "operator"},
    )
    assert resubmit.status_code == 201, resubmit.text
    new_id = resubmit.json()["id"]
    assert new_id != change_order_id
    assert resubmit.json()["revision_of"] == {"id": change_order_id, "code": "co-rev", "state": "rejected", "revision_seq": 1}
    assert resubmit.json()["revision_seq"] == 2
    # 原单可查回到新单
    original = client.get(f"/api/temple/operations/procurement/change_orders/{change_order_id}").json()
    assert [item["code"] for item in original["revisions"]] == ["co-rev-r2"]
    # 提交后撤回，再重提
    assert sign_and_submit(client, new_id, committed=100, fund="FUND-R2").status_code == 200
    withdrawn = client.post(f"/api/temple/operations/procurement/change_orders/{new_id}/withdraw", json={"actor": "operator", "note": "需修改"})
    assert withdrawn.status_code == 200
    assert withdrawn.json()["state"] == "withdrawn"
    again = client.post(
        f"/api/temple/operations/procurement/change_orders/{new_id}/resubmit",
        json={"code": "co-rev-r3", "items": [
            {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": 2, "reason": "再次复核"},
        ], "actor": "operator"},
    )
    assert again.status_code == 201
    chain = again.json()["revision_of"]
    assert chain["code"] == "co-rev-r2"
    assert again.json()["revision_seq"] == 3
    # 驳回的单子不能批准
    bad_approve = client.post(f"/api/temple/operations/procurement/change_orders/{change_order_id}/approve", json={"actor": "director", "note": "xx"})
    assert bad_approve.status_code == 409


def test_change_order_validation_rules(client):
    prepare_temple(client)
    package = freeze_package(client)
    package_id = package["id"]

    def post(items, code="co-x", **kw):
        payload = {"code": code, "title": "测试变更", "items": items, "actor": "operator"}
        payload.update(kw)
        return client.post(f"/api/temple/operations/procurement/packages/{package_id}/change_orders", json=payload)

    # 新增已存在的材料
    response = post([{"change_kind": "addition", "material_code": "tile-grey", "name": "青瓦", "unit": "块", "quantity": 1, "unit_price": 1}], code="dup-add")
    assert response.status_code == 422
    # 调整不存在的材料
    response = post([{"change_kind": "adjustment", "material_code": "missing", "quantity_delta": 1}], code="adj-missing")
    assert response.status_code == 422
    # 调整后数量为负
    response = post([{"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": -9999}], code="neg")
    assert response.status_code == 422
    # 替代目标与原材料相同
    response = post([{"change_kind": "substitution", "material_code": "tile-grey", "target_material_code": "tile-grey",
                      "target_name": "x", "target_unit": "块", "target_quantity": 1}], code="self-sub")
    assert response.status_code == 422
    # 同一材料在一张单中出现两次（schema 层）
    response = post([
        {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": 1},
        {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": 2},
    ], code="twice")
    assert response.status_code == 422


def test_project_detail_shows_commitment_pending_and_material_sources(client):
    prepare_temple(client)
    from app.temple.rules import DEFAULT_RULES
    policy = client.post("/api/temple/temples/shanmen-temple/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    client.post(f"/api/temple/policies/{policy['id']}/publish", json={"actor": "tests", "effective_from": "2026-09-01T00:00:00Z"})
    campaign = client.post(
        "/api/temple/operations/restoration_campaigns",
        json={"temple_code": "shanmen-temple", "safety_policy_id": policy["id"], "code": "roof-campaign", "name": "屋面修缮", "strategy": "halls", "hall_codes": ["east"], "actor": "operator"},
    ).json()
    package = freeze_package(client, package_payload(restoration_campaign_code="roof-campaign"))
    package_id = package["id"]
    # 验收一部分
    client.post(f"/api/temple/operations/procurement/packages/{package_id}/receipts", json={"material_code": "tile-grey", "quantity": 30, "actor": "site"})
    # 提一张待决变更
    created = client.post(
        f"/api/temple/operations/procurement/packages/{package_id}/change_orders",
        json={"code": "co-project", "title": "项目加价", "items": [
            {"change_kind": "adjustment", "material_code": "tile-grey", "quantity_delta": 50, "reason": "现场扩面"},
        ], "actor": "operator"},
    )
    sign_and_submit(client, created.json()["id"], committed=1000, fund="FUND-PJ")
    detail = client.get(f"/api/temple/operations/restoration_campaigns/{campaign['id']}").json()
    summary = detail["procurement"]
    assert summary["committed_total"] == 7000.0
    assert summary["pending_impact_total"] == 1000.0
    assert summary["projected_committed_total"] == 8000.0
    assert summary["accepted_total"] == 600.0
    pkg = summary["packages"][0]
    tile = [m for m in pkg["materials"] if m["material_code"] == "tile-grey"][0]
    assert tile["accepted_quantity"] == 30.0
    assert tile["source"]["version_no"] == 1


def test_service_with_frozen_clock_and_direct_connection(client):
    prepare_temple(client)
    connection = get_connection()
    service = ProcurementService(connection, FrozenClock(FROZEN_NOW))
    detail = service.create_package(package_payload(code="clock-package"))
    frozen = service.freeze_package(detail["id"], "manager")
    assert frozen["frozen_at"] == "2026-09-28T08:00:00+00:00"
    orders = service.create_change_order(frozen["id"], {
        "code": "co-clock", "title": "t", "items": [
            {"change_kind": "addition", "material_code": "new-mat", "name": "新材料", "unit": "个", "quantity": 1, "unit_price": 10},
        ], "actor": "operator",
    })
    for role, signer in (("project_manager", "pm"), ("finance", "cfo"), ("heritage_officer", "officer")):
        service.add_signature(orders["id"], {"signer_role": role, "signer": signer})
    service.submit_change_order(orders["id"], {"actor": "operator", "committed_fund_amount": 10, "fund_reference": "F"})
    effective = service.approve_change_order(orders["id"], "director", "ok")
    assert effective["state"] == "effective"
    assert service.package_detail(frozen["id"])["active_version"]["version_no"] == 2
