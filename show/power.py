"""Power monitoring and power-plan inspection for SONiC."""

import json
import os
import subprocess

import click
from swsscommon.swsscommon import ConfigDBConnector
from tabulate import tabulate

try:
    import sonic_platform
except ImportError:
    sonic_platform = None


SWITCH_POWER_SETTINGS_FILE = "/usr/share/sonic/hwsku/switch_power_settings.json"
SWITCH_POWER_SETTINGS_FILE_ENV = "SWITCH_POWER_SETTINGS_FILE"
SWITCH_POWER_SETTINGS_FILE_FALLBACK = "/usr/share/sonic/device"
SAI_SYNC_RESPONSE_TIMEOUT_FIELD = "sai_sync_response_timeout_ms"
SAI_SYNC_RESPONSE_TIMEOUT_STARTUP_FIELD = "sai_sync_response_timeout_startup_ms"
MUST_SHOW_CONTAINERS = ("swss", "syncd")


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------

def _get_psu_rows():
    """
    Return a list of rows [name, status, voltage, current, power_w] using
    the same sonic_platform Chassis/PSU API that `psuutil` itself uses —
    this reads through the standard platform abstraction rather than
    shelling out to a CLI tool, so it works uniformly across vendors.
    """
    if sonic_platform is None:
        return None, "sonic_platform not available on this host"

    try:
        chassis = sonic_platform.platform.Platform().get_chassis()
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to load platform chassis: {exc}"

    rows = []
    try:
        for psu in chassis.get_all_psus():
            name = psu.get_name()
            status = "NOT PRESENT"
            voltage = "N/A"
            current = "N/A"
            power_w = "N/A"

            if psu.get_presence():
                try:
                    status = "OK" if psu.get_powergood_status() else "NOT OK"
                except NotImplementedError:
                    status = "UNKNOWN"

                try:
                    voltage = psu.get_voltage()
                except NotImplementedError:
                    pass

                try:
                    current = psu.get_current()
                except NotImplementedError:
                    pass

                try:
                    power_w = psu.get_power()
                except NotImplementedError:
                    pass

            rows.append([name, status, voltage, current, power_w])
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to read PSU data: {exc}"

    return rows, None


TURBOSTAT_SUMMARY_FIELDS = ("Busy%", "PkgWatt", "RAMWatt")


