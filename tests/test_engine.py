import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auditor import database, engine  # noqa: E402


class EngineUnitTests(unittest.TestCase):
    def test_ooxml_workbook_with_legacy_xls_name_is_read_by_content(self):
        columns = [
            "Número do receituário", "Data de Emissão", "Produto", "Dose",
            "Tipo de Dosagem", "Nome RT",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ReceitasEmitidas.xls"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(columns)
            sheet.append(["R-1", "28/08/2026", "PRODUTO A", 60, "mL/ha", "RT TESTE"])
            workbook.save(path)

            rows = engine._read_source("agrotis_recipe", path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1]["Número do receituário"], "R-1")

    def test_dates_are_day_first_and_dose_is_converted_before_area(self):
        self.assertEqual(engine.iso_date("04/12/2025 21:50"), "2025-12-04")
        self.assertEqual(engine._normalized_dose(60, "mL/ha"), (0.06, "L/ha"))
        dose, _ = engine._normalized_dose(60, "mL/ha")
        self.assertAlmostEqual(40 / dose, 666.6666666667)

    def test_direction_and_series_are_derived_deterministically(self):
        self.assertEqual(engine._expected_direction("sap_entry_history"), "1")
        self.assertEqual(engine._expected_direction("sap_exit_current"), "2")
        self.assertEqual(engine._expected_direction("sap_exit_current", "Entrada"), "1")
        self.assertEqual(engine._series(1.0), "1")
        self.assertEqual(engine._series("001.0"), "1")

    def test_entry_origin_distinguishes_internal_transfer_from_supplier(self):
        internal = engine._entry_origin({
            "Nome": "BOA ESPERANCA AGROPECUARIA LTDA",
            "CNPJ": "01.722.958/0012-01",
        })
        supplier = engine._entry_origin({
            "Nome": "SYNGENTA PROTECAO DE CULTIVOS LTDA",
            "CNPJ": "60.744.463/0041-87",
        })
        self.assertEqual(internal["tipo_entrada"], "TRANSFERENCIA_RETORNO")
        self.assertEqual(supplier["tipo_entrada"], "ENTRADA_FORNECEDOR")

    def test_return_chain_lists_all_previous_exits_for_the_lot(self):
        exits = [
            {
                "id": 10, "nf": "201", "series": "1", "doc_date": "2025-12-03",
                "cnpj": "01722958001201", "center": "0714", "sap_material": "PRODUTO A",
                "material_key": "PRODUTO A", "lot": "LOTE-A", "manufacturer_lot": "FAB-A",
                "quantity": 15.0, "unit": "L", "actual_id": 110,
            },
            {
                "id": 11, "nf": "202", "series": "1", "doc_date": "2025-12-02",
                "cnpj": "01722958001201", "center": "0714", "sap_material": "PRODUTO A",
                "material_key": "PRODUTO A", "lot": "LOTE-A", "manufacturer_lot": "FAB-A",
                "quantity": 25.0, "unit": "L", "actual_id": 111,
            },
            {
                "id": 12, "nf": "203", "series": "1", "doc_date": "2025-12-01",
                "cnpj": "01722958001201", "center": "0714", "sap_material": "PRODUTO A",
                "material_key": "PRODUTO A", "lot": "OUTRO", "manufacturer_lot": "OUTRO",
                "quantity": 99.0, "unit": "L", "actual_id": 112,
            },
        ]
        actuals = {
            110: {"id": 110, "nf": "201", "product": "PRODUTO A", "lot": "FAB-A", "quantity": 1, "volume": 15, "unit": "L"},
            111: {"id": 111, "nf": "202", "product": "PRODUTO A", "lot": "FAB-A", "quantity": 1, "volume": 25, "unit": "L"},
            112: {"id": 112, "nf": "203", "product": "PRODUTO A", "lot": "OUTRO", "quantity": 1, "volume": 99, "unit": "L"},
        }
        chain = engine._return_chain_for_item(None, 1, {
            "direction": "1", "material_key": "PRODUTO A", "doc_date": "2025-12-04",
            "nf": "300", "manufacturer_lot": "FAB-A", "lot": "LOTE-A",
            "quantity": 40.0, "unit": "L", "cnpj": "01722958001201",
        }, {"exits_by_material": {"PRODUTO A": exits}, "actuals": actuals})
        self.assertEqual(chain["status"], "PENDENTE_ESTORNO")
        self.assertEqual([item["nf"] for item in chain["originals"]], ["201", "202"])
        self.assertEqual({item["id"] for item in chain["movements"]}, {110, 111})
        self.assertEqual(chain["matched_quantity"], 40.0)
        self.assertEqual(chain["remaining_quantity"], 0.0)

    def test_recipe_fields_are_read_by_column_name_not_position(self):
        raw = {
            "Nome RT": "KARLA DANIELLY GARCIA DE LIMA",
            "Quantidade": "40.00",
            "Diagnóstico": "Diagnóstico X",
            "Produto": "PRODUTO A",
            "Tipo de Dosagem": "mL/ha",
            "Data de Emissão": "03/12/2025",
            "Dose": "60.000",
            "Número do receituário": "BR2025TESTE",
            "Nome da Propriedade": "FAZENDA TESTE",
            "ART": "ART-1",
        }

        class RecipeConnection:
            def execute(self, query, params):
                return [{"raw_json": json.dumps(raw)}]

        engine.RECIPE_CACHE.pop(987654, None)
        recipe = engine._recipes_for_regularization(RecipeConnection(), 987654)[0]
        self.assertEqual(recipe["numero_receita"], "BR2025TESTE")
        self.assertEqual(recipe["nome_rt"], "KARLA DANIELLY GARCIA DE LIMA")
        self.assertEqual(recipe["dose_recomendada"], 60.0)
        self.assertEqual(recipe["quantidade_receita"], 40.0)

    def test_recipe_fields_accept_legacy_xls_replacement_characters(self):
        raw = {
            "Data de Emiss�o": "2026-06-01 08:00:00.0",
            "Diagn�stico": "ALVO TESTE",
            "N�mero do receitu�rio": "BR2026LEGADO",
            "Produto": "PRODUTO A",
            "�rea": "666,67",
        }

        class RecipeConnection:
            postgres = False

            def execute(self, query, params):
                return [{"id": 1, "row_number": 1, "raw_json": json.dumps(raw)}]

        engine.RECIPE_CACHE.clear()
        recipe = engine._recipes_for_regularization(RecipeConnection(), 987655)[0]
        self.assertEqual(recipe["data_emissao"], "2026-06-01")
        self.assertEqual(recipe["diagnostico"], "ALVO TESTE")
        self.assertEqual(recipe["numero_receita"], "BR2026LEGADO")
        self.assertEqual(recipe["area_receita"], 666.67)

    @unittest.skipUnless((ROOT / "dados" / "Relatório Saldo de Agrotóxico.pdf").exists(), "PDF de exemplo ausente")
    def test_pdf_parser_matches_printed_report_totals(self):
        rows = engine._parse_sisdev_stock_pdf(ROOT / "dados" / "Relatório Saldo de Agrotóxico.pdf")
        self.assertEqual(len(rows), 281)
        self.assertEqual(sum(row["QUANTIDADE"] for row in rows), 103518)
        self.assertEqual(sum(row["VOLUME"] for row in rows), 731844)
        self.assertEqual({row["CNPJ"] for row in rows}, {"01722958001473"})


class EngineDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(database, "DB_PATH", Path(self.temp.name) / "test.sqlite")
        self.env_patch = mock.patch.dict(os.environ, {"DATABASE_URL": ""})
        self.db_patch.start()
        self.env_patch.start()
        engine.RECIPE_CACHE.clear()

    def tearDown(self):
        self.env_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def _path(self, name):
        path = Path(self.temp.name) / name
        path.touch()
        return path

    @staticmethod
    def _sap_row(lot="ABC1234"):
        return {
            "Número de nota fiscal eletrônica": 123.0,
            "Séries": 1.0,
            "Data documento": "04/12/2025",
            "Texto breve material": "PRODUTO A",
            "Lote": lot,
            "Lote Fabricante": "XJSD8312",
            "Quantidade": 40.0,
            "UMB": "L",
            "Centro": "0714",
            "CNPJ": "01.722.958/0014-73",
        }

    @staticmethod
    def _sisdev_row(lot="XJSD8312"):
        return {
            "Nº NF": 123,
            "SÉRIE NF": "1",
            "TIPO MOVIMENTO": "Saída por transferência",
            "DATA MOVIMENTO": "04/12/2025",
            "DATA NF": "04/12/2025",
            "PRODUTO": "PRODUTO A",
            "LOTE": lot,
            "QNT": -2.0,
            "VOLUME": 20.0,
            "U.M.": "L",
            "CPF/CNPJ REVENDA": "01.722.958/0014-73",
            "SITUAÇÃO": "Lançado",
        }

    def test_bulk_import_deduplicates_exact_sisdev_export_rows(self):
        run_id = engine.create_import_run()
        rows = [(4, self._sisdev_row()), (5, dict(self._sisdev_row()))]
        progress = []
        with mock.patch.object(engine, "_read_source", return_value=rows):
            result = engine.import_source(
                run_id, "sisdev_movement", self._path("movement.xlsx"), batch_size=1,
                progress_callback=progress.append,
            )
        self.assertEqual(result["imported_rows"], 1)
        self.assertEqual(result["duplicate_rows"], 1)
        self.assertEqual(progress[-1]["processed_rows"], 2)
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM actual_movements").fetchone()[0], 1)

    def test_reconciliation_never_reuses_one_sisdev_movement(self):
        run_id = engine.create_import_run()
        sources = {
            "sap_exit_current": [(2, self._sap_row()), (3, dict(self._sap_row()))],
            "sisdev_movement": [(4, self._sisdev_row())],
        }
        with mock.patch.object(engine, "_read_source", side_effect=lambda source, path: sources[source]):
            engine.import_source(run_id, "sap_exit_current", self._path("exit.xlsx"))
            engine.import_source(run_id, "sisdev_movement", self._path("movement.xlsx"))
        engine.reconcile_run(run_id, require_complete=False)
        with database.connect() as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM reconciliations WHERE expected_id IS NOT NULL ORDER BY id"
            )]
            direction = conn.execute("SELECT DISTINCT direction FROM expected_movements").fetchone()[0]
        self.assertEqual(statuses.count("CORRETO"), 1)
        self.assertEqual(statuses.count("NAO_LANCADO"), 1)
        self.assertEqual(direction, "2")

    def test_reconciliation_aggregates_split_sap_lines_before_matching_other_lots(self):
        run_id = engine.create_import_run()
        sap_first = self._sap_row(lot="0622258820")
        sap_first.update({"Lote Fabricante": "0622258820", "Quantidade": 340.0})
        sap_second = dict(sap_first)
        sap_second["Quantidade"] = 1200.0
        sap_other = self._sap_row(lot="0015268820")
        sap_other.update({"Lote Fabricante": "0015268820", "Quantidade": 2920.0})
        sisdev_first = self._sisdev_row(lot="0622258820")
        sisdev_first.update({"QNT": -77.0, "VOLUME": 20.0})
        sisdev_other = self._sisdev_row(lot="0015268820")
        sisdev_other.update({"QNT": -146.0, "VOLUME": 20.0})
        sources = {
            "sap_exit_current": [(2, sap_first), (3, sap_second), (4, sap_other)],
            "sisdev_movement": [(5, sisdev_first), (6, sisdev_other)],
        }
        with mock.patch.object(engine, "_read_source", side_effect=lambda source, path: sources[source]):
            engine.import_source(run_id, "sap_exit_current", self._path("exit.xlsx"))
            engine.import_source(run_id, "sisdev_movement", self._path("movement.xlsx"))
        engine.reconcile_run(run_id, require_complete=False)

        with database.connect() as conn:
            rows = conn.execute(
                """SELECT r.status,r.actual_id,r.details_json,e.manufacturer_lot
                   FROM reconciliations r JOIN expected_movements e ON e.id=r.expected_id
                   ORDER BY e.id"""
            ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["CORRETO", "CORRETO", "CORRETO"])
        self.assertEqual(rows[0]["actual_id"], rows[1]["actual_id"])
        self.assertNotEqual(rows[1]["actual_id"], rows[2]["actual_id"])
        grouped_details = json.loads(rows[0]["details_json"])
        self.assertEqual(grouped_details["aggregated_expected_rows"], 2)
        self.assertEqual(grouped_details["aggregated_expected_quantity"], 1540.0)

    def test_manufacturer_lot_difference_is_not_classified_correct(self):
        run_id = engine.create_import_run()
        sources = {
            "sap_exit_current": [(2, self._sap_row())],
            "sisdev_movement": [(4, self._sisdev_row(lot="ABC1234"))],
        }
        with mock.patch.object(engine, "_read_source", side_effect=lambda source, path: sources[source]):
            engine.import_source(run_id, "sap_exit_current", self._path("exit.xlsx"))
            engine.import_source(run_id, "sisdev_movement", self._path("movement.xlsx"))
        engine.reconcile_run(run_id, require_complete=False)
        with database.connect() as conn:
            status = conn.execute(
                "SELECT status FROM reconciliations WHERE expected_id IS NOT NULL"
            ).fetchone()[0]
        self.assertEqual(status, "DIVERGENCIA_LOTE_FABRICANTE")

    def test_regularization_queue_groups_items_by_document_and_loads_detail_on_demand(self):
        first = self._sap_row(lot="LOTE-A")
        second = dict(self._sap_row(lot="LOTE-B"))
        second["Texto breve material"] = "PRODUTO B"
        second["Lote Fabricante"] = "FABRICANTE-B"
        run_id = engine.create_import_run()
        with mock.patch.object(
            engine, "_read_source", return_value=[(2, first), (3, second)],
        ):
            engine.import_source(run_id, "sap_exit_current", self._path("exit.xlsx"))
        engine.reconcile_run(run_id, require_complete=False)

        queue = engine.page_records_v2("regularization", {"page": 1, "per_page": 25})
        self.assertEqual(queue["pagination"]["total"], 1)
        self.assertEqual(len(queue["rows"]), 1)
        document = queue["rows"][0]
        self.assertEqual(document["itens_count"], 2)
        self.assertEqual(document["lotes_count"], 2)
        self.assertNotIn("expected_id", document)

        pending = engine.page_records_v2(
            "regularization", {"page": 1, "per_page": 25, "status": "PENDENTE"},
        )
        self.assertEqual(pending["pagination"]["total"], 1)

        entries = engine.page_records_v2(
            "regularization", {"page": 1, "per_page": 25, "direction": "1"},
        )
        exits = engine.page_records_v2(
            "regularization", {"page": 1, "per_page": 25, "direction": "2"},
        )
        self.assertEqual(entries["pagination"]["total"], 0)
        self.assertEqual(exits["pagination"]["total"], 1)

        detail = engine.regularization_document_detail(document["document_id"])
        self.assertIsNotNone(detail)
        self.assertEqual(len(detail["items"]), 2)
        self.assertEqual({item["produto"] for item in detail["items"]}, {"PRODUTO A", "PRODUTO B"})

    def test_regularization_product_lot_filter_returns_all_related_notes(self):
        first = self._sap_row(lot="LOTE-A")
        first.update({
            "Número de nota fiscal eletrônica": 101,
            "Texto breve material": "PRODUTO A",
            "Lote Fabricante": "FAB-A",
        })
        second = dict(first)
        second["Número de nota fiscal eletrônica"] = 102
        third = self._sap_row(lot="LOTE-B")
        third.update({
            "Número de nota fiscal eletrônica": 103,
            "Texto breve material": "PRODUTO B",
            "Lote Fabricante": "FAB-B",
        })
        run_id = engine.create_import_run()
        with mock.patch.object(
            engine, "_read_source", return_value=[(2, first), (3, second), (4, third)],
        ):
            engine.import_source(run_id, "sap_exit_current", self._path("exit.xlsx"))
        engine.reconcile_run(run_id, require_complete=False)

        result = engine.page_records_v2(
            "regularization",
            {"page": 1, "per_page": 25, "product": "PRODUTO A", "lot": "FAB-A"},
        )

        self.assertEqual(result["pagination"]["total"], 2)
        self.assertEqual({row["numero_nfe"] for row in result["rows"]}, {"000000101", "000000102"})
        labels = {item["label"] for item in result["filter_options"]["product_lots"]}
        self.assertIn("PRODUTO A & FABA", labels)
        self.assertIn("PRODUTO B & FABB", labels)

        typed = engine.page_records_v2(
            "regularization", {"page": 1, "per_page": 25, "lot_search": "fab-a"},
        )
        self.assertEqual(typed["pagination"]["total"], 2)

    def test_regularization_lot_filter_opens_the_complete_document(self):
        first = self._sap_row(lot="LOTE-A")
        first.update({
            "Número de nota fiscal eletrônica": 101,
            "Texto breve material": "PRODUTO A",
            "Lote Fabricante": "FAB-A",
        })
        second = self._sap_row(lot="LOTE-B")
        second.update({
            "Número de nota fiscal eletrônica": 101,
            "Texto breve material": "PRODUTO B",
            "Lote Fabricante": "FAB-B",
        })
        other_document = dict(first)
        other_document["Número de nota fiscal eletrônica"] = 102
        run_id = engine.create_import_run()
        with mock.patch.object(
            engine, "_read_source", return_value=[(2, first), (3, second), (4, other_document)],
        ):
            engine.import_source(run_id, "sap_exit_current", self._path("exit.xlsx"))
        engine.reconcile_run(run_id, require_complete=False)

        filters = {"page": 1, "per_page": 25, "product": "PRODUTO A", "lot": "FAB-A"}
        queue = engine.page_records_v2("regularization", filters)
        document = next(row for row in queue["rows"] if row["numero_nfe"] == "000000101")
        detail = engine.regularization_document_detail(document["document_id"], filters)

        self.assertIsNotNone(detail)
        self.assertEqual(len(detail["items"]), 2)
        self.assertEqual({item["produto"] for item in detail["items"]}, {"PRODUTO A", "PRODUTO B"})
        self.assertEqual({item["lote"] for item in detail["items"]}, {"FABA", "FABB"})

    def test_supplier_entry_has_no_recipe_or_return_chain(self):
        entry = self._sap_row()
        entry.update({
            "Nome": "SYNGENTA PROTECAO DE CULTIVOS LTDA",
            "CNPJ": "60.744.463/0041-87",
        })
        run_id = engine.create_import_run()
        with mock.patch.object(engine, "_read_source", return_value=[(2, entry)]):
            engine.import_source(run_id, "sap_entry_current", self._path("entry.xlsx"))
        engine.reconcile_run(run_id, require_complete=False)

        queue = engine.page_records_v2(
            "regularization", {"page": 1, "per_page": 25, "direction": "1"},
        )
        self.assertEqual(queue["pagination"]["total"], 1)
        document = queue["rows"][0]
        self.assertEqual(document["situacao"], "ENTRADA_FORNECEDOR")
        self.assertEqual(document["tipo_entrada"], "ENTRADA_FORNECEDOR")
        self.assertEqual(document["emitente"], "SYNGENTA PROTECAO DE CULTIVOS LTDA")

        detail = engine.regularization_document_detail(document["document_id"])
        self.assertEqual(detail["recipes"], [])
        self.assertIsNone(detail["return_chain"])
        self.assertNotIn("numero_receita", detail["items"][0])

    def test_sap_launch_status_uses_known_icons_without_guessing(self):
        self.assertEqual(engine._sap_launch_status({"Ícone": "@A0@"}), "LANCADO_MANUALMENTE")
        self.assertEqual(engine._sap_launch_status({"Ícone": "@0V@"}), "LANCADO_AUTOMATICAMENTE")
        self.assertEqual(engine._sap_launch_status({"Ícone": "@1A@"}), "DOCUMENTO_PENDENTE")
        self.assertEqual(engine._sap_launch_status({"Ícone": "desconhecido"}), "NAO_INFORMADO")


if __name__ == "__main__":
    unittest.main()
