# Hardware Health Checks – AstraeusNX

Werkzeuge, um Verschleiß an AstraeusNX früh zu erkennen: AIO-Kühlung, GPU, RAM und Laufwerke. Jede Prüfung stellt feste Testbedingungen her, prüft, dass sie wirklich gelten, und vergleicht das Ergebnis mit der eigenen Baseline. Entscheidend ist der Trend über Monate. Absolute Grenzwerte dienen nur als grobes Sicherheitsnetz.

## Überblick

| Skript | Prüft | Dauer | Rhythmus |
|---|---|---|---|
| `disk_health.py` | SMART aller Laufwerke: Defektzähler, Restlebensdauer, Verschleißprognose | Sekunden | wöchentlich per systemd-Timer |
| `aio_health.py` | Pumpe, Kühlmittel-Wärmeabfuhr, CPU-Kühlerkontakt, Gehäuselüfter | ca. 30 min | alle 3–4 Monate |
| `gpu_health.py` | GPU-Kühlung (Paste/Pads), Lüfterlager, Drosselung, Rechenfehler | ca. 17 min | alle 3–4 Monate |
| `ram_health.py` | Speicherfehler, EXPO noch aktiv, Bandbreite, Modultemperatur | ca. 31 min | alle 3–4 Monate |
| `health_all.py` | alle vier nacheinander mit Übersichtsseite | ca. 80 min | Quartalstermin |
| `health_common.py` | gemeinsame Bausteine, wird nicht direkt gestartet | – | – |

## Voraussetzungen

Alle Skripte brauchen root und holen sich `sudo` bei Bedarf selbst.

| Paket | Quelle | benötigt für |
|---|---|---|
| `python` ≥ 3.10 | extra | alle |
| `liquidctl` | extra | AIO, GPU (NZXT-Lüfter, Pumpe) |
| `stress-ng` | extra | AIO (ohne: schwächere Python-Last, nicht vergleichbar) |
| `python-nvidia-ml-py` | extra | GPU |
| `cuda`, `gpu-burn-git` | extra, AUR | GPU |
| `stressapptest` | AUR | RAM |
| `dmidecode` | extra | RAM (ohne: keine EXPO-Prüfung) |
| `smartmontools` | extra | Laufwerke |
| CoolerControl | – | wird für AIO- und GPU-Test pausiert und danach neu gestartet |

```fish
sudo pacman -S liquidctl stress-ng python-nvidia-ml-py cuda dmidecode smartmontools
paru -S gpu-burn-git stressapptest
```

## Installation

Alle Dateien liegen gemeinsam in `homelab-infra/astraeus_nx/scripts/health_checks/`. Die Test-Skripte importieren `health_common.py` aus demselben Ordner.

```fish
chmod +x aio_health.py gpu_health.py ram_health.py disk_health.py health_all.py
git add --chmod=+x aio_health.py gpu_health.py ram_health.py disk_health.py health_all.py
```

### Wöchentlicher Laufwerks-Check

Die Units liegen unter `homelab-infra/astraeus_nx/systemd/`. Der Pfad in `ExecStart` muss auf diesen Ordner zeigen.

```fish
sudo cp ../../systemd/disk-health.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now disk-health.timer
systemctl list-timers disk-health.timer
```

Findet ein Lauf ein FAIL, endet das Skript mit Exit-Code 2, und die Unit erscheint in `systemctl --failed`.

## Benutzung

```fish
sudo ./health_all.py                     # everything, summary page at the end
sudo ./health_all.py --skip gpu          # leave out single checks (repeatable)

sudo ./aio_health.py                     # single checks
sudo ./gpu_health.py
sudo ./ram_health.py
sudo ./disk_health.py
```

Während der aktiven Tests den Rechner nicht benutzen. Andere Last verfälscht die Messung, und der RAM-Test belegt rund 44 GB.

### Optionen

Alle Skripte kennen `--out-dir PATH` (anderer Ergebnisordner) und `--no-open` (Report nicht öffnen).