def _get_turbostat_summary(window_s=1):
    """
    Run turbostat for a short window and return the system-wide summary
    row (PkgWatt, RAMWatt, Busy%). Requires root (MSR access).
    """
    try:
        result = subprocess.run(
            ["turbostat", "--quiet", "--Summary", "--show", ",".join(TURBOSTAT_SUMMARY_FIELDS),
             "sleep", str(window_s)],
            capture_output=True, text=True, timeout=window_s + 10, check=True,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"turbostat unavailable: {exc}"

    # turbostat writes its counters to stderr when it runs a child command
    output = "".join(s for s in (result.stdout, getattr(result, "stderr", None)) if isinstance(s, str))

    # --show pins the column set/order, so an exact header match is unambiguous
    stats = _parse_turbostat_fixed_columns(output)
    if stats is not None:
        return stats, None

    # Fall back to scanning for the "-  -" summary row on platforms/turbostat
    # builds where --show isn't honored or the column set still varies.
    return _parse_turbostat_generic(output)


def _parse_turbostat_fixed_columns(output):
    header_seen = False
    data_line = None
    for line in output.splitlines():
        cols = line.split()
        if cols == list(TURBOSTAT_SUMMARY_FIELDS):
            header_seen = True
            continue
        if header_seen and len(cols) == len(TURBOSTAT_SUMMARY_FIELDS):
            data_line = cols  # keep the last sample (after the sleep window)

    if not data_line:
        return None

    row = dict(zip(TURBOSTAT_SUMMARY_FIELDS, data_line))
    try:
        return {
            "busy_pct": float(row["Busy%"].rstrip("%")),
            "pkg_watt": float(row["PkgWatt"]),
            "ram_watt": float(row["RAMWatt"]),
        }
    except ValueError:
        return None


def _parse_turbostat_generic(output):
    header, data_line = None, None
    pending_data_cols = []
    for line in output.splitlines():
        cols = _normalize_turbostat_columns(line.split())
        if not cols:
            continue

        # If we found a header but are waiting for complete data, check if this is a continuation
        if header is not None and data_line is None and pending_data_cols:
            # Try appending these columns to see if we get a match
            combined_cols = pending_data_cols + cols
            if len(combined_cols) == len(header):
                data_line = combined_cols
                pending_data_cols = []
                continue

        # If we already found a data line, we're done
        if data_line is not None:
            continue

        # Check if this is a header line
        if header is None and _is_turbostat_summary_header(cols):
            header = cols
            continue

        # If we have a header but no data yet, this could be the start of data
        if header is not None and data_line is None:
            if len(cols) == len(header):
                data_line = cols
            else:
                # Might be incomplete data that needs continuation
                pending_data_cols = cols

    if not header or not data_line:
        return None, "could not parse turbostat output (unexpected column layout)"

    row = dict(zip(header, data_line))
    try:
        return {
            "pkg_watt": _parse_turbostat_float(row, ("PkgWatt", "Pkg_Watt")),
            "ram_watt": _parse_turbostat_float(row, ("RAMWatt", "RAM_Watt")),
            "busy_pct": _parse_turbostat_float(row, ("Busy%", "Busy")),
        }, None
    except (KeyError, ValueError) as exc:
        return None, f"unexpected turbostat fields: {exc}"


def _normalize_turbostat_columns(columns):
    normalized = []
    index = 0
    while index < len(columns):
        column = columns[index]
        if column == "Busy" and index + 1 < len(columns) and columns[index + 1] == "%":
            normalized.append("Busy%")
            index += 2
            continue
        normalized.append(column)
        index += 1
    return normalized


def _is_turbostat_summary_header(columns):
    return "PkgWatt" in columns and "RAMWatt" in columns and ("Busy%" in columns or "Busy" in columns)


def _parse_turbostat_float(row, keys):
    for key in keys:
        if key in row:
            return float(row[key].rstrip("%"))
    raise KeyError(keys[0])


def _get_container_states():
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception:  # noqa: BLE001
        return {}

    states = {}
    for line in result.stdout.strip().splitlines():
        if "|" in line:
            name, state = line.split("|", 1)
            states[name.strip()] = state.strip()
    return states


def _matches_container(name, prefix):
    # Multi-ASIC platforms name them swss0, syncd1, ...
    return name == prefix or (name.startswith(prefix) and name[len(prefix):].isdigit())


def _get_all_container_names():
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception:  # noqa: BLE001
        return []
    return [name.strip() for name in result.stdout.splitlines() if name.strip()]


def _inspect_cpu_limit(container):
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.HostConfig.CpuPeriod}}|{{.HostConfig.CpuQuota}}", container],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception:  # noqa: BLE001
        return None, None
    parts = result.stdout.strip().split("|")
    return (parts[0], parts[1]) if len(parts) == 2 else (None, None)


def _get_docker_cpu_quota_rows(config_db):
    """Live --cpu-period/--cpu-quota state for every FEATURE with a criticality tier."""
    tiered = {name: cfg.get("criticality") for name, cfg in config_db.get_table("FEATURE").items()
              if cfg.get("criticality")}
    if not tiered:
        return []

    containers = _get_all_container_names()
    cpu_count = os.cpu_count() or 1
    rows = []
    for feature, tier in sorted(tiered.items()):
        targets = [c for c in containers if _matches_container(c, feature)]
        if not targets:
            rows.append([feature, tier, "-", "not found"])
            continue
        for container in targets:
            period, quota = _inspect_cpu_limit(container)
            if quota is None:
                rows.append([container, tier, "-", "unknown"])
            elif quota in ("-1", "0", ""):
                rows.append([container, tier, "-", "unlimited"])
            else:
                try:
                    period_i = int(period) if period not in ("0", "") else 100000
                    quota_i = int(quota)
                    pct = quota_i * 100 / (period_i * cpu_count)
                    rows.append([container, tier, f"{quota_i}/{period_i}", f"{pct:.0f}%"])
                except (TypeError, ValueError):
                    rows.append([container, tier, "-", "unknown"])
    return rows


