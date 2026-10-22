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
        "tab_setup": "Příprava",
        "tab_rescue": "Záchrana",
        "source_path": "Zdroj (jen ke čtení):",
        "source_hint": "Např. /Volumes/Macintosh HD - Data/Users/zakaznik — zdroj se nikdy nemění",
        "dest_path": "Cíl:",
        "dest_hint": "Např. /Volumes/RecoverySSD/Zakaznik/user-data — na záchranném disku, mimo zdroj",
        "job_path": "Pracovní složka úlohy:",
        "job_hint": "Sem se ukládá stav úlohy, např. /Volumes/RecoverySSD/Zakaznik/.rescue — mimo zdroj",
        "browse": "Vybrat…",
        "profile": "Profil:",
        "profile_home": "Domovská složka zákazníka (doporučeno)",
        "profile_volume": "Disk / složka (volume)",
        "profile_restore": "Obnova na nový disk (restore)",
        "volume_excludes": "Vyloučené vzory (čárkou oddělené, jen volume):",
        "job_loaded_note": "Úloha je otevřená, cesty a profil už nejde měnit. Pro jinou úlohu ji zavřete.",
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
        "btn_guide_approve": "Zaznamenat souhlas: {phase}…",
        "select_phase": "Fáze",
        "btn_approve": "Zaznamenat souhlas…",
        "approved_badge": "Schváleno ({by})",
        "no_approval_needed": "Bez schválení",
        "btn_scan": "Skenovat",
        "btn_copy": "Kopírovat",
        "btn_stop": "Zastavit",
        "btn_stop_waiting": "Zastavit čekání",
        "btn_resume": "Pokračovat",
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
        "state_idle": "Připraveno",
        "state_running": "Probíhá",
        "state_waiting": "Čeká na disk",
        "state_stopping": "Zastavuji…",
        "state_stopped": "Zastaveno",
        "state_paused_storage": "Pozastaveno",
        "state_failed": "Chyba",
        "state_done": "Dokončeno",
        "msg_stopping": "Počkejte, až proces skutečně skončí; do té doby nelze spustit nic dalšího.",
        "msg_stopped": "Hotová práce zůstává uložená, dá se na ni navázat.",
        "msg_paused_storage": "Disk se odpojil nebo došlo místo: znovu připojte původní disk (nebo uvolněte "
                              "místo na cíli) a stiskněte Pokračovat.",
        "msg_pause_reason": "Důvod: {reason}",
        "msg_waiting": "Pokud se disk nevrátí sám, připojte ho znovu. Kopírování pak pokračuje samo.",
        "msg_waiting_external": "Čeká jiný proces (např. v Terminálu); tato konzole ho nemůže zastavit.",
        "msg_storage_escalation": "Disk se opakovaně odpojuje nebo se nevrátil. Další kopírování ho zatěžuje; "
                                  "zvažte image disku (ddrescue).",
        "storage_events_summary": "Odpojení disku: {losses} · automaticky obnoveno: {reconnects}",
        "msg_failed": "Příkaz {command} skončil chybou (kód {code}). Podrobnosti jsou v záznamu činnosti.",
        "msg_preflight_failed": "Operace nebyla spuštěna. Výsledek kontroly je na kartě Příprava.",
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
        "confirm_stop_msg": "Zastavit probíhající operaci?\n\nHotová práce zůstane uložená.",
        "confirm_stop_waiting_msg": "Zastavit čekání na disk?\n\nÚloha zůstane pozastavená, později stačí "
                                    "Pokračovat.",
        "confirm_close_title": "Ukončit aplikaci",
        "confirm_close_msg": "Probíhá operace. Zastavit ji a po jejím ukončení zavřít aplikaci?",
        "confirm_close_stopping_msg": "Proces se stále ukončuje. Zavřít okno hned? "
                                      "Proces může ještě chvíli doběhnout na pozadí.",
        "approve_dialog_title": "Souhlas pro fázi {phase}",
        "approve_dialog_text": "Fáze „{phase}“ vyžaduje souhlas zákazníka. Zaznamenejte ho, jen pokud zákazník "
                               "opravdu souhlasil. Uloží se jméno a čas.",
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
        "confirm_ack_title": "Disk se opakovaně odpojuje",
        "confirm_ack_msg": "Disk se opakovaně odpojuje nebo se nevrátil a automatické obnovení se zastavilo. "
                           "Další kopírování disk zatěžuje; zvažte image disku (ddrescue).\n\nPokračovat a automatické obnovení znovu zapnout?",
        "confirm_continue_msg": "Disk se opakovaně odpojuje nebo se nevrátil a automatické obnovení se zastavilo. "
                                "Další kopírování disk zatěžuje; zvažte image disku (ddrescue).\n\nPokračovat bez automatického obnovení?",
        "err_select_failed": "Vyberte selhaný soubor v levé tabulce.",
        "err_select_both": "Vyberte selhaný soubor i kandidáta.",
        "err_job_load": "Úlohu nelze otevřít:\n{error}",
        "err_busy": "Operace už běží.",
        "err_not_gated": "Fáze {phase} souhlas nevyžaduje.",
        "confirm_fallback_title": "Záchrana z duplikátu",
        "confirm_fallback_msg": "Nahradit selhaný soubor\n{path}\n\nobsahem kandidáta\n{candidate}?\n\n"
                                "Shoda: {evidence}\nPokračujte jen, pokud jste ověřili, že jde o stejný soubor.",
        "saved_msg": "Uloženo: {path}",
        "tab_results": "Výsledky",
        "btn_resume_anyway": "Přesto pokračovat…",
        "do_scan": "Skenovat: {phase}",
        "do_copy": "Kopírovat: {phase}",
        "do_resume": "Pokračovat: {phase}",
        "do_verify": "Ověřit integritu",
        "do_report": "Vytvořit technickou zprávu",
        "do_customer_report": "Vytvořit protokol pro zákazníka",
        "do_status": "Zobrazit stav úlohy",
        "detail": "Detail",
        "advanced": "Pokročilé",
        "status_no_job": "Žádná otevřená úloha",
        "status_no_job_body": "Na kartě Příprava založte novou úlohu, nebo otevřete existující.",
        "status_ready": "Připraveno",
        "status_needs_consent": "Čeká na souhlas zákazníka",
        "status_copy_complete": "Kopírování dokončeno",
        "status_done": "Hotovo",
        "status_done_body": "Data jsou zachráněná a protokoly vytvořené.",
        "status_needs_review": "Potřeba ruční kontroly",
        "status_needs_review_body": "Některé soubory se nepodařilo zachránit; projděte je v technické zprávě.",
        "status_integrity_failed": "Ověření cíle selhalo",
        "status_integrity_failed_body": "Znovu připojte zdroj, zopakujte kopírování a pak znovu ověřte integritu.",
        "status_restore_check_body": "Ověřte opakovaným během, že už se nic nekopíruje, a vytvořte technickou zprávu.",
        "status_failed": "Operace selhala",
        "status_preflight_failed": "Kontrola prostředí selhala",
        "status_stopped": "Zastaveno",
        "status_stopping": "Zastavuji…",
        "status_paused": "Pozastaveno: úložiště nedostupné",
        "status_escalation": "Disk se opakovaně odpojuje",
        "status_cooldown": "Chladnutí disku — {remaining} (pokus {attempt})",
        "status_waiting": "Čekám na disk — {remaining} (pokus {attempt})",
        "status_running_body": "Průběh se ukládá, po přerušení se pokračuje tam, kde se skončilo.",
        "status_run_preflight": "Kontroluji prostředí",
        "status_run_init": "Zakládám úlohu",
        "status_run_scan": "Skenuji",
        "status_run_copy": "Kopíruji",
        "status_run_verify": "Ověřuji integritu",
        "status_run_approve": "Zaznamenávám souhlas",
        "status_run_report": "Vytvářím protokol",
        "status_run_other": "Probíhá operace",
        "status_next_scan": "Další krok: skenování ({phase}).",
        "status_next_copy": "Další krok: kopírování ({phase}).",
        "status_next_resume": "Další krok: pokračovat v kopírování ({phase}).",
        "status_next_verify": "Další krok: ověřit zkopírovaná data.",
        "status_next_reports": "Zbývá vytvořit protokoly.",
        "status_next_status": "Postup je v Detailu.",
        "status_next_approve": "Je potřeba souhlas zákazníka ({phase}).",
        "status_next_none": "Postup je v Detailu.",
        "reconnect_hint_on": "Automatické obnovení po odpojení: zapnuto",
        "reconnect_hint_off": "Automatické obnovení po odpojení: vypnuto",
        "summary_files": "{done} / {total} souborů",
        "summary_eta": "zbývá {eta}",
        "status_cooldown_body": "Disk se odpojil a chladne. Potom se zkusí znovu připojit a kopírování pokračuje.",
    },
    "en": {
        "app_title": "macOS Data Rescue — technician console",
        "tab_setup": "Setup",
        "tab_rescue": "Rescue",
        "source_path": "Source (read-only):",
        "source_hint": "E.g. /Volumes/Macintosh HD - Data/Users/customer — the source is never modified",
        "dest_path": "Destination:",
        "dest_hint": "E.g. /Volumes/RecoverySSD/Customer/user-data — on the recovery disk, outside the source",
        "job_path": "Job directory:",
        "job_hint": "Job state is stored here, e.g. /Volumes/RecoverySSD/Customer/.rescue — outside the source",
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
        "btn_guide_approve": "Record consent: {phase}…",
        "select_phase": "Phase",
        "btn_approve": "Record consent…",
        "approved_badge": "Approved ({by})",
        "no_approval_needed": "No approval needed",
        "btn_scan": "Scan",
        "btn_copy": "Copy",
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
        "state_idle": "Ready",
        "state_running": "Running",
        "state_waiting": "Waiting for disk",
        "state_stopping": "Stopping…",
        "state_stopped": "Stopped",
        "state_paused_storage": "Paused",
        "state_failed": "Failed",
        "state_done": "Done",
        "msg_stopping": "Wait until the process has really exited; nothing else can start until then.",
        "msg_stopped": "Finished work stays saved and can be continued.",
        "msg_paused_storage": "A disk disconnected or ran out of space: reconnect the original disk (or free "
                              "space on the destination), then press Resume.",
        "msg_pause_reason": "Reason: {reason}",
        "msg_waiting": "If the disk does not come back on its own, reconnect it; copying then continues by itself.",
        "msg_waiting_external": "Another process (e.g. in Terminal) is waiting; this console cannot stop it.",
        "msg_storage_escalation": "The disk keeps disconnecting or did not come back. More copying stresses it; "
                                  "consider imaging the disk (ddrescue).",
        "storage_events_summary": "Disk disconnections: {losses} · reconnected automatically: {reconnects}",
        "msg_failed": "Command {command} failed (exit {code}). See the activity log for details.",
        "msg_preflight_failed": "The operation was not started. The check results are on the Setup tab.",
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
        "confirm_stop_msg": "Stop the running operation?\n\nFinished work stays saved.",
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
        "confirm_ack_title": "The disk keeps disconnecting",
        "confirm_ack_msg": "The disk keeps disconnecting or did not come back, so automatic reconnect stopped. "
                           "More copying stresses the disk; consider imaging it (ddrescue).\n\nContinue and turn automatic reconnect back on?",
        "confirm_continue_msg": "The disk keeps disconnecting or did not come back, so automatic reconnect stopped. "
                                "More copying stresses the disk; consider imaging it (ddrescue).\n\nContinue without automatic reconnect?",
        "err_select_failed": "Select a failed file in the left table.",
        "err_select_both": "Select both a failed file and a candidate.",
        "err_job_load": "Cannot open the job:\n{error}",
        "err_busy": "An operation is already running.",
        "err_not_gated": "Phase {phase} does not require consent.",
        "confirm_fallback_title": "Recover from a duplicate",
        "confirm_fallback_msg": "Replace the failed file\n{path}\n\nwith the content of the candidate\n{candidate}?\n\n"
                                "Match: {evidence}\nContinue only if you verified it is the same file.",
        "saved_msg": "Saved: {path}",
        "tab_results": "Results",
        "btn_resume_anyway": "Continue anyway…",
        "do_scan": "Scan: {phase}",
        "do_copy": "Copy: {phase}",
        "do_resume": "Resume: {phase}",
        "do_verify": "Verify integrity",
        "do_report": "Create technician report",
        "do_customer_report": "Create customer report",
        "do_status": "Show job status",
        "detail": "Details",
        "advanced": "Advanced",
        "status_no_job": "No job open",
        "status_no_job_body": "Create a new job on the Setup tab, or open an existing one.",
        "status_ready": "Ready",
        "status_needs_consent": "Waiting for customer consent",
        "status_copy_complete": "Copying complete",
        "status_done": "Done",
        "status_done_body": "The data is rescued and the reports are created.",
        "status_needs_review": "Manual review needed",
        "status_needs_review_body": "Some files could not be rescued; review them in the technician report.",
        "status_integrity_failed": "Destination verification failed",
        "status_integrity_failed_body": "Reconnect the source, repeat the copy, then verify integrity again.",
        "status_restore_check_body": "Confirm with another run that nothing is copied any more, then create the technician report.",
        "status_failed": "Operation failed",
        "status_preflight_failed": "Preflight failed",
        "status_stopped": "Stopped",
        "status_stopping": "Stopping…",
        "status_paused": "Paused: storage unavailable",
        "status_escalation": "The disk keeps disconnecting",
        "status_cooldown": "Disk cooldown — {remaining} (attempt {attempt})",
        "status_waiting": "Waiting for the disk — {remaining} (attempt {attempt})",
        "status_running_body": "Progress is saved; after an interruption it continues where it stopped.",
        "status_run_preflight": "Checking the environment",
        "status_run_init": "Creating the job",
        "status_run_scan": "Scanning",
        "status_run_copy": "Copying",
        "status_run_verify": "Verifying integrity",
        "status_run_approve": "Recording consent",
        "status_run_report": "Creating report",
        "status_run_other": "Operation running",
        "status_next_scan": "Next: scan {phase}.",
        "status_next_copy": "Next: copy {phase}.",
        "status_next_resume": "Next: resume copying ({phase}).",
        "status_next_verify": "Next: verify the copied data.",
        "status_next_reports": "The reports are still missing.",
        "status_next_status": "The steps are in Details.",
        "status_next_approve": "Customer consent is needed ({phase}).",
        "status_next_none": "The steps are in Details.",
        "reconnect_hint_on": "Automatic reconnect after disk loss: on",
        "reconnect_hint_off": "Automatic reconnect after disk loss: off",
        "summary_files": "{done} / {total} files",
        "summary_eta": "{eta} left",
        "status_cooldown_body": "The disk disconnected and is cooling down. Then it is reconnected and copying continues.",
    },
}