| Skript | Option | Standard | Zweck |
|---|---|---|---|
| `aio_health.py` | `--fan-duty` | 60 | feste Duty aller NZXT-Lüfter |
| | `--load-minutes` | 15 | Dauer der CPU-Last |
| | `--idle-min-minutes` / `--idle-max-minutes` | 2 / 10 | Rahmen für das Einschwingen im Leerlauf |
| | `--cooldown-minutes` | 5 | Abkühlphase |
| | `--sweep-settle` / `--sweep-measure` | 15 / 10 s | Pumpen-Sweep pro Stufe |
| | `--quick` | – | Probelauf mit 5 min Last, nicht mit vollen Läufen vergleichbar |
| | `--ambient` | – | Raumtemperatur von Hand |
| | `--prom-url` + `--prom-query` | – | Raumtemperatur aus Prometheus, die Query muss genau eine Serie liefern |
| `gpu_health.py` | `--gpu-fan-duty` | 75 | feste Duty der GPU-Lüfter |
| | `--fan-duty` | 60 | feste Duty der NZXT-Gehäuselüfter |
| | `--load-minutes` | 10 | Dauer von gpu-burn |
| | `--gpu` | 0 | NVML-Index der GPU |
| `ram_health.py` | `--minutes` | 30 | Dauer von stressapptest |
| `disk_health.py` | `--device PATH[:TYPE]` | – | zusätzliches Laufwerk, z. B. `/dev/sdd:sat` für USB-Gehäuse |
| `health_all.py` | `--skip NAME` | – | `disk`, `aio`, `gpu` oder `ram` auslassen |

Beispiel mit Raumtemperatur aus Netatmo:

```fish
sudo ./aio_health.py --prom-url http://mnemosyne.local:9090 \
    --prom-query 'netatmo_sensor_temperature_celsius{module="Office"}'
```

`health_all.py` übergibt diese Option nicht. In der Gesamtsitzung fehlt die Raumtemperatur also, das Urteil hängt aber nicht davon ab.

### Unterbrechen

Strg+C ist jederzeit sicher. Der laufende Test stellt Lüfter, Pumpe und CoolerControl wieder her und speichert sein Teilergebnis. `health_all.py` wartet darauf, überspringt die restlichen Tests und zeigt die Übersicht, sofern mindestens ein Ergebnis vorliegt.

Exit-Codes: 0 = in Ordnung, 2 = mindestens ein FAIL, 130 = unterbrochen.

## Ergebnisse

Alle Prüfungen schreiben nach `~/.local/share/hardware-health/`. Die Dateien gehören dem aufrufenden Benutzer, auch wenn das Skript unter sudo oder per Timer läuft.

| Präfix | Rohdaten | Report | Log |
|---|---|---|---|
| `aio-` | `aio-run-*.json` | `aio-report-*.html` | `aio-stress-*.log` |
| `gpu-` | `gpu-run-*.json` | `gpu-report-*.html` | `gpu-burn-*.log` |
| `ram-` | `ram-run-*.json` | `ram-report-*.html` | `ram-stress-*.log` |
| `disk-` | `disk-run-*.json` | `disk-report-*.html` | – (rohe SMART-Daten stecken in der JSON) |
| `all-` | `all-run-*.json` | `all-report-*.html` | – |

Ein Einzeltest öffnet seinen Report am Ende im Browser. Der Browser startet dabei als dein Benutzer in deiner Desktop-Sitzung, nie als root. `health_all.py` öffnet nur die Übersichtsseite.

### Baseline und Archiv

Jede Prüfung vergleicht mit dem **ersten** vergleichbaren Lauf im Ordner. Probeläufe, A/B-Tests (z. B. ohne Seitenteil) und verfälschte Läufe deshalb nach `archive/` verschieben, sonst werden sie zur Baseline oder tauchen im Trend auf:

```fish
mv ~/.local/share/hardware-health/gpu-*-20261012-170005.* ~/.local/share/hardware-health/archive/
```

Läufe gelten nur als vergleichbar, wenn ihre Bedingungen übereinstimmen:

| Prüfung | Vergleichsschlüssel |
|---|---|
| AIO | Kraken-Modell, Lüfter-Duty, Lastprogramm, Lastdauer, CPU-Threads, Power-Profil |
| GPU | GPU-Modell, Leistungslimit, Lüfter-Duty (GPU und Gehäuse), Lastdauer |
| RAM | Testgröße, Dauer, Modul-Teilenummern |
| Laufwerke | Modell und Seriennummer pro Laufwerk (nie der `/dev`-Pfad) |

Ändert sich eine dieser Größen, zum Beispiel durch den X3D-Turbo-Modus oder ein anderes Power-Profil, beginnt automatisch eine neue Baseline.

## Was geprüft wird

### AIO / CPU (`aio_health.py`)

