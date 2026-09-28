from __future__ import annotations


def prepare_temple(client):
    response = client.post(
        "/api/temple/temples",
        json={"code": "shanmen-temple", "name": "山门古寺", "temple_type": "mountain", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 100, "ventilation_capacity": 1000},
    )
    assert response.status_code == 201, response.text


def create_fund(client, code="heritage-fund", total=100000.0):
    response = client.post(
        "/api/temple/procurement/funds",
        json={"temple_code": "shanmen-temple", "code": code, "name": "修缮专项资金", "total_amount": total, "actor": "finance"},
    )
    assert response.status_code == 201, response.text
    return response.json()


ROOF_MATERIALS = [
    {"material_code": "tile-01", "material_name": "青瓦", "material_spec": "240x120", "material_unit": "片", "quantity": 100, "unit_price": 50},
    {"material_code": "wood-01", "material_name": "松木梁", "material_spec": "4m", "material_unit": "根", "quantity": 10, "unit_price": 300},
]
SIGNOFFS = [
    {"signer_role": "curator", "signer": "curator-zhang", "note": "文保负责人同意"},
    {"signer_role": "finance", "signer": "finance-li", "note": "预算复核通过"},
]


def create_package(client, code="roof-pack", budget_cap=10000.0, fund_code="heritage-fund", materials=None, signoffs=None):
    response = client.post(
        "/api/temple/procurement/packages",
        json={
            "temple_code": "shanmen-temple",
            "fund_code": fund_code,
            "code": code,
            "name": "屋面修缮采购包",
            "scope_summary": "大雄宝殿屋面",
            "budget_cap": budget_cap,
            "required_signoffs": ["curator", "finance"] if signoffs is None else signoffs,
            "materials": ROOF_MATERIALS if materials is None else materials,
            "actor": "procurer",
        },
    )
    return response


def freeze_package(client, code="roof-pack", signoffs=None):
    return client.post(f"/api/temple/procurement/packages/{code}/freeze", json={"actor": "procurer", "signoffs": SIGNOFFS if signoffs is None else signoffs})


def frozen_package(client, code="roof-pack", budget_cap=10000.0, fund_code="heritage-fund", materials=None, signoffs=None, setup=True):
    if setup:
        prepare_temple(client)
        if fund_code == "heritage-fund":
            create_fund(client)
    created = create_package(client, code, budget_cap, fund_code, materials, signoffs)
    assert created.status_code == 201, created.text
    frozen = freeze_package(client, code, SIGNOFFS if signoffs is None else signoffs)
    assert frozen.status_code == 200, frozen.text
    return frozen.json()


def create_change(client, code, items, package_code="roof-pack", title="现场变更"):
    response = client.post(
        "/api/temple/procurement/change_orders",
        json={"package_code": package_code, "code": code, "title": title, "items": items, "actor": "site-engineer"},
    )
    return response


def submit_change(client, change_order_id, signoffs=None):
    return client.post(
        f"/api/temple/procurement/change_orders/{change_order_id}/submit",
        json={"actor": "site-engineer", "signoffs": SIGNOFFS if signoffs is None else signoffs},
    )


def test_package_creation_validates_budget_cap_and_duplicates(client):
    prepare_temple(client)
    create_fund(client)
    over_budget = create_package(client, "over-pack", budget_cap=1000.0)
    assert over_budget.status_code == 422
    assert over_budget.json()["error"]["context"]["total"] == 8000.0
    duplicate_materials = [dict(ROOF_MATERIALS[0]), dict(ROOF_MATERIALS[0])]
    bad = create_package(client, "dup-pack", materials=duplicate_materials)
    assert bad.status_code == 422
    ok = create_package(client)
    assert ok.status_code == 201, ok.text
    assert ok.json()["state"] == "draft"
    assert ok.json()["current_version_no"] == 0


