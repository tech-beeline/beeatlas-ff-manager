# Copyright (c) 2024 PJSC VimpelCom
"""Контрактные тесты: имя фитнес-функции в ответах API."""
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main


def _ff_row(
    ff_id: int,
    code: str,
    description,
    status: str = "ADOPT",
    *,
    method: str = "http://example/{code}",
    method_synchronous: bool = True,
    applicability=None,
) -> dict:
    return {
        "id": ff_id,
        "code": code,
        "description": description,
        "applicability": applicability,
        "auxiliary_check": False,
        "status": status,
        "script": None,
        "method": method.format(code=code) if method else None,
        "method_synchronous": method_synchronous,
        "created_date": None,
        "updated_date": None,
    }


class TestFfNameFromRow(unittest.TestCase):
    def test_none_row(self):
        self.assertIsNone(main._ff_name_from_row(None))

    def test_missing_or_blank_description(self):
        self.assertIsNone(main._ff_name_from_row({}))
        self.assertIsNone(main._ff_name_from_row({"description": None}))
        self.assertIsNone(main._ff_name_from_row({"description": ""}))
        self.assertIsNone(main._ff_name_from_row({"description": "   "}))

    def test_trimmed_description(self):
        self.assertEqual(main._ff_name_from_row({"description": "  Проверка  "}), "Проверка")
        self.assertEqual(main._ff_name_from_row({"description": 123}), "123")


class TestRunAllWorkspaceName(unittest.TestCase):
    def test_results_and_skipped_have_name(self):
        rows = [
            _ff_row(1, "A", " Alpha ", "ADOPT"),
            _ff_row(2, "B", None, "TRIAL", method_synchronous=False),
            _ff_row(3, "T", "Test check", "TEST"),
            _ff_row(4, "C", "Gamma", "ADOPT", applicability="A"),
            _ff_row(5, "E", "Epsilon", "ADOPT", applicability="MISSING"),
        ]
        applicabilities = {r["code"]: r["applicability"] for r in rows}

        def prerequisites(_product, applicability, _code_to_id, _branch, extra_passed=None):
            return applicability == "A"

        def run_check(code, _app, **_kwargs):
            return True, f"ok:{code}", {"is_check": True, "details": [], "countDetail": 0, "successDetail": 0}

        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "fetch_structurizr_credentials", return_value=("key", "secret")))
            stack.enter_context(patch.object(main, "upload_workspace_json", return_value=42))
            stack.enter_context(patch.object(main, "get_all_fitness_functions", return_value=rows))
            stack.enter_context(patch.object(main, "get_fitness_function_applicabilities", return_value=applicabilities))
            stack.enter_context(patch.object(main, "get_fitness_function_code_to_id", return_value={r["code"]: r["id"] for r in rows}))
            stack.enter_context(patch.object(main, "get_fitness_function_codes_excluded_from_run_all", return_value={"T"}))
            stack.enter_context(patch.object(main, "list_scripts", return_value=["D"]))
            stack.enter_context(patch.object(main, "_prerequisites_satisfied", side_effect=prerequisites))
            stack.enter_context(patch.object(main, "run_ff_check_no_persist", side_effect=run_check))
            out = main._run_all_workspace_impl("DEMOFF", "main", b"{}", {})

        self.assertEqual(out["docId"], 42)
        self.assertEqual(set(out["results"]), {"A", "C", "D"})
        self.assertEqual(out["results"]["A"]["code"], "A")
        self.assertEqual(out["results"]["A"]["name"], "Alpha")
        self.assertEqual(out["results"]["C"]["name"], "Gamma")
        self.assertIsNone(out["results"]["D"]["name"])
        self.assertIsNone(out["results"]["D"]["status"])

        for code, item in out["results"].items():
            self.assertEqual(item["code"], code)
            for key in ("name", "success", "message", "check_result", "status"):
                self.assertIn(key, item)

        skipped = {item["code"]: item for item in out["skipped"]}
        self.assertEqual(set(skipped), {"B", "E", "T"})
        self.assertIsNone(skipped["B"]["name"])
        self.assertEqual(skipped["E"]["name"], "Epsilon")
        self.assertEqual(skipped["T"]["name"], "Test check")
        for item in skipped.values():
            for key in ("code", "name", "reason"):
                self.assertIn(key, item)


