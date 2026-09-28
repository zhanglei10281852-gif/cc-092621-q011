from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.temple.repository import TempleRepository
from app.temple.schema import ensure_temple_schema

_MONEY_EPSILON = 0.005


def money(value: float) -> float:
    return round(float(value) + 0.0, 2)


class ProcurementService:
    """采购包冻结、版本快照、验收与变更单管理。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = TempleRepository(self.connection)

    # ------------------------------------------------------------------ 采购包

    def create_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        restoration_campaign_id = None
        if payload.get("restoration_campaign_code"):
            campaign = self.connection.execute(
                "SELECT id FROM restoration_campaigns WHERE temple_id=? AND code=?",
                (temple["id"], payload["restoration_campaign_code"]),
            ).fetchone()
            if campaign is None:
                raise NotFoundError("关联修缮活动不存在")
            restoration_campaign_id = campaign["id"]
        required = list(payload.get("required_signatures") or [])
        if len(required) != len(set(required)):
            raise ValidationError("必需签署角色不能重复")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO procurement_packages(temple_id,restoration_campaign_id,code,name,currency,budget_limit,required_signatures_json,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (temple["id"], restoration_campaign_id, payload["code"], payload["name"], payload.get("currency") or "CNY", money(payload["budget_limit"]), json.dumps(required, ensure_ascii=False), payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("采购包编码已存在") from exc
            package_id = cursor.lastrowid
            version_id = connection.execute(
                "INSERT INTO procurement_package_versions(package_id,version_no,state,budget_total,created_at) VALUES(?,1,'draft',0,?)",
                (package_id, now),
            ).lastrowid
            for item in payload.get("materials") or []:
                self._upsert_draft_line(connection, package_id, version_id, item, now)
            total = self._version_total(connection, version_id)
            connection.execute("UPDATE procurement_package_versions SET budget_total=? WHERE id=?", (total, version_id))
            self._event(connection, "procurement_package", package_id, "created", payload["actor"], {"materials": len(payload.get("materials") or [])}, now)
            return self.package_detail(package_id, connection)

    def list_packages(self, temple_code: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            clauses.append("n.code=?")
            params.append(temple_code)
        if state:
            clauses.append("p.state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT p.*,n.code AS temple_code,n.name AS temple_name FROM procurement_packages p JOIN temple_sites n ON n.id=p.temple_id" + where + " ORDER BY p.id DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_draft_line(self, package_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        package = self._package(package_id)
        if package["state"] != "draft":
            raise ConflictError("只有草稿采购包可以直接调整清单，冻结后请使用变更单")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            version = self._draft_version(connection, package_id)
            self._upsert_draft_line(connection, package_id, version["id"], payload, now)
            total = self._version_total(connection, version["id"])
            connection.execute("UPDATE procurement_package_versions SET budget_total=? WHERE id=?", (total, version["id"]))
            connection.execute("UPDATE procurement_packages SET updated_at=? WHERE id=?", (now, package_id))
            self._event(connection, "procurement_package", package_id, "draft_line_updated", payload["actor"], {"material_code": payload["material_code"]}, now)
            return self.package_detail(package_id, connection)

    def remove_draft_line(self, package_id: int, material_code: str, actor: str) -> dict[str, Any]:
        package = self._package(package_id)
        if package["state"] != "draft":
            raise ConflictError("只有草稿采购包可以直接调整清单，冻结后请使用变更单")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            version = self._draft_version(connection, package_id)
            material = connection.execute(
                "SELECT m.id FROM procurement_materials m JOIN procurement_package_lines l ON l.material_id=m.id WHERE l.package_version_id=? AND m.material_code=?",
                (version["id"], material_code),
            ).fetchone()
            if material is None:
                raise NotFoundError("清单中没有该材料")
            connection.execute("DELETE FROM procurement_package_lines WHERE package_version_id=? AND material_id=?", (version["id"], material["id"]))
            total = self._version_total(connection, version["id"])
            connection.execute("UPDATE procurement_package_versions SET budget_total=? WHERE id=?", (total, version["id"]))
            connection.execute("UPDATE procurement_packages SET updated_at=? WHERE id=?", (now, package_id))
            self._event(connection, "procurement_package", package_id, "draft_line_removed", actor, {"material_code": material_code}, now)
            return self.package_detail(package_id, connection)

    def freeze_package(self, package_id: int, actor: str) -> dict[str, Any]:
        package = self._package(package_id)
        if package["state"] != "draft":
            raise ConflictError("只有草稿采购包可以冻结")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            version = self._draft_version(connection, package_id)
            lines = connection.execute("SELECT * FROM procurement_package_lines WHERE package_version_id=?", (version["id"],)).fetchall()
            if not lines:
                raise ValidationError("采购清单为空，不能冻结")
            total = self._version_total(connection, version["id"])
            if total > money(package["budget_limit"]) + _MONEY_EPSILON:
                raise ConflictError("冻结预算总额超过采购包预算上限", context={"budget_total": total, "budget_limit": money(package["budget_limit"])})
            connection.execute(
                "UPDATE procurement_package_versions SET state='active',budget_total=?,activated_at=? WHERE id=?",
                (total, now, version["id"]),
            )
            connection.execute(
                "UPDATE procurement_packages SET state='frozen',frozen_at=?,frozen_by=?,updated_at=? WHERE id=?",
                (now, actor, now, package_id),
            )
            self._event(connection, "procurement_package", package_id, "frozen", actor, {"version_no": 1, "budget_total": total}, now)
            return self.package_detail(package_id, connection)

    def package_detail(self, package_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        package = connection.execute(
            "SELECT p.*,n.code AS temple_code,n.name AS temple_name,c.code AS restoration_campaign_code FROM procurement_packages p "
            "JOIN temple_sites n ON n.id=p.temple_id LEFT JOIN restoration_campaigns c ON c.id=p.restoration_campaign_id WHERE p.id=?",
            (package_id,),
        ).fetchone()
        if package is None:
            raise NotFoundError("采购包不存在")
        result = dict(package)
        result["required_signatures"] = json.loads(result.pop("required_signatures_json"))
        active = connection.execute("SELECT * FROM procurement_package_versions WHERE package_id=? AND state='active'", (package_id,)).fetchone()
        active_total = money(active["budget_total"]) if active else 0.0
        pending_rows = connection.execute(
            "SELECT id,code,title,state,net_delta_amount,committed_fund_amount,fund_reference FROM change_orders WHERE package_id=? AND state='submitted' ORDER BY id",
            (package_id,),
        ).fetchall()
        pending_items = [dict(row) for row in pending_rows]
        pending_delta = money(sum(row["net_delta_amount"] for row in pending_rows))
        reserved_increase = money(sum(max(0.0, row["net_delta_amount"]) for row in pending_rows))
        result["budget"] = {
            "budget_limit": money(package["budget_limit"]),
            "active_total": active_total,
            "pending_change_orders": pending_items,
            "pending_delta": pending_delta,
            "reserved_increase": reserved_increase,
            "projected_total": money(active_total + reserved_increase),
            "available_headroom": money(money(package["budget_limit"]) - active_total - reserved_increase),
        }
        result["active_version"] = self._version_header(connection, active["id"]) if active else None
        if active:
            result["lines"] = self._lines_for_version(connection, package_id, active["id"])
        else:
            draft = connection.execute("SELECT id FROM procurement_package_versions WHERE package_id=? AND state='draft' ORDER BY version_no LIMIT 1", (package_id,)).fetchone()
            result["lines"] = self._lines_for_version(connection, package_id, draft["id"]) if draft else []
        result["versions"] = [self._version_header(connection, row["id"]) for row in connection.execute(
            "SELECT id FROM procurement_package_versions WHERE package_id=? ORDER BY version_no", (package_id,)
        ).fetchall()]
        result["events"] = self._events(connection, "procurement_package", package_id)
        return result

    def list_versions(self, package_id: int) -> list[dict[str, Any]]:
        self._package(package_id)
        rows = self.connection.execute("SELECT id FROM procurement_package_versions WHERE package_id=? ORDER BY version_no", (package_id,)).fetchall()
        return [self.version_detail(row["id"]) for row in rows]

    def version_detail(self, version_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM procurement_package_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("采购包版本不存在")
        result = self._version_header(self.connection, version_id)
        result["lines"] = [dict(line) for line in self.connection.execute(
            "SELECT * FROM procurement_package_lines WHERE package_version_id=? ORDER BY material_code,id",
            (version_id,),
        ).fetchall()]
        return result

    # ------------------------------------------------------------------ 验收

    def record_receipt(self, package_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        package = self._package(package_id)
        if package["state"] != "frozen":
            raise ConflictError("只有已冻结的采购包可以登记验收")
        if payload["quantity"] <= 0:
            raise ValidationError("验收数量必须大于零")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            active_version = self._active_version(connection, package_id)
            line = connection.execute(
                "SELECT l.*,m.id AS material_id FROM procurement_package_lines l JOIN procurement_materials m ON m.id=l.material_id "
                "WHERE l.package_version_id=? AND m.material_code=?",
                (active_version["id"], payload["material_code"]),
            ).fetchone()
            if line is None:
                raise NotFoundError("当前生效版本中没有该材料")
            unit_price = money(payload.get("unit_price") if payload.get("unit_price") is not None else line["unit_price"])
            if unit_price < 0:
                raise ValidationError("验收单价不能为负")
            cursor = connection.execute(
                "INSERT INTO procurement_receipts(package_id,material_id,material_code,quantity,unit_price,source_version_id,source_line_id,reference,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (package_id, line["material_id"], payload["material_code"], float(payload["quantity"]), unit_price, active_version["id"], line["id"], payload.get("reference") or "", payload["actor"], now),
            )
            receipt = dict(connection.execute("SELECT * FROM procurement_receipts WHERE id=?", (cursor.lastrowid,)).fetchone())
            self._event(connection, "procurement_package", package_id, "received", payload["actor"], {"material_code": payload["material_code"], "quantity": payload["quantity"]}, now)
            return receipt

    def list_receipts(self, package_id: int) -> list[dict[str, Any]]:
        self._package(package_id)
        rows = self.connection.execute("SELECT * FROM procurement_receipts WHERE package_id=? ORDER BY id", (package_id,)).fetchall()
        return [dict(row) for row in rows]

    def campaign_procurement_summary(self, restoration_campaign_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        """修缮活动（项目）维度的采购承诺汇总。"""
        connection = connection or self.connection
        packages = connection.execute(
            "SELECT id FROM procurement_packages WHERE restoration_campaign_id=? ORDER BY id",
            (restoration_campaign_id,),
        ).fetchall()
        items: list[dict[str, Any]] = []
        committed_total = 0.0
        pending_impact_total = 0.0
        accepted_total = 0.0
        for package_row in packages:
            package_id = package_row["id"]
            detail = self.package_detail(package_id, connection)
            committed_total += detail["budget"]["active_total"]
            pending_impact_total += detail["budget"]["pending_delta"]
            materials = []
            for line in detail["lines"]:
                accepted_total += line["accepted_amount"]
                materials.append({
                    "material_code": line["material_code"],
                    "name": line["name"],
                    "unit": line["unit"],
                    "spec": line["spec"],
                    "quantity": line["quantity"],
                    "unit_price": line["unit_price"],
                    "line_amount": line["line_amount"],
                    "accepted_quantity": line["accepted_quantity"],
                    "source": line["source"],
                })
            items.append({
                "package_id": package_id,
                "code": detail["code"],
                "name": detail["name"],
                "state": detail["state"],
                "budget": detail["budget"],
                "materials": materials,
            })
        return {
            "packages": items,
            "committed_total": money(committed_total),
            "pending_impact_total": money(pending_impact_total),
            "accepted_total": money(accepted_total),
            "projected_committed_total": money(committed_total + pending_impact_total),
        }

    # ------------------------------------------------------------------ 变更单

    def create_change_order(self, package_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        package = self._package(package_id)
        if package["state"] != "frozen":
            raise ConflictError("只有已冻结的采购包可以提出变更单")
        items = payload.get("items") or []
        if not items:
            raise ValidationError("变更单必须至少包含一项变更")
        with transaction(immediate=True) as connection:
            base_version = self._active_version(connection, package_id)
            simulation = self._simulate(connection, base_version["id"], items)
            now = to_storage(self.clock.now())
            try:
                cursor = connection.execute(
                    "INSERT INTO change_orders(package_id,code,title,reason,impact,state,base_version_id,net_delta_amount,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?, 'draft', ?,?,?,?,?)",
                    (package_id, payload["code"], payload["title"], payload.get("reason") or "", payload.get("impact") or "", base_version["id"], simulation["net_delta"], payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("变更单编码已存在") from exc
            change_order_id = cursor.lastrowid
            self._insert_items(connection, change_order_id, items, simulation)
            self._event(connection, "change_order", change_order_id, "created", payload["actor"], {"items": len(items), "net_delta": simulation["net_delta"]}, now)
            return self.change_order_detail(change_order_id, connection)

    def list_change_orders(self, package_id: int | None = None, state: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if package_id is not None:
            clauses.append("package_id=?")
            params.append(package_id)
        if state:
            clauses.append("state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute("SELECT * FROM change_orders" + where + " ORDER BY id DESC", params).fetchall()
        return [dict(row) for row in rows]

    def change_order_detail(self, change_order_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        change_order = connection.execute("SELECT * FROM change_orders WHERE id=?", (change_order_id,)).fetchone()
        if change_order is None:
            raise NotFoundError("变更单不存在")
        result = dict(change_order)
        package = connection.execute("SELECT code,name,budget_limit FROM procurement_packages WHERE id=?", (change_order["package_id"],)).fetchone()
        result["package_code"] = package["code"]
        result["package_name"] = package["name"]
        result["items"] = [dict(row) for row in connection.execute(
            "SELECT * FROM change_order_items WHERE change_order_id=? ORDER BY item_seq", (change_order_id,)
        ).fetchall()]
        result["signatures"] = [dict(row) for row in connection.execute(
            "SELECT * FROM change_order_signatures WHERE change_order_id=? ORDER BY signer_role", (change_order_id,)
        ).fetchall()]
        predecessor = connection.execute("SELECT id,code,state,revision_seq FROM change_orders WHERE id=?", (change_order["revision_of_id"],)).fetchone()
        result["revision_of"] = dict(predecessor) if predecessor else None
        result["revisions"] = [dict(row) for row in connection.execute(
            "SELECT id,code,state,revision_seq FROM change_orders WHERE revision_of_id=? ORDER BY revision_seq,id",
            (change_order_id,),
        ).fetchall()]
        result["validation"] = self._validation_report(connection, change_order)
        result["events"] = self._events(connection, "change_order", change_order_id)
        return result

    def add_signature(self, change_order_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        change_order = self._change_order(change_order_id)
        if change_order["state"] != "draft":
            raise ConflictError("只有草稿变更单可以补充签署")
        package = self._package(change_order["package_id"])
        required = json.loads(package["required_signatures_json"])
        if payload["signer_role"] not in required:
            raise ValidationError("该角色不在采购包要求的签署名单中", context={"required_signatures": required})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                connection.execute(
                    "INSERT INTO change_order_signatures(change_order_id,signer_role,signer,signed_at) VALUES(?,?,?,?)",
                    (change_order_id, payload["signer_role"], payload["signer"], now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该角色已经签署") from exc
            self._event(connection, "change_order", change_order_id, "signed", payload["signer"], {"signer_role": payload["signer_role"]}, now)
            return self.change_order_detail(change_order_id, connection)

    def submit_change_order(self, change_order_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        change_order = self._change_order(change_order_id)
        if change_order["state"] != "draft":
            raise ConflictError("只有草稿变更单可以提交")
        package = self._package(change_order["package_id"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._assert_signatures_complete(connection, package, change_order_id)
            base_version = self._require_current_base(connection, change_order)
            items = connection.execute("SELECT * FROM change_order_items WHERE change_order_id=? ORDER BY item_seq", (change_order_id,)).fetchall()
            simulation = self._simulate(connection, base_version["id"], [dict(row) for row in items])
            committed = money(payload.get("committed_fund_amount") or 0)
            fund_reference = (payload.get("fund_reference") or "").strip()
            increase = max(0.0, simulation["net_delta"])
            if increase > 0 and (committed + _MONEY_EPSILON < increase or not fund_reference):
                raise ConflictError("专项资金承诺不足以覆盖增支，或缺少专项资金批文号", context={"required_commitment": increase, "committed": committed})
            self._assert_budget_headroom(connection, package, change_order_id, simulation["net_delta"])
            connection.execute(
                "UPDATE change_orders SET state='submitted',committed_fund_amount=?,fund_reference=?,net_delta_amount=?,submitted_at=?,updated_at=? WHERE id=?",
                (committed, fund_reference, simulation["net_delta"], now, now, change_order_id),
            )
            self._event(connection, "change_order", change_order_id, "submitted", payload["actor"], {"net_delta": simulation["net_delta"], "committed": committed}, now)
            return self.change_order_detail(change_order_id, connection)

    def reject_change_order(self, change_order_id: int, actor: str, note: str) -> dict[str, Any]:
        return self._decide(change_order_id, actor, note, "rejected", "rejected")

    def withdraw_change_order(self, change_order_id: int, actor: str, note: str) -> dict[str, Any]:
        change_order = self._change_order(change_order_id)
        if change_order["state"] not in {"draft", "submitted"}:
            raise ConflictError("只有草稿或待决变更单可以撤回")
        target_state = "withdrawn"
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE change_orders SET state=?,decision_note=?,decided_by=?,decided_at=?,updated_at=? WHERE id=?",
                (target_state, note, actor, now, now, change_order_id),
            )
            self._event(connection, "change_order", change_order_id, "withdrawn", actor, {"note": note}, now)
            return self.change_order_detail(change_order_id, connection)

    def approve_change_order(self, change_order_id: int, actor: str, note: str) -> dict[str, Any]:
        change_order = self._change_order(change_order_id)
        if change_order["state"] != "submitted":
            raise ConflictError("只有待决变更单可以批准生效")
        package = self._package(change_order["package_id"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            base_version = self._require_current_base(connection, change_order)
            items = connection.execute("SELECT * FROM change_order_items WHERE change_order_id=? ORDER BY item_seq", (change_order_id,)).fetchall()
            simulation = self._simulate(connection, base_version["id"], [dict(row) for row in items])
            self._assert_budget_headroom(connection, package, change_order_id, simulation["net_delta"])
            self._assert_acceptance_floor(connection, package, simulation)
            next_no = int(connection.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM procurement_package_versions WHERE package_id=?",
                (package["id"],),
            ).fetchone()[0])
            new_total = money(base_version["budget_total"] + simulation["net_delta"])
            new_version_id = connection.execute(
                "INSERT INTO procurement_package_versions(package_id,version_no,state,budget_total,change_order_id,activated_at,created_at) VALUES(?,?,'active',?,?,?,?)",
                (package["id"], next_no, new_total, change_order_id, now, now),
            ).lastrowid
            self._insert_target_lines(connection, package["id"], new_version_id, base_version["id"], change_order_id, simulation, now)
            connection.execute("UPDATE procurement_package_versions SET state='superseded' WHERE id=?", (base_version["id"],))
            connection.execute(
                "UPDATE change_orders SET state='effective',effective_version_id=?,decision_note=?,decided_by=?,decided_at=?,effective_at=?,net_delta_amount=?,updated_at=? WHERE id=?",
                (new_version_id, note, actor, now, now, simulation["net_delta"], now, change_order_id),
            )
            connection.execute("UPDATE procurement_packages SET updated_at=? WHERE id=?", (now, package["id"]))
            self._event(connection, "procurement_package", package["id"], "version_activated", actor, {"version_no": next_no, "change_order_code": change_order["code"]}, now)
            self._event(connection, "change_order", change_order_id, "effective", actor, {"version_no": next_no, "net_delta": simulation["net_delta"]}, now)
            return self.change_order_detail(change_order_id, connection)

    def resubmit_change_order(self, change_order_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        previous = self._change_order(change_order_id)
        if previous["state"] not in {"rejected", "withdrawn"}:
            raise ConflictError("只有驳回或撤回的变更单可以重新提交")
        old_items = self.connection.execute("SELECT * FROM change_order_items WHERE change_order_id=? ORDER BY item_seq", (change_order_id,)).fetchall()
        items = payload.get("items")
        if items is None:
            items = [{k: row[k] for k in row.keys()} for row in old_items]
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            base_version = self._active_version(connection, previous["package_id"])
            simulation = self._simulate(connection, base_version["id"], items)
            try:
                cursor = connection.execute(
                    "INSERT INTO change_orders(package_id,code,title,reason,impact,state,base_version_id,revision_of_id,revision_seq,net_delta_amount,fund_reference,committed_fund_amount,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?, 'draft', ?,?,?,?,?,?,?,?,?)",
                    (
                        previous["package_id"], payload["code"], payload.get("title") or previous["title"],
                        payload.get("reason") or previous["reason"], payload.get("impact") or previous["impact"],
                        base_version["id"], change_order_id, int(previous["revision_seq"]) + 1, simulation["net_delta"],
                        payload.get("fund_reference") or previous["fund_reference"], money(payload.get("committed_fund_amount") if payload.get("committed_fund_amount") is not None else previous["committed_fund_amount"]),
                        payload["actor"], now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("变更单编码已存在") from exc
            new_id = cursor.lastrowid
            self._insert_items(connection, new_id, items, simulation)
            self._event(connection, "change_order", change_order_id, "revised", payload["actor"], {"new_change_order_id": new_id, "revision_seq": previous["revision_seq"] + 1}, now)
            self._event(connection, "change_order", new_id, "created", payload["actor"], {"revision_of": previous["code"], "items": len(items)}, now)
            return self.change_order_detail(new_id, connection)

    def _decide(self, change_order_id: int, actor: str, note: str, state: str, event_type: str) -> dict[str, Any]:
        change_order = self._change_order(change_order_id)
        if change_order["state"] != "submitted":
            raise ConflictError("只有待决变更单可以驳回")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE change_orders SET state=?,decision_note=?,decided_by=?,decided_at=?,updated_at=? WHERE id=?",
                (state, note, actor, now, now, change_order_id),
            )
            self._event(connection, "change_order", change_order_id, event_type, actor, {"note": note}, now)
            return self.change_order_detail(change_order_id, connection)

    # ------------------------------------------------------------------ 内部

    def _simulate(self, connection: sqlite3.Connection, base_version_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
        base_rows = connection.execute("SELECT * FROM procurement_package_lines WHERE package_version_id=?", (base_version_id,)).fetchall()
        base: dict[str, sqlite3.Row] = {row["material_code"]: row for row in base_rows}
        targets: dict[str, dict[str, Any]] = {
            code: {
                "material_code": code, "name": row["name"], "unit": row["unit"], "spec": row["spec"],
                "quantity": float(row["quantity"]), "unit_price": float(row["unit_price"]),
                "kind": row["line_kind"], "predecessor_code": None, "is_new": False,
            }
            for code, row in base.items()
        }
        touched: set[str] = set()
        item_deltas: list[float] = []
        for index, item in enumerate(items, start=1):
            kind = item["change_kind"]
            code = item["material_code"]
            if not code:
                raise ValidationError(f"第{index}项缺少材料编码")
            if code in touched:
                raise ValidationError(f"变更单中材料 {code} 出现了多次")
            touched.add(code)
            if kind == "addition":
                if code in base:
                    raise ValidationError(f"新增材料 {code} 已在基线清单中，应使用数量调整")
                if not item.get("name") or not item.get("unit"):
                    raise ValidationError(f"新增材料 {code} 必须提供名称和单位")
                if float(item.get("quantity") or 0) <= 0:
                    raise ValidationError(f"新增材料 {code} 数量必须大于零")
                price = money(item.get("unit_price") or 0)
                targets[code] = {
                    "material_code": code, "name": item["name"], "unit": item["unit"], "spec": item.get("spec") or "",
                    "quantity": float(item["quantity"]), "unit_price": price, "kind": "addition",
                    "predecessor_code": None, "is_new": True,
                }
                item_deltas.append(money(float(item["quantity"]) * price))
            elif kind == "removal":
                if code not in base:
                    raise ValidationError(f"移除材料 {code} 不在基线清单中")
                del targets[code]
                item_deltas.append(money(-float(base[code]["line_amount"])))
            elif kind == "adjustment":
                if code not in base:
                    raise ValidationError(f"调整材料 {code} 不在基线清单中，应使用新增")
                source = base[code]
                new_quantity = float(source["quantity"]) + float(item.get("quantity_delta") or 0)
                if new_quantity < -_MONEY_EPSILON:
                    raise ValidationError(f"材料 {code} 调整后数量为负")
                new_price = float(source["unit_price"]) if item.get("new_unit_price") is None else float(item["new_unit_price"])
                if new_price < 0:
                    raise ValidationError(f"材料 {code} 新单价不能为负")
                if abs(float(item.get("quantity_delta") or 0)) < _MONEY_EPSILON and item.get("new_unit_price") is None:
                    raise ValidationError(f"材料 {code} 的调整没有数量或单价变化")
                targets[code].update({"quantity": new_quantity, "unit_price": new_price, "kind": "adjustment", "predecessor_code": code})
                item_deltas.append(money(new_quantity * new_price - float(source["line_amount"])))
            elif kind == "substitution":
                target_code = item.get("target_material_code") or ""
                if code not in base:
                    raise ValidationError(f"替代材料 {code} 不在基线清单中")
                if not target_code or target_code == code:
                    raise ValidationError(f"材料 {code} 的替代材料编码缺失或与原材料相同")
                if target_code in base:
                    raise ValidationError(f"替代材料 {target_code} 已在基线清单中，应使用数量调整")
                if target_code in targets:
                    raise ValidationError(f"替代材料 {target_code} 在同一变更单中重复定义")
                if not item.get("target_name") or not item.get("target_unit"):
                    raise ValidationError(f"替代材料 {target_code} 必须提供名称和单位")
                if float(item.get("target_quantity") or 0) <= 0:
                    raise ValidationError(f"替代材料 {target_code} 数量必须大于零")
                target_price = money(item.get("target_unit_price") or 0)
                del targets[code]
                targets[target_code] = {
                    "material_code": target_code, "name": item["target_name"], "unit": item["target_unit"],
                    "spec": item.get("target_spec") or "", "quantity": float(item["target_quantity"]),
                    "unit_price": target_price, "kind": "substitution", "predecessor_code": None, "is_new": True,
                    "substitutes_code": code,
                }
                item_deltas.append(money(float(item["target_quantity"]) * target_price - float(base[code]["line_amount"])))
            else:
                raise ValidationError(f"未知变更类型：{kind}")
        return {"targets": targets, "net_delta": money(sum(item_deltas)), "item_deltas": item_deltas}

    def _insert_items(self, connection: sqlite3.Connection, change_order_id: int, items: list[dict[str, Any]], simulation: dict[str, Any]) -> None:
        for seq, (item, line_delta) in enumerate(zip(items, simulation["item_deltas"], strict=True), start=1):
            connection.execute(
                "INSERT INTO change_order_items(change_order_id,item_seq,change_kind,material_code,name,unit,spec,quantity,unit_price,quantity_delta,new_unit_price,"
                "target_material_code,target_name,target_unit,target_spec,target_quantity,target_unit_price,line_delta_amount,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    change_order_id, seq, item["change_kind"], item["material_code"], item.get("name") or "", item.get("unit") or "",
                    item.get("spec") or "", float(item.get("quantity") or 0), money(item.get("unit_price") or 0),
                    float(item.get("quantity_delta") or 0),
                    None if item.get("new_unit_price") is None else float(item["new_unit_price"]),
                    item.get("target_material_code") or "", item.get("target_name") or "", item.get("target_unit") or "",
                    item.get("target_spec") or "", float(item.get("target_quantity") or 0), money(item.get("target_unit_price") or 0),
                    line_delta, item.get("reason") or "",
                ),
            )

    def _insert_target_lines(
        self, connection: sqlite3.Connection, package_id: int, new_version_id: int, base_version_id: int,
        change_order_id: int, simulation: dict[str, Any], now: str,
    ) -> None:
        base_rows = {row["material_code"]: row for row in connection.execute(
            "SELECT * FROM procurement_package_lines WHERE package_version_id=?", (base_version_id,)
        ).fetchall()}
        for code, target in simulation["targets"].items():
            if target["is_new"]:
                material_id = self._get_or_create_material(connection, package_id, code, target["name"], target["unit"], target["spec"], now)
                predecessor_id = None
                if target["kind"] == "substitution" and target.get("substitutes_code") in base_rows:
                    predecessor_id = base_rows[target["substitutes_code"]]["id"]
                origin_change = change_order_id
                kind = target["kind"]
            else:
                source_line = base_rows[code]
                material_id = source_line["material_id"]
                predecessor_id = source_line["id"]
                if target.get("predecessor_code"):
                    kind = target["kind"]
                    origin_change = change_order_id
                else:
                    kind = source_line["line_kind"]
                    origin_change = source_line["change_order_id"]
            amount = money(target["quantity"] * target["unit_price"])
            connection.execute(
                "INSERT INTO procurement_package_lines(package_id,package_version_id,material_id,material_code,name,unit,spec,quantity,unit_price,line_amount,line_kind,predecessor_line_id,change_order_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (package_id, new_version_id, material_id, code, target["name"], target["unit"], target["spec"],
                 float(target["quantity"]), target["unit_price"], amount, kind, predecessor_id, origin_change),
            )

    def _assert_acceptance_floor(self, connection: sqlite3.Connection, package: sqlite3.Row, simulation: dict[str, Any]) -> None:
        accepted = {
            row["material_code"]: float(row["accepted"])
            for row in connection.execute(
                "SELECT material_code,SUM(quantity) AS accepted FROM procurement_receipts WHERE package_id=? GROUP BY material_code",
                (package["id"],),
            ).fetchall()
        }
        violations: list[dict[str, Any]] = []
        for code, accepted_quantity in accepted.items():
            target = simulation["targets"].get(code)
            if target is None:
                violations.append({"material_code": code, "accepted_quantity": accepted_quantity, "target_quantity": 0})
            elif target["quantity"] + _MONEY_EPSILON < accepted_quantity:
                violations.append({"material_code": code, "accepted_quantity": accepted_quantity, "target_quantity": target["quantity"]})
        if violations:
            raise ConflictError("新版本数量不能低于已验收供应数量，或移除已验收材料", context={"violations": violations})

    def _assert_budget_headroom(self, connection: sqlite3.Connection, package: sqlite3.Row, change_order_id: int, net_delta: float) -> None:
        active = connection.execute("SELECT budget_total FROM procurement_package_versions WHERE package_id=? AND state='active'", (package["id"],)).fetchone()
        active_total = float(active["budget_total"]) if active else 0.0
        reserved = float(connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN net_delta_amount>0 THEN net_delta_amount ELSE 0 END),0) FROM change_orders WHERE package_id=? AND state='submitted' AND id<>?",
            (package["id"], change_order_id),
        ).fetchone()[0])
        projected = active_total + reserved + net_delta
        if projected > money(package["budget_limit"]) + _MONEY_EPSILON:
            raise ConflictError("变更后预算将超过采购包预算上限", context={
                "budget_limit": money(package["budget_limit"]), "projected_total": money(projected),
                "active_total": money(active_total), "reserved_by_pending": money(reserved),
            })

    def _assert_signatures_complete(self, connection: sqlite3.Connection, package: sqlite3.Row, change_order_id: int) -> None:
        required = json.loads(package["required_signatures_json"])
        signed = {row["signer_role"] for row in connection.execute(
            "SELECT signer_role FROM change_order_signatures WHERE change_order_id=?", (change_order_id,)
        ).fetchall()}
        missing = [role for role in required if role not in signed]
        if missing:
            raise ConflictError("变更单所需签署尚未齐备", context={"required_signatures": required, "missing": missing})

    def _validation_report(self, connection: sqlite3.Connection, change_order: sqlite3.Row) -> dict[str, Any]:
        package = connection.execute("SELECT * FROM procurement_packages WHERE id=?", (change_order["package_id"],)).fetchone()
        required = json.loads(package["required_signatures_json"])
        signed = {row["signer_role"] for row in connection.execute(
            "SELECT signer_role FROM change_order_signatures WHERE change_order_id=?", (change_order["id"],)
        ).fetchall()}
        missing = [role for role in required if role not in signed]
        issues: list[str] = []
        if missing:
            issues.append("签署未齐备")
        active = connection.execute("SELECT id FROM procurement_package_versions WHERE package_id=? AND state='active'", (package["id"],)).fetchone()
        if active is None or active["id"] != change_order["base_version_id"]:
            issues.append("基线版本已被其他变更替代，需要重提")
        increase = max(0.0, float(change_order["net_delta_amount"]))
        if increase > 0 and (not change_order["fund_reference"] or float(change_order["committed_fund_amount"]) + _MONEY_EPSILON < increase):
            issues.append("专项资金承诺不足")
        return {"missing_signatures": missing, "ready_to_submit": not issues and change_order["state"] == "draft", "issues": issues}

    @staticmethod
    def _require_current_base(connection: sqlite3.Connection, change_order: sqlite3.Row) -> sqlite3.Row:
        active = connection.execute(
            "SELECT * FROM procurement_package_versions WHERE package_id=? AND state='active'",
            (change_order["package_id"],),
        ).fetchone()
        if active is None or active["id"] != change_order["base_version_id"]:
            raise ConflictError("基线版本已被其他生效变更替代，请基于当前版本重新提交")
        return active

    def _lines_for_version(self, connection: sqlite3.Connection, package_id: int, version_id: int) -> list[dict[str, Any]]:
        version = connection.execute("SELECT id,version_no,change_order_id,state FROM procurement_package_versions WHERE id=?", (version_id,)).fetchone()
        result: list[dict[str, Any]] = []
        for line in connection.execute("SELECT * FROM procurement_package_lines WHERE package_version_id=? ORDER BY material_code", (version_id,)).fetchall():
            item = dict(line)
            accepted = connection.execute(
                "SELECT COALESCE(SUM(quantity),0),COALESCE(SUM(quantity*unit_price),0) FROM procurement_receipts WHERE material_id=?",
                (line["material_id"],),
            ).fetchone()
            item["accepted_quantity"] = float(accepted[0])
            item["accepted_amount"] = money(accepted[1])
            source: dict[str, Any] = {"version_no": version["version_no"]}
            if line["change_order_id"]:
                origin_change = connection.execute("SELECT code FROM change_orders WHERE id=?", (line["change_order_id"],)).fetchone()
                source["change_order_id"] = line["change_order_id"]
                source["change_order_code"] = origin_change["code"] if origin_change else None
            predecessor = None
            if line["predecessor_line_id"]:
                prev = connection.execute(
                    "SELECT l.id,l.material_code,l.quantity,l.unit_price,v.version_no FROM procurement_package_lines l JOIN procurement_package_versions v ON v.id=l.package_version_id WHERE l.id=?",
                    (line["predecessor_line_id"],),
                ).fetchone()
                predecessor = dict(prev) if prev else None
            source["predecessor_line"] = predecessor
            source["line_kind"] = line["line_kind"]
            item["source"] = source
            result.append(item)
        return result

    def _version_header(self, connection: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT v.*,co.code AS change_order_code,co.title AS change_order_title FROM procurement_package_versions v "
            "LEFT JOIN change_orders co ON co.id=v.change_order_id WHERE v.id=?",
            (version_id,),
        ).fetchone()
        return dict(row)

    def _upsert_draft_line(self, connection: sqlite3.Connection, package_id: int, version_id: int, item: dict[str, Any], now: str) -> None:
        if not item.get("name") or not item.get("unit"):
            raise ValidationError(f"材料 {item.get('material_code')} 必须提供名称和单位")
        if float(item.get("quantity") or 0) < 0 or float(item.get("unit_price") or 0) < 0:
            raise ValidationError("材料数量和单价不能为负")
        material_id = self._get_or_create_material(connection, package_id, item["material_code"], item["name"], item["unit"], item.get("spec") or "", now)
        amount = money(float(item["quantity"]) * float(item["unit_price"]))
        existing = connection.execute("SELECT id FROM procurement_package_lines WHERE package_version_id=? AND material_id=?", (version_id, material_id)).fetchone()
        if existing:
            connection.execute(
                "UPDATE procurement_package_lines SET name=?,unit=?,spec=?,quantity=?,unit_price=?,line_amount=? WHERE id=?",
                (item["name"], item["unit"], item.get("spec") or "", float(item["quantity"]), money(item["unit_price"]), amount, existing["id"]),
            )
        else:
            connection.execute(
                "INSERT INTO procurement_package_lines(package_id,package_version_id,material_id,material_code,name,unit,spec,quantity,unit_price,line_amount,line_kind) VALUES(?,?,?,?,?,?,?,?,?,?, 'line')",
                (package_id, version_id, material_id, item["material_code"], item["name"], item["unit"], item.get("spec") or "", float(item["quantity"]), money(item["unit_price"]), amount),
            )

    @staticmethod
    def _get_or_create_material(connection: sqlite3.Connection, package_id: int, code: str, name: str, unit: str, spec: str, now: str) -> int:
        existing = connection.execute("SELECT id FROM procurement_materials WHERE package_id=? AND material_code=?", (package_id, code)).fetchone()
        if existing:
            connection.execute("UPDATE procurement_materials SET name=?,unit=?,spec=? WHERE id=?", (name, unit, spec, existing["id"]))
            return int(existing["id"])
        cursor = connection.execute(
            "INSERT INTO procurement_materials(package_id,material_code,name,unit,spec,created_at) VALUES(?,?,?,?,?,?)",
            (package_id, code, name, unit, spec, now),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _version_total(connection: sqlite3.Connection, version_id: int) -> float:
        return money(connection.execute("SELECT COALESCE(SUM(line_amount),0) FROM procurement_package_lines WHERE package_version_id=?", (version_id,)).fetchone()[0])

    def _draft_version(self, connection: sqlite3.Connection, package_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM procurement_package_versions WHERE package_id=? AND state='draft' ORDER BY version_no LIMIT 1", (package_id,)).fetchone()
        if row is None:
            raise NotFoundError("采购包草稿版本不存在")
        return row

    def _active_version(self, connection: sqlite3.Connection, package_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM procurement_package_versions WHERE package_id=? AND state='active'", (package_id,)).fetchone()
        if row is None:
            raise ConflictError("采购包尚未冻结，没有生效版本")
        return row

    def _package(self, package_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM procurement_packages WHERE id=?", (package_id,)).fetchone()
        if row is None:
            raise NotFoundError("采购包不存在")
        return row

    def _change_order(self, change_order_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM change_orders WHERE id=?", (change_order_id,)).fetchone()
        if row is None:
            raise NotFoundError("变更单不存在")
        return row

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.repository.temple_by_code(code)
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    @staticmethod
    def _event(connection: sqlite3.Connection, resource_type: str, resource_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO restoration_events(resource_type,resource_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (resource_type, resource_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, resource_type: str, resource_id: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM restoration_events WHERE resource_type=? AND resource_id=? ORDER BY id",
            (resource_type, resource_id),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