def test_freeze_snapshots_version_budget_and_requires_signoffs(client):
    detail = frozen_package(client)
    assert detail["state"] == "frozen"
    assert detail["current_version_no"] == 1
    current = detail["current_version"]
    assert current["version_no"] == 1
    assert current["total_amount"] == 8000.0
    assert detail["committed_amount"] == 8000.0
    assert detail["financials"]["committed_amount"] == 8000.0
    assert detail["financials"]["fund_held"] == 8000.0
    assert detail["financials"]["fund_available"] == 92000.0
    assert {item["signer_role"] for item in detail["freeze_signoffs"]} == {"curator", "finance"}
    # 冻结时的清单快照永久可查；重复编码不能再建。
    assert create_package(client, "roof-pack").status_code == 409
    version = client.get("/api/temple/procurement/packages/roof-pack/versions/1")
    assert version.status_code == 200
    snapshot = {item["material_code"]: item for item in version.json()["manifest"]}
    assert snapshot["tile-01"]["quantity"] == 100
    # 缺签署或多签署都不能冻结。
    create_package(client, "loose-pack")
    missing = client.post("/api/temple/procurement/packages/loose-pack/freeze", json={"actor": "p", "signoffs": SIGNOFFS[:1]})
    assert missing.status_code == 422
    assert missing.json()["error"]["context"]["missing_signoffs"] == ["finance"]
    extra = client.post(
        "/api/temple/procurement/packages/loose-pack/freeze",
        json={"actor": "p", "signoffs": [*SIGNOFFS, {"signer_role": "mayor", "signer": "x"}]},
    )
    assert extra.status_code == 422
    assert extra.json()["error"]["context"]["extra_signoffs"] == ["mayor"]
    # 已冻结不能重复冻结。
    assert freeze_package(client).status_code == 409


def test_freeze_rejected_when_special_fund_insufficient(client):
    prepare_temple(client)
    create_fund(client, code="small-fund", total=5000.0)
    create_package(client, "tight-pack", fund_code="small-fund")
    response = freeze_package(client, "tight-pack")
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "专项资金余额不足以承诺采购包预算"


