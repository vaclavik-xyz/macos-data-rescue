"""Localized strings (Czech default, English) and display formatters for the GUI."""
from __future__ import annotations

LANGUAGES = ("cs", "en")
LANGUAGE_NAMES = {"cs": "Čeština", "en": "English"}

HOME_PHASES = ("visible-home", "hidden-home", "app-data", "applications", "full-home", "all")
# volume and restore jobs have a single scope; scan/copy/verify use "all"
SINGLE_SCOPE_PHASES = ("all",)


def phases_for_profile(profile: str | None) -> tuple[str, ...]:
    return HOME_PHASES if profile in (None, "customer-home") else SINGLE_SCOPE_PHASES


MESSAGES: dict[str, dict[str, str]] = {
    "cs": {
        "app_title": "macOS Data Rescue — servisní konzole technika",
        "app_subtitle": "Záchrana dat z poškozeného Macu připojeného přes Share Disk / Target Disk",
        "language_label": "Jazyk:",
        "tab_setup": " 1. Příprava a kontrola ",
        "tab_rescue": " 2. Záchrana a průběh ",
        "tab_verify": " 3. Ověření a duplikáty ",
        "tab_reports": " 4. Protokoly ",
        "paths_title": "Cesty úlohy",
        "source_path": "Zdroj (jen ke čtení):",
        "source_hint": "Např. /Volumes/Macintosh HD - Data/Users/zakaznik — zdroj se nikdy nemění",
        "dest_path": "Cíl:",
        "dest_hint": "Např. /Volumes/RecoverySSD/Zakaznik/user-data — na záchranném disku, mimo zdroj",
        "job_path": "Pracovní složka úlohy:",
        "job_hint": "Manifest a záznamy, např. /Volumes/RecoverySSD/Zakaznik/.rescue — mimo zdroj",
        "browse": "Vybrat…",
        "profile": "Profil:",
        "profile_home": "Domovská složka zákazníka (doporučeno)",
        "profile_volume": "Disk / složka (volume)",
        "profile_restore": "Obnova na nový disk (restore)",
        "volume_excludes": "Vyloučené vzory (čárkou oddělené, jen volume):",
        "job_loaded_note": "Úloha je otevřená: zdroj, cíl a profil určuje manifest a nelze je měnit. "
                           "Pro jinou úlohu ji nejdřív zavřete.",
        "btn_preflight": "Spustit kontrolu (preflight)",
        "btn_init": "Kontrola a založení úlohy",
        "btn_open_job": "Otevřít existující úlohu…",
        "btn_close_job": "Zavřít úlohu",
        "preflight_title": "Bezpečnostní kontrola prostředí (preflight)",
        "col_status": "Stav",
        "col_check": "Kontrola",
        "col_detail": "Podrobnosti",
        "level_ok": "✓ OK",
        "level_warn": "⚠ varování",
        "level_fail": "✗ chyba",
        "preflight_none": "Kontrola zatím neproběhla. Spouští se také automaticky před každým skenem a kopírováním.",
        "preflight_running": "Kontrola probíhá…",
        "preflight_fail": "Kontrola SELHALA ({failures} chyb, {warnings} varování). Opravte chyby, jinak nelze pokračovat.",
        "preflight_ok": "Kontrola v pořádku ({checks} testů, {warnings} varování).",
        "guide_title": "Doporučený další krok (next)",
        "guide_default": "Otevřete nebo založte úlohu.",
        "btn_guide_run": "Spustit: {command}",
        "btn_guide_approve": "Zaznamenat souhlas: {phase}…",
        "btn_guide_acknowledge": "Pokračovat po potvrzení: {command}…",
        "btn_guide_none": "Není co spustit",
        "control_title": "Fáze záchrany",
        "select_phase": "Fáze:",
        "btn_approve": "Zaznamenat souhlas…",
        "approved_badge": "Schváleno ({by})",
        "no_approval_needed": "Bez schválení",
        "btn_scan": "1. Skenovat",
        "btn_copy": "2. Kopírovat",
        "btn_stop": "Zastavit",
        "btn_stop_waiting": "Zastavit čekání",
        "btn_resume": "Pokračovat (resume)",
        "caffeinate": "Nedovolit Macu usnout během operace (caffeinate)",
        "per_file_timeout": "Timeout na soubor (s):",
        "scan_budget": "Časový limit skenu (s, volitelné):",
        "limit_optional": "Limit souborů (volitelné):",
        "field_file_timeout": "Timeout na soubor",
        "field_scan_budget": "Časový limit skenu",
        "field_limit": "Limit souborů",
        "auto_reconnect": "Automaticky obnovit po odpojení disku",
        "reconnect_cooldown": "Chladnutí (s):",
        "reconnect_wait": "Čekání na disk (s):",
        "max_reconnects": "Max. obnovení:",
        "field_reconnect_cooldown": "Chladnutí",
        "field_reconnect_wait": "Čekání na disk",
        "field_max_reconnects": "Max. obnovení",
        "telemetry_title": "Průběh",
        "data_volume": "Data:",
        "file_count": "Soubory:",
        "speed": "Rychlost: {rate}",
        "eta": "Zbývá: {eta}",
        "eta_unknown": "—",
        "current_file": "Soubor: {file}",
        "badge_copied": "Zkopírováno",
        "badge_fallback": "Z duplikátu",
        "badge_pending": "Ve frontě",
        "badge_timed_out": "Vypršel čas",
        "badge_failed": "Selhalo",
        "badge_compressed": "Nečitelná komprese",
        "badge_skipped": "Přeskočeno",
        "log_title": "Záznam činnosti",
        "clear_log": "Vymazat záznam",
        "state_idle": "PŘIPRAVENO",
        "state_running": "PROBÍHÁ",
        "state_waiting": "ČEKÁ NA DISK",
        "state_stopping": "ZASTAVUJI…",
        "state_stopped": "ZASTAVENO",
        "state_paused_storage": "POZASTAVENO – ÚLOŽIŠTĚ",
        "state_failed": "CHYBA",
        "state_done": "DOKONČENO",
        "msg_stopping": "Zastavuji operaci. Počkejte, až proces skutečně skončí — "
                        "do té doby nelze spustit nic dalšího.",
        "msg_stopped": "Operace byla zastavena. Zapsaný stav zůstal v manifestu; pokračujte doporučeným krokem.",
        "msg_paused_storage": "Úložiště není dostupné — znovu připojte původní disk (případně uvolněte místo "
                              "na cílovém disku) a pak pokračujte tlačítkem Pokračovat.",
        "msg_pause_reason": "Důvod: {reason}",
        "msg_waiting": "Disk se odpojil — automatické obnovení čeká na jeho návrat. Nic dalšího nespouštějte.",
        "msg_waiting_cooldown": "Chladnutí disku: zbývá {remaining} (pokus {attempt}). Na disk se teď nesahá.",
        "msg_waiting_wait": "Čekám na návrat disku: nejdéle ještě {remaining} (pokus {attempt}). "
                            "Pokud se nevrátí sám, připojte ho znovu.",
        "msg_waiting_stop_hint": "„Zastavit čekání“ běh čistě ukončí; později pokračujte tlačítkem Pokračovat.",
        "msg_waiting_external": "Čeká jiný proces (např. v Terminálu); tato konzole ho nemůže zastavit.",
        "msg_storage_escalation": "Automatické obnovení to vzdalo — disk se opakovaně odpojoval, nepřibývala "
                                  "nová data nebo se nevrátil. Doporučení: ukončete kopírování po souborech a "
                                  "pořiďte image disku nástrojem ddrescue s mapfile, případně disk předejte do "
                                  "datové laboratoře.",
        "storage_events_summary": "Odpojení disku: {losses} · automaticky obnoveno: {reconnects}",
        "msg_failed": "Příkaz „{command}“ skončil chybou (kód {code}). Podrobnosti jsou v záznamu činnosti.",
        "msg_preflight_failed": "Kontrola prostředí selhala, operace nebyla spuštěna. Výsledek je na kartě Příprava.",
        "msg_job_unresponsive": "Pracovní složka neodpovídá už {seconds} s — zkontrolujte záchranný disk.",
        "log_running": "▶ {command}",
        "log_wrote_file": "Zapsáno: {path}",
        "log_stop_sigint": "Zastavuji: posílám přerušení (SIGINT)…",
        "log_stop_sigterm": "Proces nereaguje, posílám SIGTERM…",
        "log_stop_sigkill": "Proces stále běží, ukončuji ho (SIGKILL)…",
        "log_child_still_alive": "Proces {pid} stále běží (pravděpodobně čeká na disk); čekám na jeho ukončení.",
        "log_finished_ok": "✓ Dokončeno.",
        "log_finished_code": "✗ Skončilo s kódem {code}.",
        "log_paused_storage": "⏸ Kopírování pozastaveno: úložiště není dostupné (kód 3).",
        "log_stopped": "■ Zastaveno obsluhou.",
        "log_stopped_waiting": "■ Čekání na disk zastaveno obsluhou (kód 3); pokračujte tlačítkem Pokračovat.",
        "log_runner_error": "✗ Chyba spouštění: {error}",
        "log_job_read_error": "Nelze načíst úlohu: {error}",
        "verify_title": "Ověření integrity cíle (SHA256)",
        "verify_info": "Porovná zkopírované soubory s otisky SHA256 pořízenými při kopírování. Zdroj se znovu nečte.",
        "btn_verify": "Ověřit integritu",
        "dups_title": "Selhané soubory a záchrana z duplikátu",
        "dups_info": "Kandidáti se hledají jen podle metadat (velikost, název) — nejsou důkazem shodného obsahu. "
                     "Kandidáta před výběrem ověřte; nic se nekopíruje automaticky.",
        "failed_files_label": "Selhané soubory:",
        "col_path": "Cesta",
        "col_size": "Velikost",
        "col_evidence": "Shoda",
        "btn_find_dups": "Najít kandidáty",
        "cands_label": "Kandidáti ve zdroji:",
        "col_cand_path": "Cesta ke kandidátovi",
        "btn_recover_cand": "Zachránit z vybraného kandidáta…",
        "no_candidates": "Pro soubor {path} nebyl nalezen žádný kandidát.",
        "cust_report_title": "Protokol pro zákazníka",
        "report_format": "Formát:",
        "report_lang": "Jazyk protokolu:",
        "lang_cs": "čeština",
        "lang_en": "angličtina",
        "btn_gen_cust_report": "Vytvořit protokol",
        "btn_open_report": "Otevřít protokol",
        "report_written": "Protokol uložen: {path}",
        "restore_no_customer_report": "Úloha typu restore nemá zákaznický protokol; použijte protokol záchranné úlohy.",
        "tech_report_title": "Technická zpráva",
        "btn_refresh_tech": "Načíst zprávu",
        "btn_save_tech": "Uložit do souboru…",
        "tech_report_loading": "Načítám…",
        "err_title": "Chyba",
        "info_title": "Informace",
        "confirm_stop_title": "Zastavit operaci",
        "confirm_stop_msg": "Zastavit probíhající operaci?\n\nZapsaný stav v manifestu zůstane a lze na něj navázat.",
        "confirm_stop_waiting_msg": "Zastavit čekání na disk?\n\nBěh skončí jako pozastavený (kód 3), bez "
                                    "vzdání automatického obnovení; později pokračujte tlačítkem Pokračovat.",
        "confirm_close_title": "Ukončit aplikaci",
        "confirm_close_msg": "Probíhá operace. Zastavit ji a po jejím ukončení zavřít aplikaci?",
        "confirm_close_stopping_msg": "Proces se stále ukončuje. Zavřít okno hned? "
                                      "Proces může ještě chvíli doběhnout na pozadí.",
        "approve_dialog_title": "Souhlas pro fázi {phase}",
        "approve_dialog_text": "Fáze „{phase}“ vyžaduje výslovný souhlas zákazníka. Zaznamenejte ho jen tehdy, "
                               "když zákazník skutečně souhlasil. Souhlas se uloží do manifestu se jménem a časem.",
        "approve_by_label": "Kdo souhlasil (jméno):",
        "approve_consent_check": "Zákazník s touto fází výslovně souhlasil",
        "btn_confirm_approval": "Zaznamenat souhlas",
        "btn_cancel": "Zrušit",
        "select_source_title": "Vyberte připojenou domovskou složku (zdroj)",
        "select_dest_title": "Vyberte cílovou složku na záchranném disku",
        "select_job_title": "Vyberte pracovní složku úlohy",
        "save_tech_title": "Uložit technickou zprávu",
        "err_job_required": "Zadejte pracovní složku úlohy.",
        "err_paths_required": "Vyplňte zdroj, cíl i pracovní složku.",
        "err_invalid_number": "{field}: zadejte kladné číslo.",
        "err_invalid_limit": "{field}: zadejte kladné celé číslo nebo nechte prázdné.",
        "err_invalid_non_negative": "{field}: zadejte nulu nebo kladné číslo.",
        "err_invalid_count": "{field}: zadejte kladné celé číslo.",
        "confirm_ack_title": "Automatické obnovení to vzdalo",
        "confirm_ack_msg": "Automatické obnovení po odpojení disku to vzdalo: disk se opakovaně odpojoval, "
                           "nepřibývala nová data nebo se nevrátil.\n\nDoporučení: ukončete kopírování po souborech "
                           "a pořiďte image disku nástrojem ddrescue s mapfile, případně disk předejte do datové "
                           "laboratoře.\n\nPřesto pokračovat a automatické obnovení znovu povolit? Potvrzení se "
                           "zaznamená do úlohy (--acknowledge-storage-loss).",
        "confirm_continue_msg": "Automatické obnovení po odpojení disku to vzdalo: disk se opakovaně odpojoval, "
                                "nepřibývala nová data nebo se nevrátil.\n\nDoporučení: ukončete kopírování po "
                                "souborech a pořiďte image disku nástrojem ddrescue s mapfile, případně disk "
                                "předejte do datové laboratoře.\n\nPřesto pokračovat v kopírování po souborech "
                                "bez automatického obnovení?",
        "err_select_failed": "Vyberte selhaný soubor v levé tabulce.",
        "err_select_both": "Vyberte selhaný soubor i kandidáta.",
        "err_job_load": "Úlohu nelze otevřít:\n{error}",
        "err_busy": "Operace už běží.",
        "err_not_gated": "Fáze {phase} souhlas nevyžaduje.",
        "confirm_fallback_title": "Záchrana z duplikátu",
        "confirm_fallback_msg": "Nahradit selhaný soubor\n{path}\n\nobsahem kandidáta\n{candidate}?\n\n"
                                "Shoda: {evidence}\nPokračujte jen, pokud jste ověřili, že jde o stejný soubor.",
        "saved_msg": "Uloženo: {path}",
    },
    "en": {
        "app_title": "macOS Data Rescue — technician console",
        "app_subtitle": "Rescue data from a damaged Mac mounted via Share Disk / Target Disk",
        "language_label": "Language:",
        "tab_setup": " 1. Setup & preflight ",
        "tab_rescue": " 2. Rescue & progress ",
        "tab_verify": " 3. Verification & duplicates ",
        "tab_reports": " 4. Reports ",
        "paths_title": "Job paths",
        "source_path": "Source (read-only):",
        "source_hint": "E.g. /Volumes/Macintosh HD - Data/Users/customer — the source is never modified",
        "dest_path": "Destination:",
        "dest_hint": "E.g. /Volumes/RecoverySSD/Customer/user-data — on the recovery disk, outside the source",
        "job_path": "Job directory:",
        "job_hint": "Manifest and logs, e.g. /Volumes/RecoverySSD/Customer/.rescue — outside the source",
        "browse": "Browse…",
        "profile": "Profile:",
        "profile_home": "Customer home (recommended)",
        "profile_volume": "Disk / folder (volume)",
        "profile_restore": "Restore to new disk (restore)",
        "volume_excludes": "Exclude patterns (comma-separated, volume only):",
        "job_loaded_note": "A job is open: source, destination and profile come from its manifest and "
                           "cannot be changed. Close the job to work on another one.",
        "btn_preflight": "Run preflight",
        "btn_init": "Check and create job",
        "btn_open_job": "Open existing job…",
        "btn_close_job": "Close job",
        "preflight_title": "Environment safety checks (preflight)",
        "col_status": "Status",
        "col_check": "Check",
        "col_detail": "Detail",
        "level_ok": "✓ OK",
        "level_warn": "⚠ warning",
        "level_fail": "✗ failed",
        "preflight_none": "No preflight yet. It also runs automatically before every scan and copy.",
        "preflight_running": "Preflight running…",
        "preflight_fail": "Preflight FAILED ({failures} failures, {warnings} warnings). Fix the failures to continue.",
        "preflight_ok": "Preflight passed ({checks} checks, {warnings} warnings).",
        "guide_title": "Recommended next step (next)",
        "guide_default": "Open or create a job.",
        "btn_guide_run": "Run: {command}",
        "btn_guide_approve": "Record consent: {phase}…",
        "btn_guide_acknowledge": "Continue after confirmation: {command}…",
        "btn_guide_none": "Nothing to run",
        "control_title": "Rescue phases",
        "select_phase": "Phase:",
        "btn_approve": "Record consent…",
        "approved_badge": "Approved ({by})",
        "no_approval_needed": "No approval needed",
        "btn_scan": "1. Scan",
        "btn_copy": "2. Copy",
        "btn_stop": "Stop",
        "btn_stop_waiting": "Stop waiting",
        "btn_resume": "Resume",
        "caffeinate": "Keep the Mac awake during operations (caffeinate)",
        "per_file_timeout": "Per-file timeout (s):",
        "scan_budget": "Scan time budget (s, optional):",
        "limit_optional": "File limit (optional):",
        "field_file_timeout": "Per-file timeout",
        "field_scan_budget": "Scan time budget",
        "field_limit": "File limit",
        "auto_reconnect": "Automatically reconnect after disk loss",
        "reconnect_cooldown": "Cooldown (s):",
        "reconnect_wait": "Wait for disk (s):",
        "max_reconnects": "Max. reconnects:",
        "field_reconnect_cooldown": "Cooldown",
        "field_reconnect_wait": "Wait for disk",
        "field_max_reconnects": "Max. reconnects",
        "telemetry_title": "Progress",
        "data_volume": "Data:",
        "file_count": "Files:",
        "speed": "Speed: {rate}",
        "eta": "ETA: {eta}",
        "eta_unknown": "—",
        "current_file": "File: {file}",
        "badge_copied": "Copied",
        "badge_fallback": "From duplicate",
        "badge_pending": "Pending",
        "badge_timed_out": "Timed out",
        "badge_failed": "Failed",
        "badge_compressed": "Unreadable compression",
        "badge_skipped": "Skipped",
        "log_title": "Activity log",
        "clear_log": "Clear log",
        "state_idle": "READY",
        "state_running": "RUNNING",
        "state_waiting": "WAITING FOR DISK",
        "state_stopping": "STOPPING…",
        "state_stopped": "STOPPED",
        "state_paused_storage": "PAUSED – STORAGE",
        "state_failed": "FAILED",
        "state_done": "DONE",
        "msg_stopping": "Stopping the operation. Wait until the process has really exited — "
                        "nothing else can start until then.",
        "msg_stopped": "The operation was stopped. Committed progress stays in the manifest; "
                       "continue with the recommended step.",
        "msg_paused_storage": "Storage unavailable — reconnect the original disk (or free space on the "
                              "destination), then press Resume.",
        "msg_pause_reason": "Reason: {reason}",
        "msg_waiting": "The disk disconnected — automatic reconnect is waiting for it. Do not start anything else.",
        "msg_waiting_cooldown": "Disk cooldown: {remaining} left (attempt {attempt}). Nothing touches the disk now.",
        "msg_waiting_wait": "Waiting for the disk to come back: at most {remaining} more (attempt {attempt}). "
                            "Reconnect it if it does not come back on its own.",
        "msg_waiting_stop_hint": "“Stop waiting” ends the run cleanly; press Resume later to continue.",
        "msg_waiting_external": "Another process (e.g. in Terminal) is waiting; this console cannot stop it.",
        "msg_storage_escalation": "Automatic reconnect gave up — the disk kept disconnecting, no new data was "
                                  "copied, or it did not come back. Recommended: stop file-level copying and "
                                  "image the disk with ddrescue and a mapfile, or hand it to a data recovery lab.",
        "storage_events_summary": "Disk disconnections: {losses} · reconnected automatically: {reconnects}",
        "msg_failed": "Command “{command}” failed (exit {code}). See the activity log for details.",
        "msg_preflight_failed": "Preflight failed, so the operation was not started. See the Setup tab.",
        "msg_job_unresponsive": "The job directory has not responded for {seconds} s — check the recovery disk.",
        "log_running": "▶ {command}",
        "log_wrote_file": "Wrote: {path}",
        "log_stop_sigint": "Stopping: sending interrupt (SIGINT)…",
        "log_stop_sigterm": "Process did not stop, sending SIGTERM…",
        "log_stop_sigkill": "Process still running, killing it (SIGKILL)…",
        "log_child_still_alive": "Process {pid} is still alive (probably waiting on a disk); waiting for it to exit.",
        "log_finished_ok": "✓ Finished.",
        "log_finished_code": "✗ Exited with code {code}.",
        "log_paused_storage": "⏸ Copy paused: storage unavailable (exit 3).",
        "log_stopped": "■ Stopped by operator.",
        "log_stopped_waiting": "■ Waiting for the disk stopped by operator (exit 3); press Resume to continue.",
        "log_runner_error": "✗ Could not run the command: {error}",
        "log_job_read_error": "Cannot read the job: {error}",
        "verify_title": "Destination integrity (SHA256)",
        "verify_info": "Compares copied files with SHA256 digests captured during copying. "
                       "The source is not read again.",
        "btn_verify": "Verify integrity",
        "dups_title": "Failed files and recovery from a duplicate",
        "dups_info": "Candidates are metadata matches (size, name) only — not proof of identical content. "
                     "Inspect a candidate before selecting it; nothing is copied automatically.",
        "failed_files_label": "Failed files:",
        "col_path": "Path",
        "col_size": "Size",
        "col_evidence": "Match",
        "btn_find_dups": "Find candidates",
        "cands_label": "Candidates in the source:",
        "col_cand_path": "Candidate path",
        "btn_recover_cand": "Recover from selected candidate…",
        "no_candidates": "No candidate found for {path}.",
        "cust_report_title": "Customer report",
        "report_format": "Format:",
        "report_lang": "Report language:",
        "lang_cs": "Czech",
        "lang_en": "English",
        "btn_gen_cust_report": "Create report",
        "btn_open_report": "Open report",
        "report_written": "Report written: {path}",
        "restore_no_customer_report": "Restore jobs have no customer report; use the rescue job's report.",
        "tech_report_title": "Technician report",
        "btn_refresh_tech": "Load report",
        "btn_save_tech": "Save to file…",
        "tech_report_loading": "Loading…",
        "err_title": "Error",
        "info_title": "Information",
        "confirm_stop_title": "Stop operation",
        "confirm_stop_msg": "Stop the running operation?\n\nCommitted progress stays in the manifest and can be resumed.",
        "confirm_stop_waiting_msg": "Stop waiting for the disk?\n\nThe run ends as paused (exit 3) without an "
                                    "auto-reconnect give-up; press Resume later to continue.",
        "confirm_close_title": "Quit",
        "confirm_close_msg": "An operation is running. Stop it and close the application once it has exited?",
        "confirm_close_stopping_msg": "The process is still stopping. Close the window now? "
                                      "The process may keep running in the background for a while.",
        "approve_dialog_title": "Consent for phase {phase}",
        "approve_dialog_text": "Phase “{phase}” requires the customer's explicit consent. Record it only if the "
                               "customer actually agreed. The consent is stored in the manifest with name and time.",
        "approve_by_label": "Who consented (name):",
        "approve_consent_check": "The customer explicitly agreed to this phase",
        "btn_confirm_approval": "Record consent",
        "btn_cancel": "Cancel",
        "select_source_title": "Select the mounted user home (source)",
        "select_dest_title": "Select the destination folder on the recovery disk",
        "select_job_title": "Select the job directory",
        "save_tech_title": "Save technician report",
        "err_job_required": "Enter the job directory.",
        "err_paths_required": "Source, destination and job directory are all required.",
        "err_invalid_number": "{field}: enter a positive number.",
        "err_invalid_limit": "{field}: enter a positive whole number or leave it empty.",
        "err_invalid_non_negative": "{field}: enter zero or a positive number.",
        "err_invalid_count": "{field}: enter a positive whole number.",
        "confirm_ack_title": "Automatic reconnect gave up",
        "confirm_ack_msg": "Automatic reconnect after disk loss gave up: the disk kept disconnecting, no new data "
                           "was copied, or it did not come back.\n\nRecommended: stop file-level copying and image "
                           "the disk with ddrescue and a mapfile, or hand it to a data recovery lab.\n\n"
                           "Continue anyway and re-enable automatic reconnect? The acknowledgement is recorded in "
                           "the job (--acknowledge-storage-loss).",
        "confirm_continue_msg": "Automatic reconnect after disk loss gave up: the disk kept disconnecting, no new "
                                "data was copied, or it did not come back.\n\nRecommended: stop file-level copying "
                                "and image the disk with ddrescue and a mapfile, or hand it to a data recovery "
                                "lab.\n\nContinue file-level copying anyway, without automatic reconnect?",
        "err_select_failed": "Select a failed file in the left table.",
        "err_select_both": "Select both a failed file and a candidate.",
        "err_job_load": "Cannot open the job:\n{error}",
        "err_busy": "An operation is already running.",
        "err_not_gated": "Phase {phase} does not require consent.",
        "confirm_fallback_title": "Recover from a duplicate",
        "confirm_fallback_msg": "Replace the failed file\n{path}\n\nwith the content of the candidate\n{candidate}?\n\n"
                                "Match: {evidence}\nContinue only if you verified it is the same file.",
        "saved_msg": "Saved: {path}",
    },
}

