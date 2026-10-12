---
name: cave-local
description: Set up or look after the CAVE local stack shipped by this repository. Select the focused setup, operation, backup or maintenance skill for the user's request.
---

Use the clone containing this skill and `bin/cave`. Resolve an installed skill
symlink to find its repository; work from that root. Read the repo README and
only the skill relevant to the current job:

- Install CAVE or load a dataset: [cave-local-setup](../cave-local-setup/SKILL.md).
- Start/stop it or investigate failures: [cave-local-operate](../cave-local-operate/SKILL.md).
- Back up, inspect a backup, or recover work: [cave-local-backup](../cave-local-backup/SKILL.md).
- Update, refresh generated credentials, connect from a laptop, or remove one installation: [cave-local-maintain](../cave-local-maintain/SKILL.md).

Identify the requested deployment from `deployment.json`, including graph,
URL and project name. Commands affect only its `cave-pkg-*` project. Never
operate a different existing application to make room for it.

Use supplied dataset choices and existing authorization. Missing credentials,
source geometry or intended deletion targets require user input; routine
implementation choices do not. A supported command must produce its stated
evidence before declaring success. Report actual results and any remaining
failure plainly. The repository is sufficient; our original chat and original
server's hidden files are not prerequisites.