# short names for headlines and sentences; the CLI phase ids stay in Details and the selector
PHASE_LABELS: dict[str, dict[str, str]] = {
    "cs": {"visible-home": "viditelná data", "hidden-home": "skrytá data", "app-data": "data aplikací",
           "applications": "aplikace", "full-home": "celá domovská složka", "all": "vše"},
    "en": {"visible-home": "visible data", "hidden-home": "hidden data", "app-data": "app data",
           "applications": "applications", "full-home": "whole home", "all": "everything"},
}


def phase_label(phase: str, lang: str = "cs") -> str:
    return PHASE_LABELS.get(lang, PHASE_LABELS["cs"]).get(phase, phase)


PHASE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "cs": {
        "visible-home": "Viditelná data (Plocha, Dokumenty, Stažené, Obrázky…); bez Library a mezipamětí",
        "hidden-home": "Skryté soubory a složky v domovské složce (.ssh, konfigurace…)",
        "app-data": "Vybraná data z ~/Library (Mail, Zprávy, Safari, Poznámky, Klíčenky, zálohy iOS) — vyžaduje souhlas",
        "applications": "Aplikace z /Applications a ~/Applications jako archiv — vyžaduje souhlas",
        "full-home": "Celá domovská složka včetně ~/Library, bez mezipamětí — vyžaduje souhlas",
        "all": "Všechny naskenované soubory (kopírování / ověření); u úloh volume a restore jediný rozsah",
    },
    "en": {
        "visible-home": "Visible data (Desktop, Documents, Downloads, Pictures…); no Library or caches",
        "hidden-home": "Hidden files and folders in the home (.ssh, configuration…)",
        "app-data": "Curated ~/Library data (Mail, Messages, Safari, Notes, Keychains, iOS backups) — needs consent",
        "applications": "Apps from /Applications and ~/Applications as an archive — needs consent",
        "full-home": "Whole home including ~/Library, without caches — needs consent",
        "all": "All scanned files (copy / verify); the only scope of volume and restore jobs",
    },
}


