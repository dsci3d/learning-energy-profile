#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lernprofil-Orchestrator - Unified Workflow Manager
===================================================
Version 2.0

Zentrale Steuerung für die Auswertung von Lernenergie-Profilen.
Koordiniert Fragebogen-Auswertung, Profil-Berechnung, Visualisierung und Validierung.

DESIGN-PHILOSOPHIE
------------------
Der Orchestrator respektiert die eigenständige Ordnerlogik der Module:
- auswertung.py         → erzeugt ./auswertung/TIMESTAMP/
- auswertung_visualize.py → erzeugt ./charts/TIMESTAMP/

Der Orchestrator:
1. Ruft Module mit minimalen Argumenten auf
2. Ermittelt die tatsächlichen Output-Locations
3. Erstellt eine Session-Index-Datei mit Links zu allen Outputs

QUICK START
-----------
python auswertung_orchestrator.py                                      # Interaktiver Modus
python auswertung_orchestrator.py --csv questionaire_answered.csv      # Direkt mit CSV
python auswertung_orchestrator.py --csv questionaire_answered.csv --workflow full

VORDEFINIERTE WORKFLOWS
-----------------------
basic     = Profil-Berechnung + Text-Report
full      = Profil-Berechnung + Text-Report + HTML-Report + Visualisierungen
validate  = System-Tests + Validierung
minimal   = Nur Profil-Berechnung (JSON Output)

OUTPUT-STRUKTUR
---------------
Nach Ausführung existieren:

./auswertung/2025-11-27_14-30-00/
    ├── profil.json
    └── bericht.txt

./charts/2025-11-27_14-30-05/
    ├── report.html
    ├── radar_chart.png
    ├── dimension_bars.png
    └── chronotype.png

./lernprofil_sessions/
    └── session_20251127_143000.json   ← Session-Index mit Links