PHASE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "cs": {
        "visible-home": "Viditelná data (Plocha, Dokumenty, Stažené, Obrázky…); bez Library a mezipamětí",
        "hidden-home": "Skryté soubory a složky v domovské složce (.ssh, konfigurace…)",
        "app-data": "Vybraná data z ~/Library (Mail, Zprávy, Safari, Poznámky, Klíčenky, zálohy iOS) — vyžaduje souhlas",
        "applications": "Aplikace z /Applications a ~/Applications jako archiv — vyžaduje souhlas",
        "full-home": "Celá domovská složka včetně ~/Library, bez mezipamětí — vyžaduje souhlas",
        "all": "Všechny řádky manifestu (kopírování / ověření); jediný rozsah u úloh volume a restore",
    },
    "en": {
        "visible-home": "Visible data (Desktop, Documents, Downloads, Pictures…); no Library or caches",
        "hidden-home": "Hidden files and folders in the home (.ssh, configuration…)",
        "app-data": "Curated ~/Library data (Mail, Messages, Safari, Notes, Keychains, iOS backups) — needs consent",
        "applications": "Apps from /Applications and ~/Applications as an archive — needs consent",
        "full-home": "Whole home including ~/Library, without caches — needs consent",
        "all": "All manifest rows (copy / verify); the only scope of volume and restore jobs",
    },
}


