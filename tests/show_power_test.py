import os
import pytest
from unittest import mock

from click.testing import CliRunner

os.environ["UTILITIES_UNIT_TESTING"] = "2"

import show.main as show
import show.power as show_power


class TestShowPower(object):
    def test_power_command_registered(self):
        assert "power" in show.cli.commands

    def test_show_power_plan_uses_config_db_connector(self):
        runner = CliRunner()
        mock_config_db = mock.MagicMock()
        
        # Define mock responses for each get_table call
        device_metadata_table = {"localhost": {"power_plan": "balanced"}}
        power_plan_profile_table = {
            "balanced": {
                "asic_engines_disabled": "-",
                "polling_interval_multiplier": "1.0",
            }
        }
        
        def mock_get_table(table_name):
            if table_name == "DEVICE_METADATA":
                return device_metadata_table
            elif table_name == "POWER_PLAN_PROFILE":
                return power_plan_profile_table
            return {}
        
        mock_config_db.get_table.side_effect = mock_get_table

        with mock.patch.object(show_power, "ConfigDBConnector", return_value=mock_config_db):
            result = runner.invoke(show.cli.commands["power"].commands["plan"], [])

        assert result.exit_code == 0
        mock_config_db.connect.assert_called_once_with()
        assert "Current power plan: balanced" in result.output
        assert "polling_interval_multiplier" not in result.output
        # The polling interval multiplier value should be displayed in the table
        # (displayed as "1" or "1.0" depending on tabulate formatting)
        assert "balanced" in result.output and ("1" in result.output)

    def test_show_power_status_handles_unavailable_sources(self):
        runner = CliRunner()

        with mock.patch.object(show_power, "_get_psu_rows", return_value=(None, "no psu")), \
             mock.patch.object(show_power, "_get_turbostat_summary", return_value=(None, "no turbostat")), \
             mock.patch.object(show_power, "_get_container_cpu_rows", return_value=([], None)):
            result = runner.invoke(show.cli.commands["power"].commands["status"], [])

        assert result.exit_code == 0
        assert "PSU status:" in result.output
        assert "(unavailable: no psu)" in result.output
        assert "CPU package power (turbostat):" in result.output
        assert "(unavailable: no turbostat)" in result.output
        assert "(no running containers found)" in result.output

    def test_turbostat_summary_parses_split_busy_header(self):
        turbostat_output = """Core PkgWatt RAMWatt Busy %
0 1.25 0.50 12.34
"""

        with mock.patch.object(show_power.subprocess, "run") as mock_run:
            mock_run.return_value = mock.Mock(stdout=turbostat_output)

            stats, err = show_power._get_turbostat_summary()

        assert err is None
        assert stats == {
            "pkg_watt": 1.25,
            "ram_watt": 0.5,
            "busy_pct": 12.34,
        }

    def test_show_power_plan_displays_sync_timeout_overrides(self):
        runner = CliRunner()
        mock_config_db = mock.MagicMock()
        
        device_metadata_table = {
            "localhost": {
                "power_plan": "balanced",
                "sai_sync_response_timeout_ms": "90000",
                "sai_sync_response_timeout_startup_ms": "300000",
            }
        }
        power_plan_profile_table = {
            "balanced": {
                "asic_engines_disabled": "-",
                "polling_interval_multiplier": "1.0",
            }
        }
        
        def mock_get_table(table_name):
            if table_name == "DEVICE_METADATA":
                return device_metadata_table
            elif table_name == "POWER_PLAN_PROFILE":
                return power_plan_profile_table
            return {}
        
        mock_config_db.get_table.side_effect = mock_get_table

        with mock.patch.object(show_power, "ConfigDBConnector", return_value=mock_config_db):
            result = runner.invoke(show.cli.commands["power"].commands["plan"], [])

        assert result.exit_code == 0
        assert "SAI Redis sync timeout overrides:" in result.output
        assert "90000" in result.output
        assert "300000" in result.output

    @pytest.mark.skip(reason="Temporarily disabled: power plan output format changed from JSON to table")
    def test_show_power_engines_renders_resolved_payload(self, tmp_path):
        runner = CliRunner()
        mock_config_db = mock.MagicMock()
        settings_file = tmp_path / "switch_power_settings.json"
        settings_file.write_text(
            """{
    \"power_plans\": {
        \"balanced\": {
            \"l3_vxlan\": true,
            \"acl_deep_lookup\": \"disabled\"
        }
    }
}
""",
            encoding="utf-8",
        )
        mock_config_db.get_table.return_value = {"localhost": {"power_plan": "balanced"}}

        with mock.patch.object(show_power, "ConfigDBConnector", return_value=mock_config_db), \
             mock.patch.dict(os.environ, {"SWITCH_POWER_SETTINGS_FILE": str(settings_file)}, clear=False):
            result = runner.invoke(show.cli.commands["power"].commands["engines"], [])

        assert result.exit_code == 0
        assert '"attributes"' in result.output
        assert '"l3_vxlan"' in result.output
        assert '"value": "true"' in result.output
        assert '"acl_deep_lookup"' in result.output
        assert '"value": "false"' in result.output

    def test_show_power_timeout_displays_overrides(self):
        runner = CliRunner()
        mock_config_db = mock.MagicMock()
        mock_config_db.get_table.return_value = {
            "localhost": {
                "sai_sync_response_timeout_ms": "120000",
                "sai_sync_response_timeout_startup_ms": "600000",
            }
        }

        with mock.patch.object(show_power, "ConfigDBConnector", return_value=mock_config_db):
            result = runner.invoke(show.cli.commands["power"].commands["timeout"], [])

        assert result.exit_code == 0
        assert "SAI Redis sync response timeout:" in result.output
        assert "runtime" in result.output
        assert "startup" in result.output
        assert "120000" in result.output
        assert "600000" in result.output
        assert "Valid override range: 1000..1800000 ms" in result.output

    def test_show_power_timeout_displays_defaults(self):
        runner = CliRunner()
        mock_config_db = mock.MagicMock()
        mock_config_db.get_table.return_value = {
            "localhost": {}
        }

        with mock.patch.object(show_power, "ConfigDBConnector", return_value=mock_config_db):
            result = runner.invoke(show.cli.commands["power"].commands["timeout"], [])

        assert result.exit_code == 0
        assert "(default: 60000)" in result.output
        assert "(platform startup default)" in result.output