Ablauf: CoolerControl pausieren, alle NZXT-Lüfter auf feste Duty setzen und zurücklesen, Pumpen-Sweep über 40/60/80/100 %, Leerlauf bis zur stabilen Kühlmitteltemperatur, Volllast mit stress-ng, Abkühlen, danach CoolerControl neu starten.

| Kennzahl | Bedeutung | Grenze |
|---|---|---|
| Pumpe bei 100 % | Lagerverschleiß | ≥ 2.500 rpm (Spezifikation 2.800 ± 300) |
| Pumpenstabilität | Luft im Kreislauf, Lager | < 2 % Schwankung |
| Kurze Drehzahleinbrüche | einzelne Ausreißer auf ~67 % | Info, Warnung ab 2 % der Messwerte |
| Kühlmittelanstieg pro Watt | Wärmeabfuhr des Radiators | Warnung ab 0,09 K/W, Trend Warnung ab +15 %, Fehler ab +30 % |
| CPU zu Kühlmittel pro Watt | Wärmeleitpaste, Anpressdruck | Info, Trend beobachten |
| Gleiche Lüfter-Duty | ein Kanal deutlich langsamer | < 70 % des Medians ergibt eine Warnung |
| Lüfter gesetzt | Schreibbefehl wirklich angekommen | sonst Warnung, Werte nicht vergleichbar |

Sicherheitsabbruch bei Kühlmittel ≥ 55 °C oder wenn die Pumpe bei 100 % unter 300 rpm fällt. Erzeugt die CPU-Last weniger als 90 % Auslastung, gilt der Test als ungültig.

### GPU (`gpu_health.py`)

Ablauf: CoolerControl pausieren, Gehäuse- und GPU-Lüfter auf feste Duty setzen und zurücklesen, Leerlauf, 10 min gpu-burn, Abkühlen, danach GPU-Lüfter auf Automatik und CoolerControl neu starten.

| Kennzahl | Bedeutung | Grenze |
|---|---|---|
| Temperaturanstieg pro Watt | Paste und Pads (Ersatz für den fehlenden Hotspot) | Trend Warnung ab +15 %, Fehler ab +30 % |
| Interner Sensor über Kern (experimentell) | aus T.Limit abgeleiteter Hotspot-Ersatz | Trend Warnung ab +3 K |
| Lüfterdrehzahl bei fester Duty | Lagerverschleiß | Warnung bei < 85 % der anderen oder −10 % zur Baseline |
| Drosselung wegen Temperatur | Kühlung hält das Leistungslimit nicht | 0 % der Messwerte |
| Power-Brake-Ereignisse | Netzteil oder Kabel | 0 % der Messwerte |
| Rechenfehler (gpu-burn) | GPU- oder VRAM-Stabilität | 0, sonst FAIL |

Die RTX-50-Serie liefert über NVML keine Hotspot-Temperatur. T.Limit folgt aber offenbar einem heißeren internen Sensor, daraus wird der experimentelle Wert berechnet. Sicherheitsabbruch, wenn der Kern 88 °C erreicht. Zieht gpu-burn nach 20 Sekunden weniger als 60 % des Leistungslimits, gilt der Test als ungültig.

### RAM (`ram_health.py`)

Ablauf: Modulgeschwindigkeit per dmidecode erfassen, stressapptest über 75 % des eingebauten RAM in 4-GB-Schritten (bei 64 GB also 44 GB), dabei die DDR5-Modultemperaturen aufzeichnen, danach das Kernel-Log nach Hardwarefehlern durchsuchen. CoolerControl bleibt aktiv.

| Kennzahl | Bedeutung | Grenze |
|---|---|---|
| Speicherfehler | instabiler Speicher oder Speichercontroller | 0, sonst FAIL |
| Kernel-Hardwarefehler | Machine-Check- oder EDAC-Meldungen | 0, sonst FAIL |
| Eingestellte Geschwindigkeit | EXPO nach BIOS-Update verloren? | Warnung bei Änderung zum letzten Lauf |
| Kopier-Bandbreite | Einstellungen verloren, Leistungseinbruch | Warnung ab −10 % zur Baseline |
| Modultemperatur | Luftstrom über den Modulen | Warnung ab 85 °C |

memtest86+ bleibt nach BIOS- oder EXPO-Änderungen der gründlichere Test, weil er den kompletten Speicher ohne laufendes Betriebssystem prüft.