def tr(key: str, lang: str = "cs", **kwargs: object) -> str:
    table = MESSAGES.get(lang, MESSAGES["cs"])
    template = table.get(key, MESSAGES["en"].get(key, key))
    return template.format(**kwargs) if kwargs else template


def format_waiting(view, lang: str = "cs", *, stoppable: bool = True) -> str:
    """Banner text for a ``state.WaitView``: phase, live countdown, attempt, reason."""
    key = "msg_waiting_cooldown" if view.phase == "cooldown" else "msg_waiting_wait"
    attempt = view.attempt if view.attempt is not None else "?"
    lines = [tr("msg_waiting", lang), tr(key, lang, remaining=format_eta(view.remaining, lang), attempt=attempt)]
    if view.reason:
        lines.append(tr("msg_pause_reason", lang, reason=view.reason))
    lines.append(tr("msg_waiting_stop_hint" if stoppable else "msg_waiting_external", lang))
    return "\n".join(lines)


def format_rate(rate_bytes_s: float) -> str:
    if rate_bytes_s <= 0:
        return "0.0 MB/s"
    if rate_bytes_s >= 1e9:
        return f"{rate_bytes_s / 1e9:.2f} GB/s"
    return f"{rate_bytes_s / 1e6:.2f} MB/s"


def format_eta(seconds: int | float | None, lang: str = "cs") -> str:
    if seconds is None or seconds < 0:
        return tr("eta_unknown", lang)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds_left = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m {seconds_left:02d}s"
    if minutes > 0:
        return f"{minutes}m {seconds_left:02d}s"
    return f"{seconds_left}s"
