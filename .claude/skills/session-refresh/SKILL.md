---
name: session-refresh
description: |
  **SESSION END / HIGH TOKEN BUDGET** - Use when token budget >65% or before ending a session.

  Complete session refresh: Updates CLAUDE.md + PROJEKT.md → conditional restructure → Token-Budget hint.

  Trigger keywords: "session refresh", "update docs", "phase transition", "token budget high"

  Use when: (1) token budget >65%, (2) before session end, (3) phase transitions, (4) consolidating learnings.
  Conditional /project-doc-restructure (only when NEEDS_RESTRUCTURE). Danach Token-Budget manuell reduzieren (CLI Built-in).

  NOT needed at session start if previous session ended with /session-refresh.

allowed-tools: Read, Edit, Bash
---

# Session Refresh - Execution Instructions

Human-facing docs (FAQ, features, workflow): `references/README.md`

## Instructions for Claude

When this skill is triggered:

### 0. Context-Check (Token-Spar-Logik)

**BEFORE reading any files, check conversation context:**
- IF CLAUDE.md was already read in this session AND no structural changes expected → **Skip CLAUDE.md read**
- IF PROJEKT.md was already read in this session → **Skip PROJEKT.md read**
- ALWAYS run Health-Check (Bash, token-efficient)

This eliminates ~3.000-10.000 Token redundanter Reads.

### 1. Read Current State (nur wenn noetig)

- Read CLAUDE.md (project root) — **only if not already in context**
- Read PROJEKT.md (usually docs/PROJEKT.md) — **only if not already in context**
- Analyze conversation context for session learnings

### 1.5. Run PROJEKT Health-Check

```bash
${CLAUDE_PLUGIN_ROOT}/skills/session-refresh/bin/projekt-health-check.sh ./docs/PROJEKT.md
```

- Parses Task-Table (7-Column Schema)
- Validates: File existence, Status consistency, Dependencies
- Output includes `NEEDS_RESTRUCTURE` flag (see exit codes + stdout)
- Exit Codes: `0` = Healthy, `1` = Warnings, `2` = Critical, `3` = Error

**Bei Exit 2 (Critical):** Zeige Report, frage User ob vor Updates korrigieren.

### 2. Identify Updates

- What was learned this session? (patterns, decisions, insights)
- What tasks were completed or progressed?
- What blockers were resolved or discovered?
- What architectural decisions were made?

### 3. Make Targeted Edits

- Use Edit tool for specific sections
- Keep changes minimal, preserve structure
- Update: Task status, timestamps, Executive Summary

### 4. Show Compact Summary to User

Present changes concisely (max 5 Zeilen):
```
Session-Refresh: X Tasks aktualisiert.
CLAUDE.md: [sections changed]. PROJEKT.md: [tasks changed].
Restructure: [triggered/skipped (reason)].
→ Naechste Session: TASK-XXX ready.
```

Ask for confirmation or adjustments.

### 5. Conditional Restructure

**CHECK Health-Check output for NEEDS_RESTRUCTURE flag:**
- IF `NEEDS_RESTRUCTURE=true` (PROJEKT.md >10K chars OR >5 status changes OR structural issues):
  → Run `/project-doc-restructure`
- IF `NEEDS_RESTRUCTURE=false`:
  → **Skip restructure** (log: "Restructure uebersprungen - Health-Score OK")

**WICHTIG:** CLI Built-in zur Kontextreduktion kann NICHT programmatisch aufgerufen werden.
Weise User an: "Token-Budget manuell reduzieren (CLI Built-in)"

### 6. Commit + Push (automatisch, ohne Rueckfrage)

**Default: Automatisch committen + pushen. Nur stoppen wenn User explizit "nicht committen" sagt.**

#### KRITISCH: .gitignore ist UNANTASTBAR

- **NIEMALS** `git add -f`, `git add --force` oder sonstige Flags zum Umgehen von .gitignore verwenden
- Wenn `git add` einen Pfad wegen .gitignore ablehnt: **Das ist korrekt.** Der Pfad gehoert NICHT ins Repository.
- Stattdessen nur trackbare Dateien einzeln adden oder .gitignore-konforme Patterns nutzen
- Bei Unsicherheit: User fragen, NIEMALS force-adden

#### Workflow

```bash
# 1. Pruefen was ueberhaupt stageable ist (respektiert .gitignore)
git add -A

# 2. Falls git add fehlschlaegt: STOPPEN. Dateien einzeln adden die NICHT in .gitignore sind.
#    NIEMALS -f oder --force nutzen!

# 3. Commit + Push
git commit -m "$COMMIT_MSG" && git push
```

- Falls `git add` Fehler meldet ("ignored by .gitignore"): **NUR die nicht-ignorierten Dateien einzeln adden**
- Falls kein Remote konfiguriert: nur lokaler Commit
- Commit-Message: Deutsche Sprache, Format `[Typ]: Kurzbeschreibung`

### 7. Session-Handoff schreiben (automatisch, ohne Rueckfrage)

**Default: Automatisch erstellen. Nur ueberspringen wenn User explizit "kein Handoff" sagt.**

**Architektur (TASK-113, persist v1.8.0): Single-Write SSOT.** Der Handoff lebt an
GENAU EINEM Ort — kein lokales File + `cp` mehr (das war Master-Replica, driftanfaellig).

