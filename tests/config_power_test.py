import os

from click.testing import CliRunner

from utilities_common.db import Db

os.environ["UTILITIES_UNIT_TESTING"] = "2"

import config.main as config


class TestConfigPowerPlan(object):
    def test_power_plan_set_writes_device_metadata_and_profile(self):
        runner = CliRunner()
        db = Db()

        result = runner.invoke(
            config.config.commands['power-plan'].commands['set'],
            [
                'balanced',
                '--polling-interval-multiplier', '1.5',
                '--cpu-cap-important-pct', '40',
                '--cpu-cap-optional-pct', '15',
                '--asic-engines-disabled', 'vxlan, nat ',
            ],
            obj=db,
        )

        assert result.exit_code == 0
        assert db.cfgdb.get_entry('DEVICE_METADATA', 'localhost').get('power_plan') == 'balanced'
        assert db.cfgdb.get_entry('POWER_PLAN_PROFILE', 'balanced') == {
            'polling_interval_multiplier': '1.5',
            'cpu_cap_important_pct': '40',
            'cpu_cap_optional_pct': '15',
            'asic_engines_disabled': 'vxlan,nat',
        }

    def test_power_plan_set_rejects_multiplier_out_of_range(self):
        runner = CliRunner()
        db = Db()

        for value in ['0.05', '10.5']:
            result = runner.invoke(
                config.config.commands['power-plan'].commands['set'],
                ['balanced', '--polling-interval-multiplier', value],
                obj=db,
            )
            assert result.exit_code != 0
            assert db.cfgdb.get_entry('POWER_PLAN_PROFILE', 'balanced') == {}

    def test_power_plan_set_rejects_multiplier_extra_precision(self):
        runner = CliRunner()
        db = Db()

        result = runner.invoke(
            config.config.commands['power-plan'].commands['set'],
            ['balanced', '--polling-interval-multiplier', '1.25'],
            obj=db,
        )

        assert result.exit_code != 0
        assert 'one decimal place' in result.output
        assert db.cfgdb.get_entry('POWER_PLAN_PROFILE', 'balanced') == {}

    def test_power_plan_set_normalizes_integer_multiplier(self):
        runner = CliRunner()
        db = Db()

        result = runner.invoke(
            config.config.commands['power-plan'].commands['set'],
            ['low-power', '--polling-interval-multiplier', '4'],
            obj=db,
        )

        assert result.exit_code == 0
        assert db.cfgdb.get_entry('POWER_PLAN_PROFILE', 'low-power').get('polling_interval_multiplier') == '4.0'

    def test_power_plan_feature_criticality_updates_feature_table(self):
        runner = CliRunner()
        db = Db()

        result = runner.invoke(
            config.config.commands['power-plan'].commands['feature-criticality'],
            ['telemetry', 'optional'],
            obj=db,
        )

        assert result.exit_code == 0
        assert db.cfgdb.get_entry('FEATURE', 'telemetry').get('criticality') == 'optional'

    def test_power_plan_sync_timeout_set_and_reset(self):
        runner = CliRunner()
        db = Db()

        set_result = runner.invoke(
            config.config.commands['power-plan'].commands['sync-timeout'],
            ['--runtime-ms', '90000', '--startup-ms', '300000'],
            obj=db,
        )

        assert set_result.exit_code == 0
        device_metadata = db.cfgdb.get_entry('DEVICE_METADATA', 'localhost')
        assert device_metadata.get('sai_sync_response_timeout_ms') == '90000'
        assert device_metadata.get('sai_sync_response_timeout_startup_ms') == '300000'

        reset_result = runner.invoke(
            config.config.commands['power-plan'].commands['sync-timeout'],
            ['--reset-runtime', '--reset-startup'],
            obj=db,
        )

        assert reset_result.exit_code == 0
        device_metadata = db.cfgdb.get_entry('DEVICE_METADATA', 'localhost')
        assert 'sai_sync_response_timeout_ms' not in device_metadata
        assert 'sai_sync_response_timeout_startup_ms' not in device_metadata