Author: PK
Version: 2.0
Date: 2025-11
"""

import argparse
import json
import subprocess
import sys
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any


@dataclass
class WorkflowConfig:
    """Zentrale Konfiguration für alle Scripts"""
    csv_path: Path
    profile_id: Optional[str] = None
    
    def __post_init__(self):
        self.csv_path = Path(self.csv_path).expanduser().resolve()
        
        # Profile-ID aus Dateinamen ableiten wenn nicht angegeben
        if self.profile_id is None:
            self.profile_id = self.csv_path.stem


@dataclass
class StageResult:
    """Ergebnis einer Workflow-Stage"""
    stage_name: str
    success: bool
    duration_seconds: float
    output_dir: Optional[Path] = None
    output_files: List[Path] = field(default_factory=list)
    error_message: Optional[str] = None
    stdout: str = ""
    stderr: str = ""


class LernprofilOrchestrator:
    """
    Koordiniert alle Auswertungs-Steps und erstellt Session-Index.
    
    Respektiert die eigenständige Ordnerlogik der Module.
    """
    
    WORKFLOWS: Dict[str, List[str]] = {
        'minimal': ['compute'],
        'basic': ['compute'],
        'full': ['compute', 'visualize'],
        'validate': ['test', 'validation']
    }
    
    def __init__(self, config: WorkflowConfig, timeout: int = 300):
        self.config = config
        self.timeout = timeout
        self.results: List[StageResult] = []
        self.session_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Scripts im gleichen Verzeichnis wie orchestrator
        self.script_dir = Path(__file__).parent.resolve()
        self.scripts = {
            'auswertung': self.script_dir / 'auswertung.py',
            'visualize': self.script_dir / 'auswertung_visualize.py',
            'test': self.script_dir / 'auswertung_test.py',
            'validation': self.script_dir / 'auswertung_validation.py'
        }
        
        # Tracking für Output-Locations
        self.profil_json_path: Optional[Path] = None
        self.report_txt_path: Optional[Path] = None
        self.charts_dir: Optional[Path] = None
        
        self._validate_scripts()
    
    def _validate_scripts(self) -> None:
        """Prüft ob alle benötigten Scripts vorhanden sind"""
        missing = []
        for name, path in self.scripts.items():
            if not path.exists():
                missing.append(f"{name}: {path}")
        
        if missing:
            print("❌ Fehlende Scripts:")
            for m in missing:
                print(f"   {m}")
            raise FileNotFoundError("Erforderliche Scripts nicht gefunden")
    
    def _run_subprocess(self, cmd: List[str], stage_name: str) -> StageResult:
        """
        Führt einen Subprocess aus und captured Output.
        """
        start_time = datetime.now()
        
        print(f"\n{'─'*60}")
        print(f"▶ {stage_name}")
        print(f"{'─'*60}")
        print(f"  Command: {' '.join(str(c) for c in cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                encoding='utf-8',
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}
            )
            
            duration = (datetime.now() - start_time).total_seconds()
            
            if result.returncode != 0:
                error_msg = result.stderr.splitlines()[0] if result.stderr else "Unknown error"
                print(f"  ❌ Fehlgeschlagen: {error_msg}")
                return StageResult(
                    stage_name=stage_name,
                    success=False,
                    duration_seconds=duration,
                    error_message=error_msg,
                    stdout=result.stdout,
                    stderr=result.stderr
                )
            
            print(f"  ✓ Erfolgreich ({duration:.1f}s)")
            
            return StageResult(
                stage_name=stage_name,
                success=True,
                duration_seconds=duration,
                stdout=result.stdout,
                stderr=result.stderr
            )
            
        except subprocess.TimeoutExpired:
            duration = (datetime.now() - start_time).total_seconds()
            error_msg = f"Timeout nach {self.timeout}s"
            print(f"  ❌ {error_msg}")
            return StageResult(
                stage_name=stage_name,
                success=False,
                duration_seconds=duration,
                error_message=error_msg
            )
        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds()
            print(f"  ❌ Fehler: {e}")
            return StageResult(
                stage_name=stage_name,
                success=False,
                duration_seconds=duration,
                error_message=str(e)
            )
    
    def _find_latest_subdir(self, base_dir: Path) -> Optional[Path]:
        """Findet das neueste Timestamp-Unterverzeichnis."""
        if not base_dir.exists():
            return None
        
        subdirs = [d for d in base_dir.iterdir() if d.is_dir()]
        if not subdirs:
            return None
        
        # Sortiere nach Name (Timestamp-Format sortiert chronologisch)
        subdirs.sort(key=lambda d: d.name, reverse=True)
        return subdirs[0]
    
    def _parse_output_path_from_stdout(self, stdout: str, pattern: str) -> Optional[Path]:
        """Extrahiert einen Pfad aus stdout anhand eines Patterns."""
        for line in stdout.splitlines():
            if pattern in line:
                # Extrahiere Pfad nach dem Pattern
                match = re.search(r':\s*(.+)$', line)
                if match:
                    return Path(match.group(1).strip())
        return None
    
    def _stage_compute(self) -> StageResult:
        """
        Stage: Profil berechnen
        
        Ruft auswertung.py auf und ermittelt danach die Output-Location.
        Das Modul erstellt selbst einen Timestamp-Ordner unter ./auswertung/
        """
        cmd = [
            sys.executable,
            str(self.scripts['auswertung']),
            str(self.config.csv_path),
            '--id', self.config.profile_id,
            '--output', 'profil.json',    # Modul nutzt nur Dateinamen
            '--report', 'bericht.txt',    # und legt eigenen Ordner an
            '--quiet'                      # Keine stdout-Duplikation
        ]
        
        result = self._run_subprocess(cmd, "Profil-Berechnung")
        
        if result.success:
            # Finde den neuesten Output-Ordner
            auswertung_base = Path.cwd() / "auswertung"
            latest_dir = self._find_latest_subdir(auswertung_base)
            
            if latest_dir:
                result.output_dir = latest_dir
                
                # Suche nach Output-Dateien
                profil_json = latest_dir / "profil.json"
                bericht_txt = latest_dir / "bericht.txt"
                
                if profil_json.exists():
                    self.profil_json_path = profil_json
                    result.output_files.append(profil_json)
                    print(f"  → JSON-Profil: {profil_json}")
                
                if bericht_txt.exists():
                    self.report_txt_path = bericht_txt
                    result.output_files.append(bericht_txt)
                    print(f"  → Text-Report: {bericht_txt}")
            else:
                print("  ⚠ Output-Verzeichnis nicht gefunden")
        
        return result
    
    def _stage_visualize(self) -> StageResult:
        """
        Stage: Visualisierungen erstellen
        
        Benötigt profil.json aus vorheriger Stage.
        """
        if not self.profil_json_path or not self.profil_json_path.exists():
            return StageResult(
                stage_name="Visualisierung",
                success=False,
                duration_seconds=0.0,
                error_message="profil.json nicht gefunden (Stage compute fehlgeschlagen?)"
            )
        
        cmd = [
            sys.executable,
            str(self.scripts['visualize']),
            str(self.profil_json_path)
        ]
        
        result = self._run_subprocess(cmd, "Visualisierung")
        
        if result.success:
            # Finde den neuesten Charts-Ordner
            charts_base = Path.cwd() / "charts"
            latest_dir = self._find_latest_subdir(charts_base)
            
            if latest_dir:
                result.output_dir = latest_dir
                self.charts_dir = latest_dir
                
                # Liste alle generierten Dateien
                for file in latest_dir.iterdir():
                    if file.is_file():
                        result.output_files.append(file)
                        print(f"  → {file.name}")
            else:
                print("  ⚠ Charts-Verzeichnis nicht gefunden")
        
        return result
    
    def _stage_test(self) -> StageResult:
        """Stage: Unit-Tests ausführen"""
        cmd = [
            sys.executable,
            str(self.scripts['test'])
        ]
        
        result = self._run_subprocess(cmd, "Unit-Tests")
        
        # Zeige Test-Zusammenfassung
        if result.stdout:
            for line in result.stdout.splitlines():
                if 'TEST-ZUSAMMENFASSUNG' in line or line.startswith('Tests ') or line.startswith('Erfolgreich'):
                    print(f"  {line}")
        
        return result
    
    def _stage_validation(self) -> StageResult:
        """Stage: Validierung ausführen"""
        cmd = [
            sys.executable,
            str(self.scripts['validation'])
        ]
        
        result = self._run_subprocess(cmd, "Validierung")
        
        # Zeige Validierungs-Status
        if result.stdout:
            for line in result.stdout.splitlines():
                if '✅' in line or '❌' in line or 'PRODUCTION READY' in line:
                    print(f"  {line}")
        
        return result
    
    def run_workflow(self, workflow_name: str) -> bool:
        """Führt vordefinierten Workflow aus"""
        if workflow_name not in self.WORKFLOWS:
            print(f"❌ Unbekannter Workflow: {workflow_name}")
            print(f"   Verfügbar: {', '.join(self.WORKFLOWS.keys())}")
            return False
        
        stages = self.WORKFLOWS[workflow_name]
        
        print("\n" + "═"*60)
        print(f"WORKFLOW: {workflow_name.upper()}")
        print("═"*60)
        print(f"Stages: {' → '.join(stages)}")
        self._show_config()
        
        # Führe Stages aus
        for stage in stages:
            stage_method = getattr(self, f'_stage_{stage}', None)
            if stage_method is None:
                print(f"❌ Stage nicht implementiert: {stage}")
                return False
            
            result = stage_method()
            self.results.append(result)
            
            if not result.success:
                print(f"\n❌ Workflow abgebrochen: {result.stage_name} fehlgeschlagen")
                self._write_session_index()
                return False
        
        self._write_session_index()
        self._print_final_summary()
        return True
    
    def _write_session_index(self) -> None:
        """
        Schreibt Session-Index als JSON.
        
        Der Index verweist auf die tatsächlichen Output-Locations der Module.
        """
        sessions_dir = Path.cwd() / "lernprofil_sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        
        index = {
            'session_id': self.session_timestamp,
            'timestamp': datetime.now().isoformat(),
            'config': {
                'csv_path': str(self.config.csv_path),
                'profile_id': self.config.profile_id
            },
            'outputs': {
                'profil_json': str(self.profil_json_path) if self.profil_json_path else None,
                'report_txt': str(self.report_txt_path) if self.report_txt_path else None,
                'charts_dir': str(self.charts_dir) if self.charts_dir else None
            },
            'stages': [
                {
                    'name': r.stage_name,
                    'success': r.success,
                    'duration_seconds': r.duration_seconds,
                    'output_dir': str(r.output_dir) if r.output_dir else None,
                    'output_files': [str(f) for f in r.output_files],
                    'error_message': r.error_message
                }
                for r in self.results
            ]
        }
        
        index_path = sessions_dir / f"session_{self.session_timestamp}.json"
        index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding='utf-8')
        
        print(f"\n  Session-Index: {index_path}")
    
    def _print_final_summary(self) -> None:
        """Zeigt finale Zusammenfassung"""
        print("\n" + "═"*60)
        print("SESSION ABGESCHLOSSEN")
        print("═"*60)
        
        total_duration = sum(r.duration_seconds for r in self.results)
        successful = sum(1 for r in self.results if r.success)
        
        print(f"Stages: {successful}/{len(self.results)} erfolgreich")
        print(f"Dauer: {total_duration:.1f}s")
        
        print("\nGenerierte Outputs:")
        
        if self.profil_json_path and self.profil_json_path.exists():
            print(f"  ✓ JSON-Profil:  {self.profil_json_path}")
        
        if self.report_txt_path and self.report_txt_path.exists():
            print(f"  ✓ Text-Report:  {self.report_txt_path}")
        
        if self.charts_dir and self.charts_dir.exists():
            print(f"  ✓ Charts:       {self.charts_dir}/")
            html_report = self.charts_dir / "report.html"
            if html_report.exists():
                print(f"                  → Öffnen Sie report.html im Browser")
    
    def _show_config(self) -> None:
        """Zeigt aktuelle Konfiguration"""
        print(f"\nKonfiguration:")
        
        if self.config.csv_path.name == 'dummy.csv':
            print("  CSV:        (nicht verwendet)")
        else:
            print(f"  CSV:        {self.config.csv_path}")
        
        print(f"  Profile-ID: {self.config.profile_id}")
    
    def interactive_menu(self) -> None:
        """Interaktives Menü zur Workflow-Auswahl"""
        print("\n" + "═"*60)
        print("LERNPROFIL-ORCHESTRATOR v2.0")
        print("═"*60)
        
        self._show_config()
        
        print("\nWorkflows:")
        print("  1. minimal  → Nur Profil-Berechnung (JSON)")
        print("  2. basic    → Profil + Text-Report")
        print("  3. full     → Profil + Text + Visualisierungen + HTML")
        print("  4. validate → System-Tests + Validierung")
        print("  Q. Beenden")
        
        while True:
            choice = input("\nAuswahl (1-4 oder Q): ").strip().upper()
            
            workflow_map = {
                '1': 'minimal',
                '2': 'basic',
                '3': 'full',
                '4': 'validate'
            }
            
            if choice == 'Q':
                print("Beendet.")
                return
            
            if choice in workflow_map:
                workflow = workflow_map[choice]
                success = self.run_workflow(workflow)
                
                if success:
                    again = input("\nWeiteren Workflow? (j/N): ").strip().lower()
                    if again != 'j':
                        return
                else:
                    return
            else:
                print("Ungültige Auswahl.")


def interactive_setup() -> WorkflowConfig:
    """Interaktiver Setup-Dialog"""
    print("\n" + "═"*60)
    print("LERNPROFIL-ORCHESTRATOR - SETUP")
    print("═"*60)
    
    while True:
        print("\nCSV-Datei mit Fragebogen-Antworten:")
        print("  Format: item_code, rating | 88 Zeilen | Likert 1-5")
        
        csv_path = None
        while csv_path is None:
            csv_input = input("  Pfad zur CSV: ").strip()
            
            if not csv_input:
                # Suche im aktuellen Verzeichnis
                csvs = list(Path.cwd().glob("*.csv"))
                if csvs:
                    print("\n  Gefundene CSV-Dateien:")
                    for i, csv in enumerate(csvs, 1):
                        print(f"    {i}. {csv.name}")
                    selection = input("  Nummer oder Pfad: ").strip()
                    if selection.isdigit() and 1 <= int(selection) <= len(csvs):
                        csv_input = str(csvs[int(selection)-1])
                    else:
                        csv_input = selection
            
            csv = Path(csv_input).expanduser()
            if csv.is_file():
                csv_path = csv
            else:
                print("  [FEHLER] Datei nicht gefunden.")
        
        # Profile-ID
        print(f"\nProfile-ID (Standard: {csv_path.stem}):")
        profile_id = input("  ID: ").strip() or csv_path.stem
        
        # Bestätigung
        print("\n" + "─"*60)
        print(f"  CSV:        {csv_path}")
        print(f"  Profile-ID: {profile_id}")
        print("─"*60)
        
        if input("\nKorrekt? (j/N): ").strip().lower() == 'j':
            return WorkflowConfig(csv_path=csv_path, profile_id=profile_id)


def main():
    parser = argparse.ArgumentParser(
        description="Lernprofil-Orchestrator v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  %(prog)s                                           # Interaktiv
  %(prog)s --csv antworten.csv --workflow basic      # Basic
  %(prog)s --csv antworten.csv --workflow full       # Mit Visualisierung
  %(prog)s --workflow validate                       # Nur Tests
        """
    )
    
    parser.add_argument('--csv', dest='csv_path',
                       help='CSV-Datei mit Fragebogen-Antworten')
    parser.add_argument('--workflow',
                       choices=['minimal', 'basic', 'full', 'validate'],
                       help='Workflow direkt ausführen')
    parser.add_argument('--id', dest='profile_id',
                       help='Profile-ID (Standard: CSV-Dateiname)')
    parser.add_argument('--timeout', type=int, default=300,
                       help='Timeout pro Stage (Standard: 300s)')
    
    args = parser.parse_args()
    
    # Routing
    if not args.csv_path and not args.workflow:
        config = interactive_setup()
    elif not args.csv_path and args.workflow == 'validate':
        config = WorkflowConfig(csv_path=Path('dummy.csv'))
    elif not args.csv_path:
        print("❌ --csv erforderlich für diesen Workflow")
        return 1
    else:
        config = WorkflowConfig(
            csv_path=args.csv_path,
            profile_id=args.profile_id
        )
    
    # Orchestrator starten
    try:
        orchestrator = LernprofilOrchestrator(config, timeout=args.timeout)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 1
    
    if args.workflow:
        success = orchestrator.run_workflow(args.workflow)
        return 0 if success else 1
    else:
        orchestrator.interactive_menu()
        return 0


if __name__ == '__main__':
    sys.exit(main())