def _get_container_cpu_rows(top_n=8):
    """Return top-N containers by CPU%, plus swss/syncd regardless of rank."""
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--all", "--format", "{{.Name}}|{{.CPUPerc}}"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"docker stats unavailable: {exc}"

    states = _get_container_states()

    rows = []
    for line in result.stdout.strip().splitlines():
        if "|" not in line:
            continue
        name, cpu_str = line.split("|", 1)
        name = name.strip()
        cpu_pct = cpu_str.strip().rstrip("%")
        try:
            cpu = float(cpu_pct)
        except ValueError:
            cpu = 0.0
        rows.append([name, cpu, states.get(name, "unknown")])

    rows.sort(key=lambda r: r[1], reverse=True)
    selected = [r for r in rows if r[2] == "running"][:top_n]
    selected_names = {r[0] for r in selected}
    selected += [r for r in rows if r[0] not in selected_names
                 and any(_matches_container(r[0], p) for p in MUST_SHOW_CONTAINERS)]

    for prefix in MUST_SHOW_CONTAINERS:
        if not any(_matches_container(r[0], prefix) for r in rows):
            selected.append([prefix, 0.0, "not found"])

    return selected, None


def _connect_config_db():
    config_db = ConfigDBConnector()
    config_db.connect()
    return config_db


def _get_current_power_plan(config_db):
    device_metadata = config_db.get_table("DEVICE_METADATA")
    localhost = device_metadata.get("localhost", {})
    return localhost.get("power_plan", "(not set)")


