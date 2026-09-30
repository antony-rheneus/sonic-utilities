import click

from utilities_common.cli import AbbreviationGroup, pass_db


POWER_PLAN_PROFILE_TABLE = 'POWER_PLAN_PROFILE'
DEVICE_METADATA_TABLE = 'DEVICE_METADATA'
VALID_TIERS = ('critical', 'important', 'optional')
SAI_SYNC_RESPONSE_TIMEOUT_FIELD = 'sai_sync_response_timeout_ms'
SAI_SYNC_RESPONSE_TIMEOUT_STARTUP_FIELD = 'sai_sync_response_timeout_startup_ms'
SAI_SYNC_RESPONSE_TIMEOUT_MIN_MS = 1000
SAI_SYNC_RESPONSE_TIMEOUT_MAX_MS = 1800000
POLLING_INTERVAL_MULTIPLIER_MIN = 0.1
POLLING_INTERVAL_MULTIPLIER_MAX = 10.0


def _validate_polling_interval_multiplier(ctx, param, value):
    # Must match sonic-power-plan.yang decimal64 fraction-digits 1
    if value is not None and round(value, 1) != value:
        raise click.BadParameter('must have at most one decimal place')
    return value


@click.group(cls=AbbreviationGroup, name='power-plan')
def power_plan():
    """Configure SONiC power plans."""
    pass


@power_plan.command('set', short_help='Activate or define a power plan')
@click.argument('plan_name', metavar='<plan-name>', required=True)
@click.option('--polling-interval-multiplier',
              type=click.FloatRange(POLLING_INTERVAL_MULTIPLIER_MIN, POLLING_INTERVAL_MULTIPLIER_MAX),
              default=None, callback=_validate_polling_interval_multiplier,
              help='Scale factor applied to flex-counter polling intervals (0.1-10.0, one decimal).')
@click.option('--cpu-cap-critical-pct', type=click.IntRange(1, 100), default=None,
              help='CPU cap percentage for critical features. Omit to leave unchanged.')
@click.option('--cpu-cap-important-pct', type=click.IntRange(1, 100), default=None,
              help='CPU cap percentage for important features. Omit to leave unchanged.')
@click.option('--cpu-cap-optional-pct', type=click.IntRange(1, 100), default=None,
              help='CPU cap percentage for optional features. Omit to leave unchanged.')
@click.option('--asic-engines-disabled', default=None,
              help='Comma-separated ASIC engine names disabled by this plan.')
@pass_db
def power_plan_set(db, plan_name, polling_interval_multiplier,
                   cpu_cap_critical_pct, cpu_cap_important_pct,
                   cpu_cap_optional_pct, asic_engines_disabled):
    """Activate a plan and upsert its POWER_PLAN_PROFILE entry."""
    click.echo("[HACKATHON] Setting power plan: {}".format(plan_name))

    config_db = db.cfgdb
    device_metadata = config_db.get_table(DEVICE_METADATA_TABLE).get('localhost', {})
    profile_entry = config_db.get_table(POWER_PLAN_PROFILE_TABLE).get(plan_name, {})

    if polling_interval_multiplier is not None:
        profile_entry['polling_interval_multiplier'] = '{:.1f}'.format(polling_interval_multiplier)
        click.echo("[HACKATHON]   Polling interval multiplier: {}".format(polling_interval_multiplier))
    elif not profile_entry.get('polling_interval_multiplier'):
        profile_entry['polling_interval_multiplier'] = '1.0'

    tier_caps = {
        'critical': cpu_cap_critical_pct,
        'important': cpu_cap_important_pct,
        'optional': cpu_cap_optional_pct,
    }
    for tier, cap in tier_caps.items():
        field_name = f'cpu_cap_{tier}_pct'
        if cap is not None:
            profile_entry[field_name] = str(cap)

    if asic_engines_disabled is not None:
        profile_entry['asic_engines_disabled'] = _normalize_csv(asic_engines_disabled)
    elif 'asic_engines_disabled' not in profile_entry:
        profile_entry['asic_engines_disabled'] = ''

    config_db.set_entry(POWER_PLAN_PROFILE_TABLE, plan_name, profile_entry)

    device_metadata['power_plan'] = plan_name
    config_db.set_entry(DEVICE_METADATA_TABLE, 'localhost', device_metadata)

    click.echo("[HACKATHON] Power plan '{}' activated in CONFIG_DB".format(plan_name))
    click.echo("Configured power plan '{}' in CONFIG_DB".format(plan_name))


