# Discord Bot - Slash Command Structure

> Goal: agree on the final command tree before I build. Three columns to edit:
> - **Group / Command** - rename / nest / split / merge freely
> - **Permissions** - who can run it
> - **Decision** - mark `[x]` KEEP, `[?]` ASK CLAUDE TO ANSWER AFTER, `[~]` DEFER, `~~strike~~` REMOVE, or write a note `//like this`
>
> Anything you cross out won't ship in this phase.

---

## A. Current state (what the bot has TODAY)

| Group | Command | Args | Permissions |
|---|---|---|---|
| `/discord-admin` | `status` | - | Administrator |
| `/discord-admin` | `test-mode` | `value:{on/off}` | Administrator |
| `/discord-admin` | `classify-toggle` | - | Administrator |
| `/discord-admin` | `add-channel` | `channel, label, keywords?, forum_tags?` | Administrator |
| `/discord-admin` | `remove-channel` | `channel` | Administrator |
| `/discord-admin` | `add-team` | `team_role, name?, category?, coordinator_role?` | Administrator |
| `/discord-admin` | `remove-team` | `team_role` | Administrator |
| `/discord-admin` | `teams` | - | Administrator |
| `/discord-admin` | `notify-me` | `frequency:{daily/weekly/never}` | Administrator |
| `/team` | `add` | `user, team?` | Συντονιστής + team-role |
| `/team` | `remove` | `user, team?` | Συντονιστής + team-role |
| `/team` | `list` | `team?` | Συντονιστής + team-role |
| `/team` | `transfer` | `user, from_team, to_team` | Administrator |
| `/stats` | (no params) | - | anyone |

---

## B. Proposed new structure - EDIT THIS

> Three top-level groups: `/ai-assistant` (general), `/forum` (forum-specific), `/team` (coordinator self-service, unchanged).
> Mark each row with `[x]` / `[~]` / `~~strike~~` / `//notes`.

### B.1 `/ai-assistant` - general-purpose admin & stats

| Sub-command | Args | Permissions | Notes / Replaces |
|---|---|---|---|
| [x] `status` | - | Administrator | Replaces `/discord-admin status` |
| [x] `health` | - | Administrator | NEW - O1 from prior plan. Shows scheduler jobs, backup age, Graph sub expiry |
| [x] `stats` | `range:{24h/7d/30d/all}` | anyone | Replaces `/stats` + adds time-range select. Keeps "anyone can run". |
| [x] `test-mode` | `value:{on/off}` | Administrator | Replaces `/discord-admin test-mode` |
| [?] `classify-toggle` | - | Administrator | Replaces `/discord-admin classify-toggle` //what does it do? Maybe move under /forum? |
- _The should be an `/ai-assistant about` that shows version + brief description._ //

/Add a new `/archive` group with:
| [x] `submit` | `file, title?` //and all the various cli-valid arguments like protokol number, tags, etc | board member (role-gated) | NEW - C1. PDF attachment + optional title via Modal. Returns embed with πρωτόκολλο id + amend/cancel buttons.//Maybe do the amendment/confirmation/cancelation dialog open as a modal |
//I wanna expand the archive workflow to include a search function that returns archived files for the user to see. But we will talk about it later, just keep in mind.

### B.2 `/forum` - forum & channel routing

| Sub-command | Args | Permissions | Notes |
|---|---|---|---|
| [ ] `channels` | - | Administrator | NEW - replaces `/discord-admin add-channel` + `remove-channel` + (planned) `list-channels` + `update-channel`. Posts an interactive embed: a table of every configured channel (mention, label, keywords, applied tags) with **buttons**: `Add` (opens Modal), `Remove` (per-row), `Edit` (per-row, opens Modal). |
- _I want a `/forum channels` "details" button that drills into ONE channel's full config (tags + keywords + recent routed message count)_ //

### B.3 `/team` - coordinator self-service (UNCHANGED from today)
//I removed team-related commands (add/remove etc) from `/ai-assistant`, since the admin can just make new roles using Discords native interface and settings easily.
| Sub-command | Args | Permissions | Notes |
|---|---|---|---|
| [x] `add` | `user, team?` | Συντονιστής + team-role | unchanged |
| [x] `remove` | `user, team?` | Συντονιστής + team-role | unchanged |
| [x] `list` | `team?` | Συντονιστής + team-role | unchanged |

- _If you can have `/team list` upgraded to a richer embed (with team avatar / icon, member count badge, etc.), it would be great_ //

### B.4 Context menu (right-click) commands

| Type | Name | Permissions | Notes |
|---|---|---|---|
| [?] Message | `Αρχειοθέτηση συνημμένου` | board member (role-gated) | Right-click message with PDF → archive |
| [?] User | `Στατιστικά μέλους` | anyone | Right-click user → ephemeral stats embed |
//Not sure what these will do tbh 
---

## C. Things to drop / keep / discuss

| Currently exists | Decision | Notes |
|---|---|---|
| `/discord-admin` group itself | drop after migration | All 9 commands move to `/ai-assistant` per B.1 |
| `/stats` standalone | drop after migration | Becomes `/ai-assistant stats` |
| Legacy `!` command prefix | drop | Switch `command_prefix` to `commands.when_mentioned` |
| `/discord-admin events list` (planned N8) | drop (not in B.1) | You said use Discord's native events UI for browsing |
| `/discord-admin webhooks` (planned N10) | drop (not in B.1) | Diagnostic only - was nice-to-have //maybe integrate to `/ai-assistant status`? |
| `/forum tag-thread` (planned N5) | drop | Coordinator can apply tags via Discord UI |
| `/forum thread-info` (planned N6) | drop | Discord UI shows this |
| `/forum pin/unpin` (planned N7) | drop | Discord UI handles pinning |

---

## D. Once you've edited B.1 / B.2 / context menus

Save the file and ping me. I'll:
1. Lock the structure
2. Build the new groups, deprecate the old ones (with backward-compat shims for a couple of versions so muscle memory doesn't break)
3. Run the test suite, ship

_Last edited: 2026-05-26 - Claude_