def _get_switch_power_settings_path():
    # First check environment variable
    env_path = os.environ.get(SWITCH_POWER_SETTINGS_FILE_ENV)
    if env_path:
        return env_path
    
    # Check standard hwsku location
    if os.path.exists(SWITCH_POWER_SETTINGS_FILE):
        return SWITCH_POWER_SETTINGS_FILE
    
    # HACKATHON: Fallback to device directory using PLATFORM and HWSKU
    # Try to get from environment (already set if /etc/sonic/sonic-environment was sourced)
    platform = os.environ.get("PLATFORM")
    hwsku = os.environ.get("HWSKU")
    
    # If not in environment, try to read from /etc/sonic/sonic-environment file
    if not platform or not hwsku:
        sonic_env_file = "/etc/sonic/sonic-environment"
        if os.path.exists(sonic_env_file):
            try:
                with open(sonic_env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("PLATFORM="):
                            platform = line.split("=", 1)[1]
                        elif line.startswith("HWSKU="):
                            hwsku = line.split("=", 1)[1]
            except Exception:  # noqa: BLE001
                pass
    
    # If still not found, query from CONFIG_DB using sonic-cfggen
    if not platform or not hwsku:
        try:
            # Query DEVICE_METADATA.localhost to get platform and hwsku
            result = subprocess.run(
                ["sonic-cfggen", "-H", "-v", "DEVICE_METADATA.localhost"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout:
                import ast
                metadata = ast.literal_eval(result.stdout.strip())
                if not platform:
                    platform = metadata.get("platform")
                if not hwsku:
                    hwsku = metadata.get("hwsku")
        except Exception:  # noqa: BLE001
            pass
    
    if platform and hwsku:
        device_path = f"{SWITCH_POWER_SETTINGS_FILE_FALLBACK}/{platform}/{hwsku}/switch_power_settings.json"
        if os.path.exists(device_path):
            return device_path
    
    # Return default path (even if it doesn't exist - caller will handle FileNotFoundError)
    return SWITCH_POWER_SETTINGS_FILE


def _load_resolved_engine_payload(plan_name):
    if not plan_name or plan_name == "(not set)":
        return None, "power plan is not set"

    settings_path = _get_switch_power_settings_path()
    try:
        with open(settings_path, encoding="utf-8") as settings_file:
            root = json.load(settings_file)
    except FileNotFoundError:
        return None, f"switch power settings file not found: {settings_path}"
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to read switch power settings file: {exc}"

    if isinstance(root.get("power_plans"), dict):
        plan_node = root["power_plans"].get(plan_name)
    else:
        plan_node = root.get(plan_name)

    if not isinstance(plan_node, dict):
        return None, f"power plan '{plan_name}' not present in {settings_path}"

    if isinstance(plan_node.get("attributes"), list):
        return plan_node, None

    attributes = []
    try:
        for engine_name, engine_value in sorted(plan_node.items()):
            if isinstance(engine_value, dict) and "sai_metadata" in engine_value and "value" in engine_value:
                engine_json = engine_value
            else:
                engine_json = {
                    "sai_metadata": {
                        "sai_attr_value_type": "SAI_ATTR_VALUE_TYPE_BOOL",
                    },
                    "value": _normalize_engine_bool(engine_name, engine_value),
                }
            attributes.append({engine_name: engine_json})
    except ValueError as exc:
        return None, str(exc)

    return {"attributes": attributes}, None


def _normalize_engine_bool(engine_name, engine_value):
    if isinstance(engine_value, bool):
        return "true" if engine_value else "false"
    if isinstance(engine_value, str):
        normalized = engine_value.strip().lower()
        if normalized in ("enabled", "true"):
            return "true"
        if normalized in ("disabled", "false"):
            return "false"
    raise ValueError(f"invalid value for engine '{engine_name}': {engine_value}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group(cls=click.core.Group, name="power")
def power():
    """Show power consumption across PSU, CPU, and container domains."""
    pass


@power.command(name="status")
@click.option("--turbostat-window", default=1, show_default=True,
              help="Seconds turbostat measures for the CPU package power sample")
@click.option("--top", default=8, show_default=True,
              help="Number of top CPU-consuming containers to show")
def status(turbostat_window, top):
    """Show current PSU wattage, CPU package power, and top container CPU users."""

    # --- PSU ---
    click.echo("PSU status:")
    psu_rows, psu_err = _get_psu_rows()
    if psu_err:
        click.echo(f"  (unavailable: {psu_err})")
    else:
        click.echo(tabulate(
            psu_rows,
            headers=["PSU", "Status", "Voltage (V)", "Current (A)", "Power (W)"],
            tablefmt="simple",
            floatfmt=".2f",
        ))

    click.echo("")

    # --- CPU package power (turbostat / RAPL) ---
    click.echo("CPU package power (turbostat):")
    ts_stats, ts_err = _get_turbostat_summary(turbostat_window)
    if ts_err:
        click.echo(f"  (unavailable: {ts_err})")
    else:
        click.echo(tabulate(
            [[ts_stats["pkg_watt"], ts_stats["ram_watt"], ts_stats["busy_pct"]]],
            headers=["PkgWatt", "RAMWatt", "Busy %"],
            tablefmt="simple",
            floatfmt=".2f",
        ))

    click.echo("")

    # --- Top container CPU consumers ---
    click.echo(f"Top {top} containers by CPU usage:")
    container_rows, container_err = _get_container_cpu_rows(top_n=top)
    if container_err:
        click.echo(f"  (unavailable: {container_err})")
    elif not container_rows:
        click.echo("  (no running containers found)")
    else:
        click.echo(tabulate(
            container_rows,
            headers=["Container", "CPU %", "State"],
            tablefmt="simple",
            floatfmt=".2f",
        ))


@power.command(name="plan")
def plan():
    """
    Show the currently configured power plan.

    Show the currently configured power plan and profile metadata.
    """
    click.echo("[HACKATHON] Displaying power plan configuration...")
    try:
        config_db = _connect_config_db()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Could not connect to CONFIG_DB: {exc}")
        return

    current_plan = _get_current_power_plan(config_db)
    click.echo("[HACKATHON] Current power plan: {}".format(current_plan))
    localhost_metadata = config_db.get_table("DEVICE_METADATA").get("localhost", {})
    runtime_timeout_ms = localhost_metadata.get(SAI_SYNC_RESPONSE_TIMEOUT_FIELD, "(default)")
    startup_timeout_ms = localhost_metadata.get(SAI_SYNC_RESPONSE_TIMEOUT_STARTUP_FIELD, "(platform default)")

    click.echo(f"Current power plan: {current_plan}")
    click.echo("SAI Redis sync timeout overrides:")
    click.echo(tabulate(
        [["runtime", runtime_timeout_ms], ["startup", startup_timeout_ms]],
        headers=["Phase", "Timeout (ms)"],
        tablefmt="simple",
    ))

    profile_table = config_db.get_table("POWER_PLAN_PROFILE")
    if not profile_table:
        click.echo("No POWER_PLAN_PROFILE entries configured yet.")
    else:
        rows = []
        for plan_name, fields in profile_table.items():
            rows.append([
                plan_name,
                fields.get("asic_engines_disabled", "-"),
                fields.get("polling_interval_multiplier", "-"),
                fields.get("cpu_cap_critical_pct", "-"),
                fields.get("cpu_cap_important_pct", "-"),
                fields.get("cpu_cap_optional_pct", "-"),
            ])
        click.echo("")
        click.echo(tabulate(
            rows,
            headers=[
                "Plan",
                "ASIC engines disabled",
                "Polling interval multiplier",
                "Critical CPU %",
                "Important CPU %",
                "Optional CPU %",
            ],
            tablefmt="simple",
        ))

    click.echo("")
    click.echo("Docker CPU quota (live, from hostcfgd):")
    quota_rows = _get_docker_cpu_quota_rows(config_db)
    if not quota_rows:
        click.echo("  No FEATURE entries have a criticality tier assigned yet.")
    else:
        click.echo(tabulate(
            quota_rows,
            headers=["Container", "Tier", "Quota/Period (us)", "Effective CPU %"],
            tablefmt="simple",
        ))


@power.command(name="timeout")
def timeout():
    """Show SAI Redis sync response timeout settings."""
    click.echo("[HACKATHON] Displaying SAI sync timeout configuration...")
    try:
        config_db = _connect_config_db()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Could not connect to CONFIG_DB: {exc}")
        return

    localhost_metadata = config_db.get_table("DEVICE_METADATA").get("localhost", {})
    runtime_timeout_ms = localhost_metadata.get(SAI_SYNC_RESPONSE_TIMEOUT_FIELD, "(default: 60000)")
    startup_timeout_ms = localhost_metadata.get(SAI_SYNC_RESPONSE_TIMEOUT_STARTUP_FIELD, "(platform startup default)")

    click.echo("SAI Redis sync response timeout:")
    click.echo(tabulate(
        [["runtime", runtime_timeout_ms], ["startup", startup_timeout_ms]],
        headers=["Phase", "Timeout (ms)"],
        tablefmt="simple",
    ))
    click.echo("Valid override range: 1000..1800000 ms")


@power.command(name="engines")
def engines():
    """Show the resolved custom engine payload for the active power plan."""
    click.echo("[HACKATHON] Displaying power plan engine configuration...")
    try:
        config_db = _connect_config_db()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Could not connect to CONFIG_DB: {exc}")
        return

    current_plan = _get_current_power_plan(config_db)
    click.echo("[HACKATHON] Current power plan: {}".format(current_plan))
    payload, error = _load_resolved_engine_payload(current_plan)

    click.echo(f"Current power plan: {current_plan}")
    click.echo(f"Settings file: {_get_switch_power_settings_path()}")

    if error:
        click.echo(f"Resolved payload unavailable: {error}")
        return

    # Display engine configuration in tabular format
    if payload and "attributes" in payload:
        rows = []
        for attr in payload["attributes"]:
            for engine_name, engine_config in attr.items():
                sai_type = engine_config.get("sai_metadata", {}).get("sai_attr_value_type", "N/A")
                value = engine_config.get("value", "N/A")
                rows.append([engine_name, sai_type, value])
        
        click.echo("")
        click.echo(tabulate(
            rows,
            headers=["Engine Name", "SAI Type", "Value"],
            tablefmt="simple",
        ))
    else:
        click.echo("")
        click.echo("No engine configuration available")