#### Schritt 1: Ziel bestimmen (Vault-Detection)

```bash
VAULT_ROOT=$("${CLAUDE_PLUGIN_ROOT}/scripts/detect-claude-vault.sh")
# Vault-Projekt → SSOT direkt im Vault. Externes Projekt → lokal (unveraendert).
```

- **Vault-Projekt** (`$VAULT_ROOT` non-empty UND `$VAULT_ROOT/_claude-pm/` existiert):
  Ziel = `$VAULT_ROOT/_claude-pm/SESSION-HANDOFF-YYYY-MM-DD-SNNN.md`. **KEIN lokales File.**
- **Externes Projekt** (`$VAULT_ROOT` empty):
  Ziel = `$PWD/docs/handoffs/SESSION-HANDOFF-YYYY-MM-DD-SNNN.md` (wie bisher).

#### Schritt 2: Handoff mit Write-Tool an das bestimmte Ziel schreiben

- **Dateiname-Pattern:** `SESSION-HANDOFF-{Datum}-S{Session-Nr}.md` (z.B. `SESSION-HANDOFF-2026-06-04-S248.md`)
- Template: `assets/session-handoff-template.md` (mit YAML-Frontmatter)
- **KRITISCH (TASK-113):** Frontmatter MUSS `pwd: {basename(PWD)}` enthalten. Der flache
  `_claude-pm/` Namensraum mischt ALLE Projekte (Session-Nummern sind pro Projekt) — der
  Loader filtert die Handoffs DIESES Projekts genau ueber `pwd:` (Fallback `project:`).
  Ohne diese Property findet die naechste Session ihren eigenen Handoff NICHT.
- Inhalt: YAML-Properties (fileClass, pwd, tasks, tags, outcome) + Erreichte Tasks, Naechste Session, Learnings
- **WICHTIG:** Jede Session erstellt eine NEUE Datei (kein Ueberschreiben). Handoffs akkumulieren.
- **PARALLELITAET:** NUR die Main-Session schreibt Handoff-Dateien. Subagents und parallele Tasks schreiben NICHT.
- **GUARDRAIL:** Vault-Projekte schreiben NIEMALS in einen anderen Vault als Claude (kein
  Fallback auf PKM). Das Detection-Skript ist 1Source-of-Truth fuer "ist PWD im Claude-Vault?".

### 7b. Vault-Properties aktualisieren (NUR Vault-Projekte)

**Nur wenn Schritt 1 ein Vault-Projekt ergab** (`$VAULT_ROOT` non-empty). Externe
Projekte ueberspringen 7b komplett (Graceful Degradation) — fuer sie endet der
Workflow nach Step 7 mit dem lokalen Handoff.

Der Handoff selbst wurde in **Step 7 bereits direkt im Vault** abgelegt (Single-Write,
kein `cp`). 7b aktualisiert nur noch die strukturierten Vault-Properties.

1. **Task-Status im Vault aktualisieren** (fuer jeden geaenderten Task):

```bash
obsidian.com property:set name="status" value="completed" type=text file="TASK-NNN-name"
```

2. **Projekt-Dokument aktualisieren** (Immediate Actions + Properties):

```bash
obsidian.com property:set name="phase" value="Phase X: ..." type=text file="PROJECT-xxx"
# Body: obsidian.com file/read holen → Write-Tool auf Pfad (Frontmatter erhalten)
```

3. **PROJECT-Doku Staleness-Check** (Warnung wenn `updated` > 30 Tage):

```bash
PROJECT_FILE="PROJECT-$(basename "$PWD")"
UPDATED=$(obsidian.com property:read name="updated" file="$PROJECT_FILE" 2>/dev/null | tr -d '\r')
if [[ -n "$UPDATED" ]]; then
  AGE_DAYS=$(( ( $(date +%s) - $(date -d "$UPDATED" +%s 2>/dev/null || echo 0) ) / 86400 ))
  [[ $AGE_DAYS -gt 30 ]] && echo "WARN: $PROJECT_FILE ist $AGE_DAYS Tage alt — Body + 'updated' aktualisieren"
fi
```

**WICHTIG:**
- 7b ist NUR fuer Claude-Vault-Projekte — externe Projekte enden nach Step 7.
- Bei Vault-Fehlern: Warnung loggen, nicht abbrechen (Graceful Degradation).
- **Vault-Selektion = CWD, nicht `vault=` (TASK-116, S247 verifiziert):** `vault=` ist
  ein No-Op und wird ignoriert. Die Commands oben landen im Claude-Vault, **weil PWD im
  Claude-Vault liegt** (Schritt-1-Detection bestaetigt). NIEMALS zum PKM-Vault cd-en.
- `file=` nutzt Wikilink-Aufloesung (Dateiname ohne Pfad/Extension).
- **Konsultation aelterer Handoffs:** Im Claude-Vault unter `_claude-pm/` via `vault-manager` Skill.

### 8. Final Report (Compact)

3-5 Zeilen Zusammenfassung:
```
Session-Refresh abgeschlossen. X Tasks aktualisiert.
Restructure: [status]. Token-Budget: manuell reduzieren.
→ Naechster Task: TASK-XXX | Tipp: /prioritize-tasks (bei >=3 pending)
```

**Kein Execution-Log** ausser bei Fehlern. Audit Trail nur in Task-File dokumentieren.