def tr(key: str, lang: str = "cs", **kwargs: object) -> str:
    table = MESSAGES.get(lang, MESSAGES["cs"])
    template = table.get(key, MESSAGES["en"].get(key, key))
    return template.format(**kwargs) if kwargs else template


def format_count(count: int, lang: str = "cs") -> str:
    """Thousands separators: ``1,234`` in English, ``1 234`` (no-break space) in Czech."""
    text = f"{count:,}"
    return text.replace(",", "\u00a0") if lang == "cs" else text


def localize_decimal(text: str, lang: str = "cs") -> str:
    """Decimal comma for Czech in an already formatted number such as ``19.3 MiB``."""
    return text.replace(".", ",") if lang == "cs" else text


def format_percent(fraction: float, lang: str = "cs") -> str:
    """Whole percent, rounded down so an unfinished job never shows 100.

    The epsilon keeps exact fractions whole (0.57 * 100 is 56.99999999999999);
    the cap keeps a fraction just below 1 from reaching 100 through it.
    """
    value = max(0, min(100, int(fraction * 100 + 1e-9)))
    if fraction < 1:
        value = min(value, 99)
    return f"{value}\u00a0%" if lang == "cs" else f"{value}%"


def elide_middle(text: str, max_width: int, measure) -> str:
    """Shorten ``text`` to ``max_width`` (in ``measure`` units) with an ellipsis in the middle.

    Both ends of a path stay readable: the top folder and the file name.
    """
    if max_width <= 0 or measure(text) <= max_width:
        return text
    best, low, high = "…", 0, len(text) - 1
    while low <= high:
        keep = (low + high) // 2
        head, tail = (keep + 1) // 2, keep // 2
        candidate = text[:head] + "…" + (text[-tail:] if tail else "")
        if measure(candidate) <= max_width:
            best, low = candidate, keep + 1
        else:
            high = keep - 1
    return best


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