class TestRunAllName(unittest.TestCase):
    def test_results_and_skipped_have_name(self):
        rows = [
            _ff_row(1, "A", " Alpha ", "ADOPT"),
            _ff_row(2, "B", None, "TRIAL", method_synchronous=False),
            _ff_row(3, "T", "Test check", "TEST"),
            _ff_row(4, "C", "Gamma", "ADOPT"),
        ]
        applicabilities = {r["code"]: r["applicability"] for r in rows}

        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "fetch_structurizr_credentials", return_value=("key", "secret")))
            stack.enter_context(patch.object(main, "get_all_fitness_functions", return_value=rows))
            stack.enter_context(patch.object(main, "get_fitness_function_applicabilities", return_value=applicabilities))
            stack.enter_context(patch.object(main, "get_fitness_function_code_to_id", return_value={r["code"]: r["id"] for r in rows}))
            stack.enter_context(patch.object(main, "get_fitness_function_codes_excluded_from_run_all", return_value={"T"}))
            stack.enter_context(patch.object(main, "list_scripts", return_value=[]))
            stack.enter_context(
                patch.object(
                    main,
                    "run_ff_check",
                    return_value=(True, "ok", {"is_check": True, "details": [], "countDetail": 0, "successDetail": 0}),
                )
            )
            out = main.run_all(
                main.RunRequest(app="DEMOFF"),
                docId=None,
                source_type=None,
                source_id=None,
                branch=None,
            )

        self.assertEqual(set(out["results"]), {"A", "B", "C"})
        self.assertEqual(out["results"]["A"]["name"], "Alpha")
        self.assertIsNone(out["results"]["B"]["name"])
        self.assertEqual(out["results"]["C"]["name"], "Gamma")
        self.assertEqual(out["results"]["A"]["status"], "ADOPT")
        self.assertEqual(out["results"]["B"]["status"], "TRIAL")

        skipped = {item["code"]: item for item in out["skipped"]}
        self.assertEqual(set(skipped), {"T"})
        self.assertEqual(skipped["T"]["name"], "Test check")


class TestRunOneName(unittest.TestCase):
    def test_name_present_for_known_ff(self):
        row = _ff_row(1, "A", " Alpha ", "TEST")
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "get_fitness_function_by_code", return_value=row))
            stack.enter_context(patch.object(main, "fetch_structurizr_credentials", return_value=("key", "secret")))
            stack.enter_context(patch.object(main, "run_ff_check", return_value=(True, "ok", {"is_check": True})))
            out = main.run_one(
                "A",
                main.RunRequest(app="DEMOFF"),
                docId=None,
                source_type=None,
                source_id=None,
                branch=None,
            )

        self.assertEqual(out["code"], "A")
        self.assertEqual(out["name"], "Alpha")
        self.assertEqual(out["status"], "TEST")

    def test_name_is_null_without_db_row(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "get_fitness_function_by_code", return_value=None))
            stack.enter_context(patch.object(main, "fetch_structurizr_credentials", return_value=("key", "secret")))
            stack.enter_context(patch.object(main, "run_ff_check", return_value=(True, "ok", {"is_check": True})))
            out = main.run_one(
                "A",
                main.RunRequest(app="DEMOFF"),
                docId=None,
                source_type=None,
                source_id=None,
                branch=None,
            )

        self.assertEqual(out["code"], "A")
        self.assertIsNone(out["name"])
        self.assertNotIn("status", out)


class TestFfCallName(unittest.TestCase):
    def test_ff_name_present_in_pending_call(self):
        row = {
            "call_id": "call-1",
            "ff_code": "A",
            "ff_name": "Alpha",
            "product_code": "DEMOFF",
            "branch": "main",
            "ff_status": "TRIAL",
            "status": "call",
        }
        with patch.object(main, "get_outside_ff_call", return_value=row):
            out = main.get_ff_call_result("call-1")

        self.assertEqual(out["ff_code"], "A")
        self.assertEqual(out["ff_name"], "Alpha")
        self.assertEqual(out["call_status"], "pending")


if __name__ == "__main__":
    unittest.main()