def test_change_order_flows_add_increase_and_creates_new_version(client):
    frozen_package(client)
    add_paint = {
        "kind": "add", "material_code": "paint-01", "material_name": "矿物涂料", "material_spec": "朱红", "material_unit": "桶",
        "material_unit_price": 100, "quantity": 20, "reason": "现场发现檐口彩绘需要重绘", "impact": "增加檐口作业2天",
    }
    created = create_change(client, "co-add-paint", [add_paint])
    assert created.status_code == 201, created.text
    change_order_id = created.json()["id"]
    assert created.json()["state"] == "draft"
    assert created.json()["budget_delta"] == 2000.0
    # 草稿不占用预算。
    detail = client.get("/api/temple/procurement/packages/roof-pack").json()
    assert detail["financials"]["pending_reserved"] == 0.0
    submitted = submit_change(client, change_order_id)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["state"] == "pending"
    detail = client.get("/api/temple/procurement/packages/roof-pack").json()
    assert detail["financials"]["pending_reserved"] == 2000.0
    assert detail["financials"]["projected_if_all_effective"] == 10000.0
    pending_row = detail["pending_change_orders"][0]
    assert pending_row["budget_delta"] == 2000.0
    assert pending_row["reserved_amount"] == 2000.0
    approved = client.post(f"/api/temple/procurement/change_orders/{change_order_id}/approve", json={"actor": "director", "note": "同意追加"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "effective"
    assert approved.json()["effective_version_no"] == 2
    detail = client.get("/api/temple/procurement/packages/roof-pack").json()
    assert detail["current_version_no"] == 2
    assert detail["committed_amount"] == 10000.0
    assert detail["financials"]["pending_reserved"] == 0.0
    versions = {item["version_no"]: item for item in detail["versions"]}
    assert versions[1]["state"] == "superseded"
    assert versions[2]["state"] == "effective"
    # 被替代版本仍可查，且保留当时的清单与来源变更单。
    v1 = client.get("/api/temple/procurement/packages/roof-pack/versions/1").json()
    assert len(v1["manifest"]) == 2
    v2 = client.get("/api/temple/procurement/packages/roof-pack/versions/2").json()
    assert v2["source_change_order"]["code"] == "co-add-paint"
    paint = next(item for item in detail["materials"] if item["material_code"] == "paint-01")
    assert paint["introduced_version_no"] == 2
    assert paint["introduced_change_order_id"] == change_order_id
    assert paint["last_change_version_no"] == 2


def test_parallel_change_orders_cannot_cross_same_budget_cap(client):
    frozen_package(client)
    first = create_change(client, "co-a", [{
        "kind": "add", "material_code": "paint-01", "material_name": "涂料", "material_unit_price": 100, "quantity": 20,
        "reason": "檐口彩绘", "impact": "+2天",
    }])
    first_id = first.json()["id"]
    assert submit_change(client, first_id).status_code == 200
    # 第二张并行变更叠加后将越过 10000 上限，提交即被拒绝。
    second = create_change(client, "co-b", [{
        "kind": "increase", "material_code": "tile-01", "quantity": 50,
        "material_unit_price": 50, "reason": "破碎率高于预期", "impact": "屋面荷载复核",
    }])
    assert second.status_code == 201
    assert second.json()["budget_delta"] == 2500.0
    denied = submit_change(client, second.json()["id"])
    assert denied.status_code == 409
    assert denied.json()["error"]["context"]["budget_cap"] == 10000.0
    # 第一张生效后，第二张再批准同样会越过新基线。
    approved = client.post(f"/api/temple/procurement/change_orders/{first_id}/approve", json={"actor": "director", "note": "ok"})
    assert approved.status_code == 200
    assert submit_change(client, second.json()["id"]).status_code == 409


def test_special_fund_gate_blocks_submit_when_balance_runs_out(client):
    prepare_temple(client)
    create_fund(client, code="exact-fund", total=8000.0)
    frozen_package(client, fund_code="exact-fund", setup=False)
    order = create_change(client, "co-over-fund", [{
        "kind": "increase", "material_code": "wood-01", "quantity": 1, "material_unit_price": 300,
        "reason": "增补一根梁", "impact": "无影响",
    }])
    response = submit_change(client, order.json()["id"])
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "专项资金余额不足以承诺本次变更"
    detail = client.get("/api/temple/procurement/funds/exact-fund").json()
    assert detail["held_amount"] == 8000.0
    assert detail["available_amount"] == 0.0


def test_special_fund_gate_counts_other_packages_baselines(client):
    prepare_temple(client)
    create_fund(client, code="shared-fund", total=13000.0)
    # 两个采购包共用专项资金：A 冻结 8000，B 冻结 4000，合计 12000。
    frozen_package(client, code="pack-a", fund_code="shared-fund", setup=False)
    b_materials = [{"material_code": "tile-01", "material_name": "青瓦", "material_unit": "片", "quantity": 80, "unit_price": 50}]
    frozen_package(client, code="pack-b", fund_code="shared-fund", materials=b_materials, setup=False)
    order_a = create_change(client, "co-a-plus", [{
        "kind": "add", "material_code": "paint-01", "material_name": "矿物涂料", "material_unit_price": 1000, "quantity": 1,
        "reason": "A包增补檐口涂料", "impact": "无影响",
    }], package_code="pack-a")
    assert submit_change(client, order_a.json()["id"]).status_code == 200
    # B 包再提 +500 时，A 包的待决预占 1000 也必须计入专项资金（合计将达 13500）。
    order_b = create_change(client, "co-b-plus", [{
        "kind": "add", "material_code": "paint-09", "material_name": "涂料", "material_unit_price": 500, "quantity": 1,
        "reason": "B包追加材料", "impact": "无影响",
    }], package_code="pack-b")
    denied = submit_change(client, order_b.json()["id"])
    assert denied.status_code == 409
    assert denied.json()["error"]["message"] == "专项资金余额不足以承诺本次变更"
    # A 包生效后总额变为 9000，B 包 +500 仍会使合计 13500 超过资金 13000。
    approved_a = client.post(f"/api/temple/procurement/change_orders/{order_a.json()['id']}/approve", json={"actor": "director", "note": "ok"})
    assert approved_a.status_code == 200
    new_b = create_change(client, "co-b-retry", [{
        "kind": "add", "material_code": "paint-10", "material_name": "涂料", "material_unit_price": 500, "quantity": 1,
        "reason": "B包追加材料", "impact": "无影响",
    }], package_code="pack-b")
    assert submit_change(client, new_b.json()["id"]).status_code == 409
    fund = client.get("/api/temple/procurement/funds/shared-fund").json()
    assert fund["held_amount"] == 13000.0


def test_accepted_quantity_floor_cannot_be_retroactively_reduced(client):
    frozen_package(client)
    # 验收 80 片青瓦。
    receipt = client.post(
        "/api/temple/procurement/packages/roof-pack/materials/tile-01/receipts",
        json={"actor": "site-keeper", "quantity": 80, "note": "首批到场"},
    )
    assert receipt.status_code == 201, receipt.text
    tile = next(item for item in receipt.json()["materials"] if item["material_code"] == "tile-01")
    assert tile["accepted_qty"] == 80
    # 调减 30 片会使订单数量 70 低于已验收 80，生效模拟阶段即拒绝。
    too_far = create_change(client, "co-cut-too-far", [{
        "kind": "decrease", "material_code": "tile-01", "quantity": 30,
        "material_unit_price": 50, "reason": "设计缩减", "impact": "无影响",
    }])
    assert too_far.status_code == 409
    assert too_far.json()["error"]["context"]["accepted"] == 80
    # 调减 10 片到 90 片可以生效。
    allowed = create_change(client, "co-cut-ok", [{
        "kind": "decrease", "material_code": "tile-01", "quantity": 10,
        "material_unit_price": 50, "reason": "设计微调", "impact": "退还10片",
    }])
    allowed_id = allowed.json()["id"]
    assert allowed.json()["budget_delta"] == -500.0
    submit_change(client, allowed_id)
    approved = client.post(f"/api/temple/procurement/change_orders/{allowed_id}/approve", json={"actor": "director", "note": "ok"})
    assert approved.status_code == 200, approved.text
    detail = client.get("/api/temple/procurement/packages/roof-pack").json()
    tile = next(item for item in detail["materials"] if item["material_code"] == "tile-01")
    assert tile["quantity"] == 90
    assert tile["accepted_qty"] == 80
    assert detail["committed_amount"] == 7500.0
    # 验收超过订单数量不允许。
    overflow = client.post(
        "/api/temple/procurement/packages/roof-pack/materials/tile-01/receipts",
        json={"actor": "site-keeper", "quantity": 20, "note": "超出"},
    )
    assert overflow.status_code == 409
    # 未冻结采购包不允许验收。
    other = create_package(client, "draft-pack")
    assert other.status_code == 201
    denied = client.post(
        "/api/temple/procurement/packages/draft-pack/materials/tile-01/receipts",
        json={"actor": "site-keeper", "quantity": 1, "note": ""},
    )
    assert denied.status_code == 409


def test_substitute_preserves_accepted_floor_and_blocks_receipt_on_old_material(client):
    frozen_package(client)
    client.post(
        "/api/temple/procurement/packages/roof-pack/materials/tile-01/receipts",
        json={"actor": "site-keeper", "quantity": 80, "note": "首批到场"},
    )
    substitution = {
        "kind": "substitute", "material_code": "tile-01", "quantity": 40,
        "substitute_code": "tile-02", "substitute_name": "仿制青瓦", "substitute_spec": "230x115", "substitute_unit": "片", "substitute_unit_price": 45,
        "reason": "原窑口停产，剩余20片改用仿制品", "impact": "外观需文保复验",
    }
    # 缺少替代信息在 schema 层被拒绝。
    bad = client.post(
        "/api/temple/procurement/change_orders",
        json={"package_code": "roof-pack", "code": "co-bad-sub", "title": "坏替代", "items": [{"kind": "substitute", "material_code": "tile-01", "quantity": 1, "reason": "原因说明足够长", "impact": "影响说明足够长"}], "actor": "e"},
    )
    assert bad.status_code == 422
    order = create_change(client, "co-substitute", [substitution])
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]
    submit_change(client, order_id)
    approved = client.post(f"/api/temple/procurement/change_orders/{order_id}/approve", json={"actor": "director", "note": "同意替代"})
    assert approved.status_code == 200, approved.text
    detail = client.get("/api/temple/procurement/packages/roof-pack").json()
    materials = {item["material_code"]: item for item in detail["materials"]}
    assert materials["tile-01"]["quantity"] == 80  # 已验收部分冻结为地板
    assert materials["tile-01"]["accepted_qty"] == 80
    assert materials["tile-01"]["substituted_by_change_order_id"] == order_id
    assert materials["tile-02"]["quantity"] == 40
    assert materials["tile-02"]["introduced_version_no"] == 2
    # 原材料不能再验收，也不能再被变更。
    no_receipt = client.post(
        "/api/temple/procurement/packages/roof-pack/materials/tile-01/receipts",
        json={"actor": "site-keeper", "quantity": 1, "note": ""},
    )
    assert no_receipt.status_code == 409
    no_change = create_change(client, "co-touch-old", [{
        "kind": "increase", "material_code": "tile-01", "quantity": 1, "material_unit_price": 50,
        "reason": "还想调整原材料", "impact": "无影响",
    }])
    assert no_change.status_code == 409
    # 被替代前的 v1 清单仍记录青瓦 100 片。
    v1 = client.get("/api/temple/procurement/packages/roof-pack/versions/1").json()
    assert next(item for item in v1["manifest"] if item["material_code"] == "tile-01")["quantity"] == 100


def test_reject_withdraw_release_reservation_and_resubmit_keeps_relation(client):
    frozen_package(client)
    rejected_order = create_change(client, "co-reject-me", [{
        "kind": "increase", "material_code": "wood-01", "quantity": 2, "material_unit_price": 300,
        "reason": "梁架糟朽", "impact": "+600",
    }])
    rejected_id = rejected_order.json()["id"]
    submit_change(client, rejected_id)
    detail = client.get("/api/temple/procurement/packages/roof-pack").json()
    assert detail["financials"]["pending_reserved"] == 600.0
    reject = client.post(f"/api/temple/procurement/change_orders/{rejected_id}/reject", json={"actor": "director", "note": "资金安排不妥"})
    assert reject.status_code == 200
    assert reject.json()["state"] == "rejected"
    detail = client.get("/api/temple/procurement/packages/roof-pack").json()
    assert detail["financials"]["pending_reserved"] == 0.0
    # 驳回后重提，关系与修订号保留。
    resubmit = client.post(
        f"/api/temple/procurement/change_orders/{rejected_id}/resubmit",
        json={"code": "co-retry", "actor": "site-engineer"},
    )
    assert resubmit.status_code == 201, resubmit.text
    new_id = resubmit.json()["id"]
    assert resubmit.json()["revision_no"] == 2
    assert resubmit.json()["resubmits"]["code"] == "co-reject-me"
    assert {item["code"] for item in resubmit.json()["revision_chain"]} == {"co-reject-me", "co-retry"}
    # 撤回流程同样释放预占。
    submit_change(client, new_id)
    withdraw = client.post(f"/api/temple/procurement/change_orders/{new_id}/withdraw", json={"actor": "site-engineer", "reason": "现场重新核算"})
    assert withdraw.status_code == 200
    assert withdraw.json()["state"] == "withdrawn"
    detail = client.get("/api/temple/procurement/packages/roof-pack").json()
    assert detail["financials"]["pending_reserved"] == 0.0
    # 已生效的变更单不能驳回或撤回。
    effective = create_change(client, "co-final", [{
        "kind": "increase", "material_code": "wood-01", "quantity": 1, "material_unit_price": 300,
        "reason": "一根替换", "impact": "无影响",
    }])
    effective_id = effective.json()["id"]
    submit_change(client, effective_id)
    client.post(f"/api/temple/procurement/change_orders/{effective_id}/approve", json={"actor": "director", "note": "ok"})
    assert client.post(f"/api/temple/procurement/change_orders/{effective_id}/withdraw", json={"actor": "e", "reason": "x"}).status_code == 409


def test_submit_requires_signoffs_and_only_frozen_package_allows_changes(client):
    frozen_package(client)
    create_package(client, "draft-pack")
    draft_change = client.post(
        "/api/temple/procurement/change_orders",
        json={"package_code": "draft-pack", "code": "co-on-draft", "title": "草稿包变更", "items": [{
            "kind": "increase", "material_code": "wood-01", "quantity": 1, "material_unit_price": 300, "reason": "原因足够", "impact": "影响足够",
        }], "actor": "e"},
    )
    assert draft_change.status_code == 409
    order = create_change(client, "co-needs-signoff", [{
        "kind": "increase", "material_code": "wood-01", "quantity": 1, "material_unit_price": 300,
        "reason": "原因足够", "impact": "影响足够",
    }])
    missing = submit_change(client, order.json()["id"], signoffs=[])
    assert missing.status_code == 422
    assert missing.json()["error"]["context"]["missing_signoffs"] == ["curator", "finance"]


def test_package_close_blocked_with_pending_change_orders(client):
    frozen_package(client)
    order = create_change(client, "co-pending-close", [{
        "kind": "increase", "material_code": "wood-01", "quantity": 1, "material_unit_price": 300,
        "reason": "原因足够", "impact": "影响足够",
    }])
    submit_change(client, order.json()["id"])
    blocked = client.post("/api/temple/procurement/packages/roof-pack/close", json={"actor": "director", "reason": "完工"})
    assert blocked.status_code == 409
    client.post(f"/api/temple/procurement/change_orders/{order.json()['id']}/withdraw", json={"actor": "e", "reason": "x"})
    closed = client.post("/api/temple/procurement/packages/roof-pack/close", json={"actor": "director", "reason": "完工"})
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"
