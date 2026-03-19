#!/usr/bin/env python3
# Script per monitorare vSphere Replication globale
# Controlla allarmi ed eventi su TUTTO l'ambiente: VM, ESXi, datastore, etc.
# Controlla servizi VRA (hms, hbrsrv) via VAMI API se --vra-host specificato
# CRITICAL solo se la replica FALLISCE (error, fault, stopped, broken)
# RPO violated/exceeded -> WARNING (la replica funziona, e' solo in ritardo)
# Compatibile con Icinga Agent / NetEye
#
# Richiede: pyVmomi (pip3 install pyvmomi)
#
# Uso:
#   ./check_vsphere_replication.py -H <vcenter> -u <user> -p <password>
#   ./check_vsphere_replication.py -H <vcenter> -u <user> -p <password> \
#     --vra-host 10.22.136.59 --vra-user admin --vra-password <pass>

import sys
import argparse
import ssl
import json
import socket
import urllib.request
import urllib.error
from datetime import datetime, timedelta

socket.setdefaulttimeout(20)

try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
except ImportError:
    print("CRITICAL! pyVmomi non installato. Eseguire: pip3 install pyvmomi")
    sys.exit(2)

# Keywords che indicano messaggi di recovery/risoluzione (da ignorare)
RECOVERY_KEYWORDS = ["no longer violated", "resolved", "restored", "recovered", "completed successfully"]

# Keywords che indicano fallimento REALE della replica (CRITICAL)
FAILURE_KEYWORDS = ["failed", "error", "fault", "stopped", "broken"]

# Servizi VRA critici per la replica
CRITICAL_SERVICES = {"hms", "hbrsrv"}


# ── VAMI API (porta 5480) ──────────────────────────────────────────

