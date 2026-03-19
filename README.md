# check_vsphere_replication.py

**Icinga / NetEye monitoring plugin** per il controllo completo dello stato di **vSphere Replication** tramite vCenter API (pyVmomi) e, opzionalmente, della **vSphere Replication Appliance (VRA)** tramite VAMI API.

---

## Panoramica

Lo script esegue un check completo dell'ambiente vSphere Replication:

| Componente | Cosa controlla | Come |
|---|---|---|
| **vCenter** | Allarmi attivi legati a replication su VM, ESXi, Datacenter, Datastore | pyVmomi `triggeredAlarmState` |
| **vCenter** | Eventi di errore HBR nelle ultime N ore | pyVmomi `EventManager` |
| **vCenter** | Conteggio VM replicate | pyVmomi `PropertyCollector` (`hbr_filter.*`) |
| **VRA** _(opzionale)_ | Servizi critici `hms` e `hbrsrv` | VAMI API porta 5480 |
| **VRA** _(opzionale)_ | Connessione trusted al vCenter | VAMI API `getSummaryInfo` |

---

## Logica degli exit code

| Exit Code | Stato | Condizione |
|---|---|---|
| `0` | **OK** | Nessun errore, nessun warning. Replica sana. |
| `1` | **WARNING** | RPO violated/exceeded (la replica funziona ma e' in ritardo) |
| `2` | **CRITICAL** | Replica fallita (`error`, `fault`, `stopped`, `broken`), servizi VRA down, o connessione vCenter non trusted |

> **Nota importante:** RPO violated/exceeded genera WARNING, non CRITICAL. La replica sta funzionando, e' solo in ritardo rispetto all'obiettivo RPO configurato.

---

## Requisiti

- **Python 3.6+**
- **pyVmomi** (VMware vSphere API Python Bindings)

```bash
pip3 install pyvmomi
```

### Permessi richiesti

| Componente | Permesso |
|---|---|
| vCenter | Utente read-only con accesso all'inventario (almeno `System.View` e `System.Read`) |
| VRA VAMI | Credenziali di accesso alla VAMI (default user: `admin`, porta `5480`) |

---

## Porte di rete richieste

Per il corretto funzionamento dello script, le seguenti porte devono essere aperte tra il server di monitoraggio (Icinga/NetEye) e i target:

| Sorgente | Destinazione | Porta | Protocollo | Descrizione |
|---|---|---|---|---|
| Monitoring server | vCenter Server | **443/tcp** | HTTPS | vSphere API (pyVmomi / SOAP) |
| Monitoring server | VRA Appliance | **5480/tcp** | HTTPS | VAMI API (gestione appliance) |

### Porte aggiuntive dell'infrastruttura vSphere Replication

Queste porte non sono usate direttamente dallo script, ma devono essere aperte nell'infrastruttura per il corretto funzionamento della replica stessa:

| Sorgente | Destinazione | Porta | Protocollo | Descrizione |
|---|---|---|---|---|
| VRA | vCenter Server | **443/tcp** | HTTPS | Registrazione e comunicazione VRA-vCenter |
| VRA | ESXi hosts | **902/tcp** | TCP | Traffico NFC (Network File Copy) per i dati di replica |
| ESXi hosts (source) | VRA (target) | **31031/tcp** | TCP | Traffico dati di replica vSphere Replication |
| ESXi hosts (source) | VRA (target) | **44046/tcp** | TCP | Traffico dati di replica (canale aggiuntivo) |
| VRA (site A) | VRA (site B) | **80/tcp** | HTTP | Comunicazione inter-site VRA (lookup service) |
| VRA (site A) | VRA (site B) | **443/tcp** | HTTPS | Comunicazione inter-site VRA (pairing) |
| VRA | VRA | **5480/tcp** | HTTPS | VAMI management locale |
| Browser/Client | VRA | **5480/tcp** | HTTPS | Accesso web VAMI per amministrazione |

> **Nota:** Le porte 31031 e 44046 sono specifiche di vSphere Replication e gestiscono il trasferimento dati tra sito sorgente e sito target.

---

## Installazione

```bash
# Clona il repository
git clone https://github.com/GiulioSavini/check-vsphere-replication.git
cd check-vsphere-replication

# Installa le dipendenze
pip3 install pyvmomi

# Rendi eseguibile
chmod +x check_vsphere_replication.py

# (Opzionale) Copia nella directory dei plugin Icinga/NetEye
cp check_vsphere_replication.py /usr/lib/nagios/plugins/
```

---

## Sintassi e parametri

```
check_vsphere_replication.py -H <vcenter> -u <user> -p <password> [opzioni]
```

### Parametri obbligatori

| Parametro | Descrizione |
|---|---|
| `-H`, `--host` | Indirizzo IP o hostname del vCenter Server |
| `-u`, `--user` | Username per l'autenticazione al vCenter |
| `-p`, `--password` | Password per l'autenticazione al vCenter |

### Parametri opzionali - vCenter

| Parametro | Default | Descrizione |
|---|---|---|
| `--port` | `443` | Porta del vCenter Server |
| `--hours` | `24` | Finestra temporale in ore per la ricerca degli eventi |
| `-t`, `--timeout` | `20` | Timeout connessione in secondi |

### Parametri opzionali - VRA (VAMI API)

| Parametro | Default | Descrizione |
|---|---|---|
| `--vra-host` | _(disabilitato)_ | IP/hostname del VRA da controllare |
| `--vra-port` | `5480` | Porta VAMI del VRA |
| `--vra-user` | `admin` | Username VAMI |
| `--vra-password` | _(richiesto se `--vra-host`)_ | Password VAMI |

---

## Esempi di utilizzo

### Check base - solo vCenter

```bash
./check_vsphere_replication.py -H <VCENTER_HOST> -u <USERNAME> -p '<PASSWORD>'
```

Output:
```
OK! vSphere Replication healthy. 12 VM(s) replicated, no errors in last 24h | replicated_vms=12 replication_errors=0 replication_rpo_violations=0
```

### Check completo - vCenter + VRA

```bash
./check_vsphere_replication.py \
  -H <VCENTER_HOST> \
  -u <USERNAME> \
  -p '<PASSWORD>' \
  --vra-host <VRA_HOST> \
  --vra-user <VRA_USERNAME> \
  --vra-password '<VRA_PASSWORD>'
```

### Check con finestra temporale ridotta (ultime 6 ore)

```bash
./check_vsphere_replication.py \
  -H <VCENTER_HOST> \
  -u <USERNAME> \
  -p '<PASSWORD>' \
  --hours 6
```

### Esempio output CRITICAL

```
CRITICAL! 2 failure(s): VM:webserver01: HbrReplicationVmErrorEvent, VRA <VRA_HOST>: hbrsrv STOPPED | replicated_vms=12 replication_errors=2 replication_rpo_violations=0 svc_hms=1 svc_hbrsrv=0
```

### Esempio output WARNING

```
WARNING! 1 warning(s): VM:dbserver03: RPO violated [yellow] | replicated_vms=12 replication_errors=0 replication_rpo_violations=1
```

---

## Performance Data (perfdata)

Lo script emette perfdata compatibili con Icinga/Nagios dopo il pipe `|`:

| Metrica | Tipo | Descrizione |
|---|---|---|
| `replicated_vms` | gauge | Numero di VM con replica attiva (rilevate tramite `hbr_filter.*` in `extraConfig`) |
| `replication_errors` | gauge | Numero di errori CRITICAL trovati (allarmi + eventi) |
| `replication_rpo_violations` | gauge | Numero di violazioni RPO (WARNING) |
| `svc_hms` | gauge | Stato servizio HMS sul VRA (1=running, 0=stopped) - solo con `--vra-host` |
| `svc_hbrsrv` | gauge | Stato servizio HBRSRV sul VRA (1=running, 0=stopped) - solo con `--vra-host` |
| `trusted_connection` | gauge | Connessione trusted al vCenter (1=OK, 0=NON trusted) - solo con `--vra-host` |

---

## Dettagli tecnici

### Architettura dello script

```
check_vsphere_replication.py
├── check_vra_vami()        # VAMI API (opzionale)
│   ├── Login (POST /configure/requestHandlers/login)
│   ├── getAllServicesStatus → check hms, hbrsrv
│   └── getSummaryInfo → check trustedConnection
├── check_all_alarms()      # pyVmomi - allarmi attivi
│   ├── Scan RootFolder
│   ├── Scan tutti i Datacenter
│   └── Scan tutte le VM
├── check_global_events()   # pyVmomi - eventi HBR
│   ├── Filtra per eventTypeId (HbrReplication*, HbrHost*, HbrStorage*)
│   ├── Ignora messaggi di recovery
│   └── Deduplica per entity + event type
└── count_replicated_vms()  # pyVmomi - PropertyCollector
    └── Cerca extraConfig con key "hbr_filter.*"
```

### Event types monitorati

**CRITICAL** (replica rotta):
- `HbrReplicationVmErrorEvent` / `HbrReplicationVmFaultEvent`
- `HbrHostErrorEvent` / `HbrHostFaultEvent`
- `HbrStorageErrorEvent` / `HbrStorageFaultEvent`
- `HbrReplicationErrorEvent` / `HbrFailoverEvent`

**WARNING** (RPO violato ma replica attiva):
- `HbrVmRpoExceededEvent`

### Filtraggio intelligente

Lo script implementa un sistema di filtraggio per evitare falsi positivi:

1. **Recovery keywords** - ignora messaggi che contengono: `no longer violated`, `resolved`, `restored`, `recovered`, `completed successfully`
2. **Deduplicazione** - per entity + event type, riporta solo un'occorrenza
3. **Limit** - massimo 1000 eventi analizzati, 5 riportati nell'output (con conteggio totale)

### Rilevamento VM replicate

Le VM con vSphere Replication attiva vengono identificate tramite la presenza di chiavi `hbr_filter.*` nell'`extraConfig` della VM. Questo metodo usa il `PropertyCollector` per prestazioni ottimali anche con migliaia di VM.

### VAMI API endpoints utilizzati

| Endpoint | Metodo | Scopo |
|---|---|---|
| `/configure/requestHandlers/login` | POST | Autenticazione, ottiene `sessionId` |
| `/configure/requestHandlers/getAllServicesStatus` | POST | Stato di tutti i servizi VRA |
| `/configure/requestHandlers/getSummaryInfo` | POST | Info configurazione e connessione vCenter |

L'header `dr.config.service.sessionid` viene usato per l'autenticazione nelle chiamate successive al login.

---

## Configurazione Icinga / NetEye

### CheckCommand definition

```
object CheckCommand "check_vsphere_replication" {
  command = [ PluginDir + "/check_vsphere_replication.py" ]
  arguments = {
    "-H" = "$vsphere_replication_host$"
    "-u" = "$vsphere_replication_user$"
    "-p" = "$vsphere_replication_password$"
    "--port" = "$vsphere_replication_port$"
    "--hours" = "$vsphere_replication_hours$"
    "--vra-host" = "$vsphere_replication_vra_host$"
    "--vra-port" = "$vsphere_replication_vra_port$"
    "--vra-user" = "$vsphere_replication_vra_user$"
    "--vra-password" = "$vsphere_replication_vra_password$"
    "-t" = "$vsphere_replication_timeout$"
  }
}
```

### Service definition

```
apply Service "vsphere-replication" {
  check_command = "check_vsphere_replication"
  vars.vsphere_replication_host = "<VCENTER_HOST>"
  vars.vsphere_replication_user = "<USERNAME>"
  vars.vsphere_replication_password = "<PASSWORD>"
  vars.vsphere_replication_hours = 24
  vars.vsphere_replication_vra_host = "<VRA_HOST>"
  vars.vsphere_replication_vra_user = "<VRA_USERNAME>"
  vars.vsphere_replication_vra_password = "<VRA_PASSWORD>"
  check_interval = 5m
  retry_interval = 1m
  assign where host.vars.role == "vcenter"
}
```

---

## Licenza

MIT License
