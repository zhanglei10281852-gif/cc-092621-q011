from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.temple.schema import ensure_temple_schema

_CENTS = 0.01


def _money(value: float) -> float:
    return round(float(value) + 1e-9, 2)


class ProcurementService:
    """采购包与变更单管理。

    采购包在冻结批准时固化清单版本与预算承诺；变更单逐项描述增减与替代，
    只有专项资金可承诺且所需签署齐备时才能生效；已验收数量构成不可追溯改小的地板。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ funds

    def create_fund(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO special_funds(temple_id,code,name,total_amount,note,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (temple["id"], payload["code"], payload["name"], _money(payload["total_amount"]), payload["note"], payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("专项资金编码已存在") from exc
            self._event(connection, "special_fund", cursor.lastrowid, "created", payload["actor"], {"total_amount": _money(payload["total_amount"])}, now)
            return self.fund_detail(payload["code"], connection)

    def fund_detail(self, code: str, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM special_funds WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("专项资金不存在")
        result = dict(row)
        held = _money(connection.execute("SELECT COALESCE(SUM(amount),0) FROM fund_commitments WHERE fund_id=? AND state='held'", (row["id"],)).fetchone()[0])
        result["held_amount"] = held
        result["available_amount"] = _money(result["total_amount"] - held)
        result["commitments"] = [dict(item) for item in connection.execute(
            "SELECT c.*,p.code AS package_code,o.code AS change_order_code FROM fund_commitments c "
            "JOIN procurement_packages p ON p.id=c.package_id LEFT JOIN procurement_change_orders o ON o.id=c.change_order_id "
            "WHERE c.fund_id=? ORDER BY c.id",
            (row["id"],),
        ).fetchall()]
        return result

    # -------------------------------------------------------------- packages

    def create_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        fund = self.connection.execute("SELECT * FROM special_funds WHERE code=?", (payload["fund_code"],)).fetchone()
        if fund is None:
            raise NotFoundError("专项资金不存在")
        if fund["temple_id"] != temple["id"]:
            raise ValidationError("专项资金不属于目标寺院")
        if payload.get("restoration_campaign_id"):
            campaign = self.connection.execute("SELECT * FROM restoration_campaigns WHERE id=?", (payload["restoration_campaign_id"],)).fetchone()
            if campaign is None or campaign["temple_id"] != temple["id"]:
                raise ValidationError("修缮活动不属于目标寺院")
        materials = payload["materials"]
        total = _money(sum(_money(item["quantity"] * item["unit_price"]) for item in materials))
        if total > payload["budget_cap"] + _CENTS:
            raise ValidationError("清单金额不能超过采购包预算上限", context={"total": total, "budget_cap": payload["budget_cap"]})
        roles = self._unique_roles(payload.get("required_signoffs") or [])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO procurement_packages(temple_id,restoration_campaign_id,fund_id,code,name,scope_summary,budget_cap,required_signoffs_json,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (temple["id"], payload.get("restoration_campaign_id"), fund["id"], payload["code"], payload["name"], payload["scope_summary"], _money(payload["budget_cap"]), json.dumps(roles, ensure_ascii=False), payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("采购包编码已存在") from exc
            package_id = cursor.lastrowid
            for item in materials:
                connection.execute(
                    "INSERT INTO procurement_material_lines(package_id,material_code,material_name,material_spec,material_unit,quantity,unit_price,accepted_qty,created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,?,?)",
                    (package_id, item["material_code"], item["material_name"], item["material_spec"], item["material_unit"], item["quantity"], item["unit_price"], now, now),
                )
            self._event(connection, "procurement_package", package_id, "created", payload["actor"], {"materials": len(materials), "total": total}, now)
            return self.package_detail(payload["code"], connection)

    def list_packages(self, temple_code: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT p.*,n.code AS temple_code,n.name AS temple_name,f.code AS fund_code,f.name AS fund_name "
            "FROM procurement_packages p JOIN temple_sites n ON n.id=p.temple_id "
            "JOIN special_funds f ON f.id=p.fund_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            clauses.append("n.code=?")
            params.append(temple_code)
        if state:
            clauses.append("p.state=?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY p.id DESC"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def package_detail(self, code: str, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        package = self._package_row(code, connection)
        result = dict(package)
        result["required_signoffs"] = json.loads(result.pop("required_signoffs_json"))
        fund = connection.execute("SELECT * FROM special_funds WHERE id=?", (package["fund_id"],)).fetchone()
        result["fund"] = {"id": fund["id"], "code": fund["code"], "name": fund["name"], "total_amount": fund["total_amount"]}
        result["materials"] = self._materials(connection, package["id"])
        result["current_version"] = self._version_header(connection, package["id"], package["current_version_no"])
        result["versions"] = [dict(row) for row in connection.execute(
            "SELECT id,version_no,state,source_change_order_id,total_amount,created_by,created_at,superseded_at FROM procurement_package_versions WHERE package_id=? ORDER BY version_no",
            (package["id"],),
        ).fetchall()]
        pending = [dict(row) for row in connection.execute(
            "SELECT id,code,title,revision_no,resubmits_change_order_id,budget_delta,reserved_amount,submitted_at FROM procurement_change_orders WHERE package_id=? AND state='pending' ORDER BY id",
            (package["id"],),
        ).fetchall()]
        result["pending_change_orders"] = pending
        pending_reserved = _money(sum(item["reserved_amount"] for item in pending))
        pending_delta = _money(sum(item["budget_delta"] for item in pending))
        held = _money(connection.execute("SELECT COALESCE(SUM(amount),0) FROM fund_commitments WHERE fund_id=? AND state='held'", (package["fund_id"],)).fetchone()[0])
        committed = _money(package["committed_amount"])
        result["financials"] = {
            "budget_cap": _money(package["budget_cap"]),
            "committed_amount": committed,
            "pending_reserved": pending_reserved,
            "pending_delta": pending_delta,
            "projected_if_all_effective": _money(committed + pending_delta),
            "cap_headroom_for_new_change": _money(package["budget_cap"] - committed - pending_reserved),
            "fund_total": _money(fund["total_amount"]),
            "fund_held": held,
            "fund_available": _money(fund["total_amount"] - held),
            "accepted_amount": _money(sum(_money(item["accepted_qty"] * item["unit_price"]) for item in result["materials"])),
        }
        result["freeze_signoffs"] = self._signoffs(connection, package["id"], None)
        result["events"] = self._events(connection, "procurement_package", package["id"])
        return result

    def freeze_package(self, code: str, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            package = self._package_row(code, connection)
            if package["state"] != "draft":
                raise ConflictError("只有草稿采购包可以冻结批准")
            required = json.loads(package["required_signoffs_json"])
            provided = self._signoff_map(payload.get("signoffs") or [])
            missing = [role for role in required if role not in provided]
            if missing:
                raise ValidationError("冻结批准缺少必需签署", context={"missing_signoffs": missing})
            extra = [role for role in provided if role not in required]
            if extra:
                raise ValidationError("存在不需要的签署角色", context={"extra_signoffs": extra})
            materials = self._materials(connection, package["id"])
            total = _money(sum(item["line_total"] for item in materials))
            if total > package["budget_cap"] + _CENTS:
                raise ConflictError("清单金额超过采购包预算上限")
            fund = connection.execute("SELECT * FROM special_funds WHERE id=?", (package["fund_id"],)).fetchone()
            held = _money(connection.execute("SELECT COALESCE(SUM(amount),0) FROM fund_commitments WHERE fund_id=? AND state='held'", (fund["id"],)).fetchone()[0])
            if held + total > fund["total_amount"] + _CENTS:
                raise ConflictError("专项资金余额不足以承诺采购包预算", context={"available": _money(fund["total_amount"] - held), "required": total})
            now = to_storage(self.clock.now())
            manifest = [
                {
                    "material_code": item["material_code"],
                    "material_name": item["material_name"],
                    "material_spec": item["material_spec"],
                    "material_unit": item["material_unit"],
                    "quantity": float(item["quantity"]),
                    "unit_price": float(item["unit_price"]),
                    "line_total": item["line_total"],
                    "status": "active",
                }
                for item in materials
            ]
            connection.execute(
                "INSERT INTO procurement_package_versions(package_id,version_no,state,source_change_order_id,manifest_json,total_amount,created_by,created_at) VALUES(?,?, 'effective',NULL,?,?,?,?)",
                (package["id"], 1, json.dumps(manifest, ensure_ascii=False, sort_keys=True), total, payload["actor"], now),
            )
            cursor = connection.execute(
                "INSERT INTO fund_commitments(fund_id,package_id,change_order_id,amount,state,reason,created_by,created_at) VALUES(?,?,NULL,?,'held',?,?,?)",
                (fund["id"], package["id"], total, "freeze", payload["actor"], now),
            )
            for role in required:
                signoff = provided[role]
                connection.execute(
                    "INSERT INTO procurement_signoffs(package_id,change_order_id,signer_role,signer,note,signed_at) VALUES(?,NULL,?,?,?,?)",
                    (package["id"], role, signoff["signer"], signoff["note"], now),
                )
            connection.execute(
                "UPDATE procurement_packages SET state='frozen',committed_amount=?,current_version_no=1,frozen_by=?,frozen_at=?,updated_at=? WHERE id=?",
                (total, payload["actor"], now, now, package["id"]),
            )
            self._event(connection, "procurement_package", package["id"], "frozen", payload["actor"], {"version_no": 1, "total": total, "commitment_id": cursor.lastrowid}, now)
            return self.package_detail(code, connection)

    def close_package(self, code: str, actor: str, reason: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            package = self._package_row(code, connection)
            if package["state"] != "frozen":
                raise ConflictError("只有已冻结采购包可以关闭")
            pending = connection.execute("SELECT COUNT(*) FROM procurement_change_orders WHERE package_id=? AND state='pending'", (package["id"],)).fetchone()[0]
            if pending:
                raise ConflictError("仍有未决变更单，不能关闭采购包", context={"pending": pending})
            now = to_storage(self.clock.now())
            connection.execute("UPDATE procurement_packages SET state='closed',closed_at=?,updated_at=? WHERE id=?", (now, now, package["id"]))
            self._event(connection, "procurement_package", package["id"], "closed", actor, {"reason": reason}, now)
            return self.package_detail(code, connection)

    def package_version(self, code: str, version_no: int) -> dict[str, Any]:
        package = self._package_row(code, self.connection)
        row = self.connection.execute("SELECT * FROM procurement_package_versions WHERE package_id=? AND version_no=?", (package["id"], version_no)).fetchone()
        if row is None:
            raise NotFoundError("清单版本不存在")
        result = dict(row)
        result["manifest"] = json.loads(result.pop("manifest_json"))
        if result["source_change_order_id"]:
            co = self.connection.execute("SELECT id,code,title FROM procurement_change_orders WHERE id=?", (result["source_change_order_id"],)).fetchone()
            result["source_change_order"] = {"id": co["id"] if co else None, "code": co["code"] if co else None, "title": co["title"] if co else None}
        return result

    def receive_material(self, package_code: str, material_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            package = self._package_row(package_code, connection)
            if package["state"] not in {"frozen", "closed"}:
                raise ConflictError("采购包尚未冻结，不能登记验收")
            line = connection.execute("SELECT * FROM procurement_material_lines WHERE package_id=? AND material_code=?", (package["id"], material_code)).fetchone()
            if line is None:
                raise NotFoundError("采购包清单中没有该材料")
            if line["substituted_by_change_order_id"] is not None:
                raise ConflictError("材料已被替代，不能继续验收")
            quantity = payload["quantity"]
            new_accepted = _money(line["accepted_qty"] + quantity)
            if new_accepted > line["quantity"] + _CENTS:
                raise ConflictError("验收数量不能超过当前清单数量", context={"ordered": line["quantity"], "accepted": line["accepted_qty"]})
            now = to_storage(self.clock.now())
            connection.execute(
                "UPDATE procurement_material_lines SET accepted_qty=?,updated_at=? WHERE id=?",
                (new_accepted, now, line["id"]),
            )
            connection.execute(
                "INSERT INTO procurement_receipts(package_id,material_code,quantity,actor,note,received_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (package["id"], material_code, quantity, payload["actor"], payload["note"], now, now),
            )
            self._event(connection, "procurement_package", package["id"], "material_received", payload["actor"], {"material_code": material_code, "quantity": quantity, "accepted_qty": new_accepted}, now)
            return self.package_detail(package_code, connection)

    # ----------------------------------------------------------- change orders

    def create_change_order(self, payload: dict[str, Any], *, resubmits_id: int | None = None) -> dict[str, Any]:
        package = self._package_row(payload["package_code"], self.connection)
        if package["state"] != "frozen":
            raise ConflictError("只有已冻结采购包可以提出变更单")
        materials = self._materials(self.connection, package["id"])
        simulation = self._simulate(materials, payload["items"])
        revision_no = 1
        if resubmits_id is not None:
            parent = self.connection.execute("SELECT * FROM procurement_change_orders WHERE id=?", (resubmits_id,)).fetchone()
            if parent is None:
                raise NotFoundError("原变更单不存在")
            if parent["package_id"] != package["id"]:
                raise ValidationError("重提变更单必须属于同一采购包")
            if parent["state"] not in {"rejected", "withdrawn"}:
                raise ConflictError("只有被驳回或已撤回的变更单可以重提")
            revision_no = int(parent["revision_no"]) + 1
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO procurement_change_orders(package_id,code,title,state,revision_no,resubmits_change_order_id,budget_delta,reserved_amount,created_by,created_at,updated_at) VALUES(?,?,?, 'draft',?,?,?,?,?,?,?)",
                    (package["id"], payload["code"], payload["title"], revision_no, resubmits_id, simulation["delta"], simulation["reserved"], payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("变更单编码已存在") from exc
            change_order_id = cursor.lastrowid
            for line_no, item in enumerate(payload["items"], start=1):
                connection.execute(
                    "INSERT INTO procurement_change_items(change_order_id,line_no,kind,material_code,material_name,material_spec,material_unit,material_unit_price,quantity,substitute_code,substitute_name,substitute_spec,substitute_unit,substitute_unit_price,reason,impact) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (change_order_id, line_no, item["kind"], item["material_code"], item["material_name"], item["material_spec"], item["material_unit"], item["material_unit_price"], item["quantity"], item["substitute_code"], item["substitute_name"], item["substitute_spec"], item["substitute_unit"], item["substitute_unit_price"], item["reason"], item["impact"]),
                )
            self._event(connection, "procurement_change_order", change_order_id, "created" if resubmits_id is None else "resubmitted", payload["actor"], {"revision_no": revision_no, "resubmits_change_order_id": resubmits_id, "delta": simulation["delta"]}, now)
            return self.change_order_detail(change_order_id, connection)

    def resubmit_change_order(self, change_order_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        parent = self._change_order_row(change_order_id, self.connection)
        package = self._package_by_id(parent["package_id"], self.connection)
        items = payload.get("items")
        if items is None:
            items = [self._item_input(self.connection, row) for row in self.connection.execute("SELECT * FROM procurement_change_items WHERE change_order_id=? ORDER BY line_no", (change_order_id,)).fetchall()]
        create_payload = {
            "package_code": package["code"],
            "code": payload["code"],
            "title": payload.get("title") or parent["title"],
            "items": items,
            "actor": payload["actor"],
        }
        return self.create_change_order(create_payload, resubmits_id=change_order_id)

    def list_change_orders(self, package_code: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT o.*,p.code AS package_code FROM procurement_change_orders o "
            "JOIN procurement_packages p ON p.id=o.package_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if package_code:
            clauses.append("p.code=?")
            params.append(package_code)
        if state:
            clauses.append("o.state=?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY o.id DESC"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def change_order_detail(self, change_order_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        order = self._change_order_row(change_order_id, connection)
        result = dict(order)
        package = connection.execute("SELECT code,name FROM procurement_packages WHERE id=?", (order["package_id"],)).fetchone()
        result["package_code"] = package["code"]
        result["package_name"] = package["name"]
        result["items"] = [self._item_dict(row) for row in connection.execute("SELECT * FROM procurement_change_items WHERE change_order_id=? ORDER BY line_no", (change_order_id,)).fetchall()]
        result["signoffs"] = self._signoffs(connection, order["package_id"], change_order_id)
        result["events"] = self._events(connection, "procurement_change_order", change_order_id)
        if order["resubmits_change_order_id"]:
            previous = connection.execute("SELECT id,code,title,revision_no,state FROM procurement_change_orders WHERE id=?", (order["resubmits_change_order_id"],)).fetchone()
            result["resubmits"] = dict(previous) if previous else None
        result["revision_chain"] = self._revision_chain(connection, order)
        return result

    def submit_change_order(self, change_order_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            order = self._change_order_row(change_order_id, connection)
            if order["state"] != "draft":
                raise ConflictError("只有草稿变更单可以提交")
            package = self._package_by_id(order["package_id"], connection)
            required = json.loads(package["required_signoffs_json"])
            provided = self._signoff_map(payload.get("signoffs") or [])
            missing = [role for role in required if role not in provided]
            if missing:
                raise ValidationError("变更单缺少必需签署", context={"missing_signoffs": missing})
            extra = [role for role in provided if role not in required]
            if extra:
                raise ValidationError("存在不需要的签署角色", context={"extra_signoffs": extra})
            # 依据当前生效版本重新模拟并占用预算（草稿期内基线可能已变化）。
            materials = self._materials(connection, package["id"])
            items = [self._item_input(connection, row) for row in connection.execute("SELECT * FROM procurement_change_items WHERE change_order_id=? ORDER BY line_no", (change_order_id,)).fetchall()]
            simulation = self._simulate(materials, items)
            self._guard_budget(connection, package, simulation["reserved"], exclude_change_order_id=change_order_id)
            now = to_storage(self.clock.now())
            if simulation["reserved"] > 0:
                connection.execute(
                    "INSERT INTO fund_commitments(fund_id,package_id,change_order_id,amount,state,reason,created_by,created_at) VALUES(?,?,?,?,'held',?,?,?)",
                    (package["fund_id"], package["id"], change_order_id, simulation["reserved"], "pending_change", payload["actor"], now),
                )
            for role in required:
                signoff = provided[role]
                connection.execute(
                    "INSERT INTO procurement_signoffs(package_id,change_order_id,signer_role,signer,note,signed_at) VALUES(?,?,?,?,?,?)",
                    (package["id"], change_order_id, role, signoff["signer"], signoff["note"], now),
                )
            connection.execute(
                "UPDATE procurement_change_orders SET state='pending',budget_delta=?,reserved_amount=?,submitted_by=?,submitted_at=?,updated_at=? WHERE id=?",
                (simulation["delta"], simulation["reserved"], payload["actor"], now, now, change_order_id),
            )
            self._event(connection, "procurement_change_order", change_order_id, "submitted", payload["actor"], {"delta": simulation["delta"], "reserved": simulation["reserved"]}, now)
            return self.change_order_detail(change_order_id, connection)

    def approve_change_order(self, change_order_id: int, actor: str, note: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            order = self._change_order_row(change_order_id, connection)
            if order["state"] != "pending":
                raise ConflictError("只有待决变更单可以批准")
            package = self._package_by_id(order["package_id"], connection)
            now = to_storage(self.clock.now())
            new_version_no = int(package["current_version_no"]) + 1
            materials = self._materials(connection, package["id"])
            items = [self._item_input(connection, row) for row in connection.execute("SELECT * FROM procurement_change_items WHERE change_order_id=? ORDER BY line_no", (change_order_id,)).fetchall()]
            simulated = self._simulate(materials, items)
            # 并行变更可能已经改变基线：连同其它待决占用一起复核两道上限。
            self._guard_budget(connection, package, 0, exclude_change_order_id=change_order_id, projected_total=simulated["total"])
            self._apply_items(connection, package["id"], items, new_version_no, change_order_id, now)
            manifest = [self._manifest_entry(entry) for entry in simulated["entries"].values()]
            connection.execute(
                "INSERT INTO procurement_package_versions(package_id,version_no,state,source_change_order_id,manifest_json,total_amount,created_by,created_at) VALUES(?,?,'effective',?,?,?,?,?)",
                (package["id"], new_version_no, change_order_id, json.dumps(manifest, ensure_ascii=False, sort_keys=True), simulated["total"], actor, now),
            )
            connection.execute(
                "UPDATE procurement_package_versions SET state='superseded',superseded_at=? WHERE package_id=? AND state='effective' AND version_no<>?",
                (now, package["id"], new_version_no),
            )
            connection.execute(
                "UPDATE procurement_packages SET committed_amount=?,current_version_no=?,updated_at=? WHERE id=?",
                (simulated["total"], new_version_no, now, package["id"]),
            )
            # 采购包累计承诺额调整为新总额，本变更单的预占同步释放。
            connection.execute(
                "UPDATE fund_commitments SET amount=? WHERE package_id=? AND change_order_id IS NULL AND state='held'",
                (simulated["total"], package["id"]),
            )
            connection.execute(
                "UPDATE fund_commitments SET state='released',released_at=? WHERE change_order_id=? AND state='held'",
                (now, change_order_id),
            )
            connection.execute(
                "UPDATE procurement_change_orders SET state='effective',effective_version_no=?,budget_delta=?,reserved_amount=0,decided_by=?,decided_at=?,decision_note=?,updated_at=? WHERE id=?",
                (new_version_no, simulated["delta"], actor, now, note, now, change_order_id),
            )
            self._event(connection, "procurement_change_order", change_order_id, "approved", actor, {"version_no": new_version_no, "total": simulated["total"], "delta": simulated["delta"]}, now)
            self._event(connection, "procurement_package", package["id"], "version_effective", actor, {"version_no": new_version_no, "change_order_code": order["code"]}, now)
            return self.change_order_detail(change_order_id, connection)

    def reject_change_order(self, change_order_id: int, actor: str, note: str) -> dict[str, Any]:
        return self._decide_change_order(change_order_id, actor, note, "rejected", "rejected")

    def withdraw_change_order(self, change_order_id: int, actor: str, reason: str) -> dict[str, Any]:
        return self._decide_change_order(change_order_id, actor, reason, "withdrawn", "withdrawn")

    def _decide_change_order(self, change_order_id: int, actor: str, note: str, state: str, event_type: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            order = self._change_order_row(change_order_id, connection)
            if order["state"] != "pending":
                raise ConflictError("只有待决变更单可以驳回或撤回")
            now = to_storage(self.clock.now())
            connection.execute("UPDATE fund_commitments SET state='released',released_at=? WHERE change_order_id=? AND state='held'", (now, change_order_id))
            connection.execute(
                f"UPDATE procurement_change_orders SET state=?,reserved_amount=0,decided_by=?,decided_at=?,decision_note=?,updated_at=? WHERE id=?",
                (state, actor, now, note, now, change_order_id),
            )
            self._event(connection, "procurement_change_order", change_order_id, event_type, actor, {"note": note}, now)
            return self.change_order_detail(change_order_id, connection)

    # ------------------------------------------------------------- simulation

    def _simulate(self, materials: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
        """把变更逐项施加到当前清单上，返回新清单、总额与预算增量。

        已验收数量是不可追溯改小的地板：调减和替代都不得使有效数量低于它。
        """
        entries: dict[str, dict[str, Any]] = {}
        for material in materials:
            entries[material["material_code"]] = {
                "material_code": material["material_code"],
                "material_name": material["material_name"],
                "material_spec": material["material_spec"],
                "material_unit": material["material_unit"],
                "quantity": float(material["quantity"]),
                "unit_price": float(material["unit_price"]),
                "status": "substituted" if material["substituted_by_change_order_id"] else "active",
            }
        for item in items:
            kind = item["kind"]
            code = item["material_code"]
            if kind == "add":
                if code in entries:
                    raise ValidationError(f"新增材料已在清单中：{code}")
                entries[code] = {
                    "material_code": code,
                    "material_name": item["material_name"],
                    "material_spec": item["material_spec"],
                    "material_unit": item["material_unit"],
                    "quantity": float(item["quantity"]),
                    "unit_price": float(item["material_unit_price"]),
                    "status": "active",
                }
                continue
            target = entries.get(code)
            if target is None:
                raise ValidationError(f"变更材料不在当前清单中：{code}")
            if target["status"] == "substituted":
                raise ConflictError(f"材料已被替代，不能继续变更：{code}")
            if kind == "increase":
                target["quantity"] += float(item["quantity"])
            elif kind == "decrease":
                new_quantity = _money(target["quantity"] - item["quantity"])
                accepted = self._accepted(materials, code)
                if new_quantity < -_CENTS:
                    raise ValidationError(f"调减数量超过当前清单数量：{code}")
                if new_quantity + _CENTS < accepted:
                    raise ConflictError(f"调减后数量低于已验收数量：{code}", context={"ordered_after": new_quantity, "accepted": accepted})
                target["quantity"] = new_quantity
            elif kind == "substitute":
                replacement = item["substitute_code"]
                if replacement == code:
                    raise ValidationError("替代材料不能与原材料相同")
                if replacement in entries:
                    raise ConflictError(f"替代材料已在清单中：{replacement}")
                accepted = self._accepted(materials, code)
                if target["quantity"] + _CENTS < accepted:
                    raise ConflictError(f"原材料数量低于已验收数量，不能替代：{code}")
                # 已验收部分留在原材料行（冻结为验收地板），其余由替代材料承接。
                target["quantity"] = _money(accepted)
                target["status"] = "substituted"
                entries[replacement] = {
                    "material_code": replacement,
                    "material_name": item["substitute_name"] or "",
                    "material_spec": item["substitute_spec"] or "",
                    "material_unit": item["substitute_unit"] or "",
                    "quantity": float(item["quantity"]),
                    "unit_price": float(item["substitute_unit_price"]),
                    "status": "active",
                }
        total = _money(sum(self._line_total_entry(entry) for entry in entries.values()))
        committed = _money(sum(self._line_total_material(material) for material in materials))
        delta = _money(total - committed)
        return {"entries": entries, "total": total, "delta": delta, "reserved": max(delta, 0.0)}

    def _apply_items(self, connection: sqlite3.Connection, package_id: int, items: list[dict[str, Any]], version_no: int, change_order_id: int, now: str) -> None:
        for item in items:
            kind = item["kind"]
            code = item["material_code"]
            if kind == "add":
                connection.execute(
                    "INSERT INTO procurement_material_lines(package_id,material_code,material_name,material_spec,material_unit,quantity,unit_price,accepted_qty,introduced_version_no,introduced_change_order_id,last_change_version_no,last_change_change_order_id,created_at,updated_at) VALUES(?,?,?,?,?, ?,?,0,?,?,?,?,?,?)",
                    (package_id, code, item["material_name"], item["material_spec"], item["material_unit"], item["quantity"], item["material_unit_price"], version_no, change_order_id, version_no, change_order_id, now, now),
                )
                continue
            row = connection.execute("SELECT * FROM procurement_material_lines WHERE package_id=? AND material_code=?", (package_id, code)).fetchone()
            if row["substituted_by_change_order_id"] is not None:
                raise ConflictError(f"材料已被替代，不能继续变更：{code}")
            if kind == "increase":
                connection.execute(
                    "UPDATE procurement_material_lines SET quantity=?,last_change_version_no=?,last_change_change_order_id=?,updated_at=? WHERE id=?",
                    (_money(row["quantity"] + item["quantity"]), version_no, change_order_id, now, row["id"]),
                )
            elif kind == "decrease":
                new_quantity = _money(row["quantity"] - item["quantity"])
                if new_quantity + _CENTS < float(row["accepted_qty"]):
                    raise ConflictError(f"调减后数量低于已验收数量：{code}", context={"ordered_after": new_quantity, "accepted": row["accepted_qty"]})
                connection.execute(
                    "UPDATE procurement_material_lines SET quantity=?,last_change_version_no=?,last_change_change_order_id=?,updated_at=? WHERE id=?",
                    (new_quantity, version_no, change_order_id, now, row["id"]),
                )
            elif kind == "substitute":
                replacement = item["substitute_code"]
                floor = _money(row["accepted_qty"])
                connection.execute(
                    "UPDATE procurement_material_lines SET quantity=?,substituted_by_change_order_id=?,last_change_version_no=?,last_change_change_order_id=?,updated_at=? WHERE id=?",
                    (floor, change_order_id, version_no, change_order_id, now, row["id"]),
                )
                connection.execute(
                    "INSERT INTO procurement_material_lines(package_id,material_code,material_name,material_spec,material_unit,quantity,unit_price,accepted_qty,introduced_version_no,introduced_change_order_id,last_change_version_no,last_change_change_order_id,created_at,updated_at) VALUES(?,?,?,?,?, ?,?,0,?,?,?,?,?,?)",
                    (package_id, replacement, item["substitute_name"] or "", item["substitute_spec"] or "", item["substitute_unit"] or "", item["quantity"], item["substitute_unit_price"], version_no, change_order_id, version_no, change_order_id, now, now),
                )

    def _guard_budget(self, connection: sqlite3.Connection, package: sqlite3.Row, extra_reserved: float, *, exclude_change_order_id: int | None, projected_total: float | None = None) -> None:
        """预算上限与专项资金两道闸门，并行待决占用全部计入。"""
        other_pending = connection.execute(
            "SELECT COALESCE(SUM(reserved_amount),0) FROM procurement_change_orders WHERE package_id=? AND state='pending' AND id IS NOT ?",
            (package["id"], exclude_change_order_id),
        ).fetchone()[0]
        fund = connection.execute("SELECT total_amount FROM special_funds WHERE id=?", (package["fund_id"],)).fetchone()
        if projected_total is None:
            # 提交待决：包基线承诺保留，再为本次正向增量预占资金。
            cap_required = float(package["committed_amount"]) + float(other_pending) + float(extra_reserved)
            if cap_required > float(package["budget_cap"]) + _CENTS:
                raise ConflictError("变更后承诺将越过采购包预算上限", context={"budget_cap": package["budget_cap"], "required": _money(cap_required), "pending_reserved": _money(other_pending)})
            other_held = connection.execute(
                "SELECT COALESCE(SUM(amount),0) FROM fund_commitments WHERE fund_id=? AND state='held' "
                "AND (change_order_id IS NULL OR change_order_id<>?)",
                (package["fund_id"], exclude_change_order_id),
            ).fetchone()[0]
            fund_required = float(other_held) + float(extra_reserved)
        else:
            # 批准生效：本包冻结承诺行将调整为新总额，本变更单预占释放，其余预占保留。
            cap_required = float(projected_total) + float(other_pending)
            if cap_required > float(package["budget_cap"]) + _CENTS:
                raise ConflictError("变更后承诺将越过采购包预算上限", context={"budget_cap": package["budget_cap"], "required": _money(cap_required), "pending_reserved": _money(other_pending)})
            other_held = connection.execute(
                "SELECT COALESCE(SUM(c.amount),0) FROM fund_commitments c WHERE c.fund_id=? AND c.state='held' "
                "AND (c.change_order_id IS NULL OR c.change_order_id<>?) "
                "AND NOT (c.change_order_id IS NULL AND c.package_id=?)",
                (package["fund_id"], exclude_change_order_id, package["id"]),
            ).fetchone()[0]
            fund_required = float(projected_total) + float(other_held)
        if fund_required > float(fund["total_amount"]) + _CENTS:
            raise ConflictError("专项资金余额不足以承诺本次变更", context={"fund_total": fund["total_amount"], "required": _money(fund_required)})

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _line_total_entry(entry: dict[str, Any]) -> float:
        return _money(entry["quantity"] * entry["unit_price"])

    @staticmethod
    def _line_total_material(material: dict[str, Any]) -> float:
        return _money(float(material["quantity"]) * float(material["unit_price"]))

    @staticmethod
    def _accepted(materials: list[dict[str, Any]], code: str) -> float:
        for material in materials:
            if material["material_code"] == code:
                return float(material["accepted_qty"])
        return 0.0

    @staticmethod
    def _manifest_entry(entry: dict[str, Any]) -> dict[str, Any]:
        result = dict(entry)
        result["line_total"] = _money(float(entry["quantity"]) * float(entry["unit_price"]))
        return result

    def _materials(self, connection: sqlite3.Connection, package_id: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT l.*,o1.code AS introduced_change_order_code,o2.code AS last_change_change_order_code,o3.code AS substituted_by_change_order_code "
            "FROM procurement_material_lines l "
            "LEFT JOIN procurement_change_orders o1 ON o1.id=l.introduced_change_order_id "
            "LEFT JOIN procurement_change_orders o2 ON o2.id=l.last_change_change_order_id "
            "LEFT JOIN procurement_change_orders o3 ON o3.id=l.substituted_by_change_order_id "
            "WHERE l.package_id=? ORDER BY l.id",
            (package_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["line_total"] = _money(float(row["quantity"]) * float(row["unit_price"]))
            result.append(item)
        return result

    @staticmethod
    def _version_header(connection: sqlite3.Connection, package_id: int, version_no: int) -> dict[str, Any] | None:
        if not version_no:
            return None
        row = connection.execute("SELECT version_no,state,source_change_order_id,total_amount,created_by,created_at FROM procurement_package_versions WHERE package_id=? AND version_no=?", (package_id, version_no)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _signoffs(connection: sqlite3.Connection, package_id: int, change_order_id: int | None) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(
            "SELECT signer_role,signer,note,signed_at FROM procurement_signoffs WHERE package_id=? AND change_order_id IS ? ORDER BY signer_role,id",
            (package_id, change_order_id),
        ).fetchall()]

    @staticmethod
    def _item_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _item_input(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        del connection
        return dict(row)

    @staticmethod
    def _unique_roles(roles: list[str]) -> list[str]:
        cleaned = [role for role in roles if role]
        if len(cleaned) != len(set(cleaned)):
            raise ValidationError("必需签署角色不能重复")
        return cleaned

    @staticmethod
    def _signoff_map(signoffs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for signoff in signoffs:
            result[signoff["signer_role"]] = signoff
        return result

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM temple_sites WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    def _package_row(self, code: str, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM procurement_packages WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("采购包不存在")
        return row

    def _package_by_id(self, package_id: int, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM procurement_packages WHERE id=?", (package_id,)).fetchone()
        if row is None:
            raise NotFoundError("采购包不存在")
        return row

    def _change_order_row(self, change_order_id: int, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM procurement_change_orders WHERE id=?", (change_order_id,)).fetchone()
        if row is None:
            raise NotFoundError("变更单不存在")
        return row

    def _revision_chain(self, connection: sqlite3.Connection, order: sqlite3.Row) -> list[dict[str, Any]]:
        # 沿 resubmits_change_order_id 先找到链首，再顺着重提关系向下走完整条链。
        head = order
        while head["resubmits_change_order_id"]:
            parent = connection.execute("SELECT * FROM procurement_change_orders WHERE id=?", (head["resubmits_change_order_id"],)).fetchone()
            if parent is None:
                break
            head = parent
        chain: list[dict[str, Any]] = []
        current = head
        while current is not None:
            chain.append({"id": current["id"], "code": current["code"], "title": current["title"], "revision_no": current["revision_no"], "state": current["state"]})
            child = connection.execute(
                "SELECT * FROM procurement_change_orders WHERE resubmits_change_order_id=? ORDER BY revision_no LIMIT 1",
                (current["id"],),
            ).fetchone()
            current = child
        return chain if len(chain) > 1 else []

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