def check_vra_vami(host, port, user, password, timeout):
    """Check servizi VRA via VAMI API. Ritorna (critical_list, perf_dict)."""
    problems = []
    perf = {}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # Login
    try:
        url = f"https://{host}:{port}/configure/requestHandlers/login"
        data = json.dumps({"username": user, "password": password}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
        result = json.loads(resp.read().decode())
        if not result.get("successful"):
            return [f"VRA {host}: login failed"], {}
        sid = result["data"]["sessionId"].strip('"')
    except Exception as e:
        return [f"VRA {host}: unreachable - {e}"], {}

    def api(endpoint):
        url = f"https://{host}:{port}/configure/requestHandlers/{endpoint}"
        req = urllib.request.Request(url, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("dr.config.service.sessionid", sid)
        resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
        return json.loads(resp.read().decode())

    # Check servizi critici
    try:
        svc = api("getAllServicesStatus")
        if svc.get("successful"):
            for s in svc["data"]:
                if s["serviceId"] in CRITICAL_SERVICES:
                    perf[f"svc_{s['serviceId']}"] = 1 if s["isRunning"] else 0
                    if not s["isRunning"]:
                        problems.append(f"VRA {host}: {s['serviceId']} STOPPED")
    except Exception:
        pass

    # Check connessione vCenter
    try:
        info = api("getSummaryInfo")
        if info.get("successful"):
            trusted = info["data"].get("drConfiguration", {}).get("trustedConnection", 0)
            perf["trusted_connection"] = trusted
            if trusted != 1:
                problems.append(f"VRA {host}: vCenter not trusted")
    except Exception:
        pass

    return problems, perf


# ── pyVmomi (vCenter) ──────────────────────────────────────────────

def check_all_alarms(content):
    """Controlla allarmi attivi nel vCenter legati a replication."""
    critical_errors = []
    warnings = []
    repl_keywords = ["replication", "hbr", "rpo", "replicated", "replicat"]

    root = content.rootFolder

    def scan_entity_alarms(entity, entity_name):
        try:
            if not hasattr(entity, 'triggeredAlarmState') or not entity.triggeredAlarmState:
                return
            for alarm_state in entity.triggeredAlarmState:
                try:
                    if alarm_state.overallStatus not in ("red", "yellow"):
                        continue
                    alarm_name = ""
                    try:
                        alarm_name = alarm_state.alarm.info.name
                    except Exception:
                        pass
                    alarm_lower = alarm_name.lower()
                    if any(kw in alarm_lower for kw in repl_keywords):
                        if any(kw in alarm_lower for kw in RECOVERY_KEYWORDS):
                            continue
                        status = str(alarm_state.overallStatus)
                        entry = f"{entity_name}: {alarm_name} [{status}]"
                        if any(kw in alarm_lower for kw in ["rpo", "violated", "exceeded", "lag"]):
                            warnings.append(entry)
                        elif any(kw in alarm_lower for kw in FAILURE_KEYWORDS):
                            critical_errors.append(entry)
                        else:
                            warnings.append(entry)
                except Exception:
                    continue
        except Exception:
            pass

    scan_entity_alarms(root, "RootFolder")

    dc_view = content.viewManager.CreateContainerView(root, [vim.Datacenter], True)
    for dc in dc_view.view:
        scan_entity_alarms(dc, f"DC:{dc.name}")
    dc_view.Destroy()

    vm_view = content.viewManager.CreateContainerView(root, [vim.VirtualMachine], True)
    for vm_obj in vm_view.view:
        scan_entity_alarms(vm_obj, f"VM:{vm_obj.name}")
    vm_view.Destroy()

    return critical_errors, warnings


def check_global_events(content, hours=24):
    """Controlla eventi globali di errore replication nelle ultime N ore."""
    errors = []
    warnings = []
    seen = set()

    event_manager = content.eventManager
    time_filter = vim.event.EventFilterSpec.ByTime()
    time_filter.beginTime = datetime.now() - timedelta(hours=hours)
    time_filter.endTime = datetime.now()

    filter_spec = vim.event.EventFilterSpec()
    filter_spec.time = time_filter

    hbr_critical_types = [
        "HbrReplicationVmErrorEvent",
        "HbrReplicationVmFaultEvent",
        "HbrHostErrorEvent",
        "HbrHostFaultEvent",
        "HbrStorageErrorEvent",
        "HbrStorageFaultEvent",
        "HbrReplicationErrorEvent",
        "HbrFailoverEvent",
        "com.vmware.vcHbr.hbrReplicationVmErrorEvent",
        "com.vmware.vcHbr.hbrReplicationVmFaultEvent",
        "com.vmware.vcHbr.hbrHostErrorEvent",
        "com.vmware.vcHbr.hbrStorageErrorEvent",
    ]
    hbr_warning_types = [
        "HbrVmRpoExceededEvent",
        "com.vmware.vcHbr.hbrVmRpoExceededEvent",
    ]

    filter_spec.eventTypeId = hbr_critical_types + hbr_warning_types

    def get_entity_name(event):
        if hasattr(event, 'vm') and event.vm:
            return event.vm.name
        if hasattr(event, 'host') and event.host:
            return event.host.name
        if hasattr(event, 'ds') and event.ds:
            return event.ds.name
        return ""

    try:
        collector = event_manager.CreateCollectorForEvents(filter_spec)
        collector.SetCollectorPageSize(100)
        all_events = []
        while True:
            page = collector.ReadNextEvents(100)
            if not page:
                break
            all_events.extend(page)
            if len(all_events) > 1000:
                break
        collector.DestroyCollector()

        for event in all_events:
            msg = getattr(event, 'fullFormattedMessage', '') or str(event)
            msg_lower = msg.lower()

            if any(kw in msg_lower for kw in RECOVERY_KEYWORDS):
                continue

            entity_name = get_entity_name(event)
            key = f"{entity_name}:{type(event).__name__}"
            if key not in seen:
                seen.add(key)
                entry = f"{entity_name}: {msg[:100]}" if entity_name else msg[:100]
                event_type = type(event).__name__
                if event_type in ("HbrVmRpoExceededEvent",) or "rpo" in msg_lower:
                    warnings.append(entry)
                else:
                    errors.append(entry)
    except Exception:
        pass

    return errors, warnings


def count_replicated_vms(content):
    """Conta le VM con vSphere Replication usando PropertyCollector (veloce)."""
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True)

    traversal = vim.PropertyCollector.TraversalSpec(
        name="traverseEntities", path="view", skip=False,
        type=vim.view.ContainerView)
    obj_spec = vim.PropertyCollector.ObjectSpec(
        obj=container, selectSet=[traversal], skip=True)
    prop_spec = vim.PropertyCollector.PropertySpec(
        type=vim.VirtualMachine, all=False,
        pathSet=["config.extraConfig"])
    filter_spec = vim.PropertyCollector.FilterSpec(
        objectSet=[obj_spec], propSet=[prop_spec])

    count = 0
    try:
        props = content.propertyCollector.RetrieveContents([filter_spec])
        for obj in props:
            for prop in obj.propSet:
                if prop.name == "config.extraConfig" and prop.val:
                    for opt in prop.val:
                        if opt.key.startswith("hbr_filter."):
                            count += 1
                            break
    except Exception:
        count = -1
    finally:
        container.Destroy()

    return count


# ── Main ───────────────────────────────────────────────────────────

def check_replication(args):
    all_critical = []
    all_warnings = []
    vra_perf = {}

    # Check VRA via VAMI (opzionale)
    if args.vra_host and args.vra_password:
        vra_problems, vra_perf = check_vra_vami(
            args.vra_host, args.vra_port,
            args.vra_user, args.vra_password,
            args.timeout
        )
        all_critical.extend(vra_problems)

    # Check vCenter via pyVmomi
    context = ssl._create_unverified_context()
    try:
        si = SmartConnect(
            host=args.host,
            user=args.user,
            pwd=args.password,
            port=int(args.port),
            sslContext=context
        )
    except Exception as e:
        print(f"CRITICAL! Cannot connect to vCenter {args.host} - {e} "
              f"| replicated_vms=0 replication_errors=0 replication_rpo_violations=0")
        sys.exit(2)

    try:
        content = si.RetrieveContent()

        replicated_count = count_replicated_vms(content)
        alarm_critical, alarm_warnings = check_all_alarms(content)
        event_errors, event_warnings = check_global_events(content, hours=args.hours)

    except Exception as e:
        print(f"CRITICAL! Error querying vCenter - {e} "
              f"| replicated_vms=0 replication_errors=0 replication_rpo_violations=0")
        sys.exit(2)
    finally:
        Disconnect(si)

    all_critical.extend(alarm_critical)
    all_critical.extend(event_errors)
    all_warnings.extend(alarm_warnings)
    all_warnings.extend(event_warnings)

    # Perfdata
    perfdata = (f"replicated_vms={replicated_count} "
                f"replication_errors={len(all_critical)} "
                f"replication_rpo_violations={len(all_warnings)}")
    for k, v in vra_perf.items():
        if isinstance(v, (int, float)):
            perfdata += f" {k}={v}"

    # Output
    if all_critical:
        error_list = ", ".join(all_critical[:5])
        extra = f" (+{len(all_critical)-5} more)" if len(all_critical) > 5 else ""
        print(f"CRITICAL! {len(all_critical)} failure(s): {error_list}{extra} | {perfdata}")
        sys.exit(2)
    elif all_warnings:
        warn_list = ", ".join(all_warnings[:5])
        extra = f" (+{len(all_warnings)-5} more)" if len(all_warnings) > 5 else ""
        print(f"WARNING! {len(all_warnings)} warning(s): {warn_list}{extra} | {perfdata}")
        sys.exit(1)
    else:
        print(f"OK! vSphere Replication healthy. {replicated_count} VM(s) replicated, "
              f"no errors in last {args.hours}h | {perfdata}")
        sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check vSphere Replication status")
    parser.add_argument("-H", "--host", required=True, help="vCenter Server address")
    parser.add_argument("-u", "--user", required=True, help="Username")
    parser.add_argument("-p", "--password", required=True, help="Password")
    parser.add_argument("--port", default="443", help="vCenter port (default: 443)")
    parser.add_argument("--hours", type=int, default=24,
                        help="Check events in last N hours (default: 24)")
    # VRA VAMI (opzionale)
    parser.add_argument("--vra-host", help="VRA IP per check servizi (porta 5480)")
    parser.add_argument("--vra-port", default=5480, type=int,
                        help="VRA VAMI port (default: 5480)")
    parser.add_argument("--vra-user", default="admin",
                        help="VRA VAMI username (default: admin)")
    parser.add_argument("--vra-password", help="VRA VAMI password")
    parser.add_argument("-t", "--timeout", default=20, type=int,
                        help="Timeout in secondi (default: 20)")
    args = parser.parse_args()

    check_replication(args)
