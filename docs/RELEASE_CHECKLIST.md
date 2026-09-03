# Release-Checkliste

## Automatisch

- `scripts/test.ps1` erfolgreich
- Ruff erfolgreich
- Python-Compileall erfolgreich
- Releasearchive lassen sich vollständig öffnen
- Manifestgrößen stimmen mit den erzeugten Dateien überein
- SHA-256-Prüfsummen stimmen
- mindestens 54 Transport-, API- und Protokolltests erfolgreich
- echter erweiterter Slicer-Smoke-Test erfolgreich
- echter Direct-LAN-, Fallback-, Undo-, QA- und Revisionsvergleichstest erfolgreich
- `.livesegroom`-Dateizuordnung und Doppelklick-Import geprüft
- echtes Slicer-UI-Breitenprofil: Modul-Mindestbreite höchstens 360 px
- Archive enthalten keine `.git`, `.venv`, Datenbank-, Patientendaten- oder Secret-Dateien

## Manuell vor produktiver Nutzung

- Zwei-Nutzer-Abnahme in Slicer gemäß `MANUAL_TEST.md` (synthetischer Prozess-Test bestanden; visuelle Fachabnahme weiterhin durchführen)
- `.seg.nrrd`-Roundtrip mit realem, nicht personenbezogenem Testdatensatz
- Prüfung der verwendeten Slicer-Version
- Server hinter TLS-Reverse-Proxy und `LIVESEG_REQUIRE_HTTPS=true`
- institutionelle Authentifizierung mit individuellen Tokens und Rollenmapping
- Backup und Wiederherstellung getestet
- Datenschutz- und Medizinprodukteprüfung abgeschlossen
