import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auditor import database, engine  # noqa: E402


class EngineUnitTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