@power_plan.command('feature-criticality', short_help='Assign a feature criticality tier')
@click.argument('feature_name', metavar='<feature-name>', required=True)
@click.argument('tier', metavar='<critical|important|optional>',
                type=click.Choice(VALID_TIERS))
@pass_db
def power_plan_feature_criticality(db, feature_name, tier):
    """Tag a feature so hostcfgd can apply plan-based CPU caps."""
    click.echo("[HACKATHON] Setting feature criticality: {} = {}".format(feature_name, tier))

    feature_entry = db.cfgdb.get_table('FEATURE').get(feature_name)
    if not feature_entry:
        raise click.ClickException("Feature '{}' does not exist".format(feature_name))

    feature_entry['criticality'] = tier
    db.cfgdb.set_entry('FEATURE', feature_name, feature_entry)
    click.echo("[HACKATHON] Feature '{}' criticality updated to '{}'".format(feature_name, tier))
    click.echo("Updated feature '{}' criticality to '{}'".format(feature_name, tier))


@power_plan.command('sync-timeout', short_help='Configure sairedis sync response timeout')
@click.option(
    '--runtime-ms',
    type=click.IntRange(SAI_SYNC_RESPONSE_TIMEOUT_MIN_MS, SAI_SYNC_RESPONSE_TIMEOUT_MAX_MS),
    default=None,
    help='Runtime SAI sync response timeout in milliseconds.',
)
@click.option(
    '--startup-ms',
    type=click.IntRange(SAI_SYNC_RESPONSE_TIMEOUT_MIN_MS, SAI_SYNC_RESPONSE_TIMEOUT_MAX_MS),
    default=None,
    help='Startup SAI sync response timeout in milliseconds for extended init path.',
)
@click.option(
    '--reset-runtime',
    is_flag=True,
    default=False,
    help='Remove runtime timeout override and fall back to SWSS default.',
)
@click.option(
    '--reset-startup',
    is_flag=True,
    default=False,
    help='Remove startup timeout override and fall back to platform startup default.',
)
@pass_db
def power_plan_sync_timeout(db, runtime_ms, startup_ms, reset_runtime, reset_startup):
    """Set or reset SAI redis sync timeout fields in DEVICE_METADATA|localhost."""

    if runtime_ms is None and startup_ms is None and not reset_runtime and not reset_startup:
        raise click.ClickException('Specify at least one option: --runtime-ms/--startup-ms/--reset-runtime/--reset-startup')

    if runtime_ms is not None and reset_runtime:
        raise click.ClickException('Cannot use --runtime-ms and --reset-runtime together')

    if startup_ms is not None and reset_startup:
        raise click.ClickException('Cannot use --startup-ms and --reset-startup together')

    config_db = db.cfgdb
    device_metadata = config_db.get_table(DEVICE_METADATA_TABLE).get('localhost', {})

    if runtime_ms is not None:
        device_metadata[SAI_SYNC_RESPONSE_TIMEOUT_FIELD] = str(runtime_ms)
    elif reset_runtime:
        device_metadata.pop(SAI_SYNC_RESPONSE_TIMEOUT_FIELD, None)

    if startup_ms is not None:
        device_metadata[SAI_SYNC_RESPONSE_TIMEOUT_STARTUP_FIELD] = str(startup_ms)
    elif reset_startup:
        device_metadata.pop(SAI_SYNC_RESPONSE_TIMEOUT_STARTUP_FIELD, None)

    config_db.set_entry(DEVICE_METADATA_TABLE, 'localhost', device_metadata)
    click.echo('Updated DEVICE_METADATA|localhost SAI sync response timeout fields')


def _normalize_csv(raw_value):
    items = [item.strip() for item in raw_value.split(',') if item.strip()]
    return ','.join(items)