### Laufwerke (`disk_health.py`)

Passiv, ohne Last und ohne Selbsttests. Laufwerke werden über Modell und Seriennummer erkannt.

| Kennzahl | Grenze |
|---|---|
| SMART-Gesamtstatus, NVMe-Warnungen, Reserveblöcke | FAIL bei Problem |
| Umgelagerte, wartende und nicht korrigierbare Sektoren, NVMe-Medienfehler | jeder Zuwachs ist FAIL |
| Samsung: Reserveblöcke, Schreib-, Lösch- und Laufzeitfehler, hinzugekommene defekte Flash-Blöcke | Baseline, Zuwachs ergibt eine Warnung |
| CRC-Fehler der Schnittstelle | Zuwachs ergibt eine Warnung (meist Kabel oder USB-Gehäuse) |
| Restlebensdauer | Warnung unter 20 %, Fehler unter 5 % |
| Verschleißprognose | ab 30 Tagen Abstand zwischen zwei Läufen, Warnung unter 1 Jahr |
| Temperatur | Warnung ab 70 °C (SSD) bzw. 55 °C (HDD) |
| Unsaubere Abschaltungen | Info mit Anteil an den Einschaltzyklen und Zuwachs |

Laufwerke, die beim letzten Mal da waren und jetzt fehlen, werden nur vermerkt, nicht als Fehler gewertet.

## Baselines (Stand 12.10.2026)

| Prüfung | Werte |
|---|---|
| AIO | 0,074 K/W Kühlmittel, Pumpe 2.753 rpm, alle Lüfter 60 %, Power-Profil `performance` |
| GPU | 0,153 K/W, interner Sensor +4 K, Lüfter 2.289 / 2.302 / 2.301 rpm bei 75 % |
| RAM | 0 Fehler, 69.707 MB/s, Module max. 70,5 °C, 6400 MT/s |
| Laufwerke | alle ≥ 98 % Restlebensdauer, 870 EVO mit 1 bzw. 5 defekten Flash-Blöcken als Baseline, ca. 20 % unsaubere Abschaltungen |

## Bekannte Stolperfallen

- **Lüfter nicht wirklich gesetzt:** Alle Kanäle in einem Schwung per `sync` zu beschreiben ließ einen Kanal auf seinem alten Wert. Die Skripte schreiben deshalb jeden Kanal einzeln und lesen ihn zurück. Bleibt eine Pinning-Warnung trotzdem, ist der Lauf nicht vergleichbar.
- **Lüfterzuordnung:** Pro NZXT-Controller gibt es nur drei Lüfterkanäle. Mehrere Lüfter hängen an Splittern, nur einer davon meldet seine Drehzahl. Kanäle mit 0 rpm sind unbelegt oder haben keinen Drehzahlausgang.
- **Turbo-Modus und Power-Profil:** Der X3D-Turbo-Modus halbiert die Kernzahl, `power-saver` drückt die Last. Beides ändert die Messwerte und startet deshalb eine neue Baseline.
- **USB-Laufwerke:** USB-Gehäuse brauchen oft `--device /dev/sdX:sat`.
- **Report öffnet sich nicht:** Ohne erkennbare Wayland- oder X-Sitzung wird nur der Pfad ausgegeben. Dann `xdg-open <pfad>` als normaler Benutzer aufrufen.
- **gpu-burn startet nicht:** Das Log `gpu-burn-*.log` nennt die Ursache. Das Skript bricht nach 20 s als ungültig ab.

## Aufbau

`health_common.py` enthält alles, was mehr als eine Prüfung braucht:

- **Urteile:** Konstanten (`OK`, `WARN`, `FAIL`, `INFO`, `INVALID`) und `check()`
- **Messhilfen:** gleitende Fenster und Steigung über Zeit
- **System:** root- und sudo-Behandlung, Ergebnisordner, Laden der Historie, Speichern, Öffnen des Reports
- **Hardware:** `CoolerControlPause` und `NzxtCooling` (Pumpe und Lüfter über liquidctl, mit Zurücklesen)
- **Ausgabe:** Konsolentabelle, HTML-Tabelle, SVG-Diagramme, Prüfplakette, CSS

Jede Prüfung ist ein eigenständiges Skript mit Ablauf, Auswertung, Report und `verdict_text()`. `health_all.py` startet die Prüfungen als eigene Prozesse und baut die Übersicht aus deren JSON-Ergebnissen.
