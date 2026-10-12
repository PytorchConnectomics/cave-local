# Instructions an agent can actually use

The skills in `skills/` are the operating instructions shipped with this repo.
They explain which commands to use, what those commands prove, and what to do
when they fail. The commands do the mechanical work; the agent supplies the
dataset choices and checks the results.

For the first installation, give your agent [the handoff guide](handoff.md),
your checkout location and the locations of your image and segmentation. It
can read `skills/cave-local/SKILL.md` directly: skill installation is optional.
The installation below makes the same instructions discoverable in later chats.

| User request | Skill |
| --- | --- |
| “Help me with this CAVE installation” | `cave-local` chooses the relevant skill |
| “Install it”, “use my dataset”, “add another dataset” | `cave-local-setup` |
| “Start it”, “stop it”, “why is the viewer broken?” | `cave-local-operate` |
| “Back it up”, “check this backup”, “restore my work” | `cave-local-backup` |
| “Update it”, “refresh its passwords”, “connect from my laptop”, “remove this installation” | `cave-local-maintain` |

To install these in Codex, run the following from the repository root. It makes
symlinks and refuses to replace an existing skill. Keep the clone at this path,
or reinstall the symlinks after moving it.

```bash
python3 - <<'PY'
import os
from pathlib import Path
repo = Path.cwd().resolve()
assert (repo / 'bin/cave').is_file(), 'Run this from the CAVE repository root'
target = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'skills'
skills = sorted((repo / 'skills').glob('cave-local*'))
target.mkdir(parents=True, exist_ok=True)
for skill in skills:
    link = target / skill.name
    if link.exists() or link.is_symlink():
        raise SystemExit(f'Existing skill preserved: {link}; choose how to handle it before installing')
for skill in skills:
    (target / skill.name).symlink_to(skill, target_is_directory=True)
    print('Installed', skill.name)
PY
```

Start a new Codex chat with this repository as its workspace, then ask it to
use `$cave-local`. Agents that support other skill directories can load the
same `SKILL.md` files. An agent can also read them directly without installation.

Keep this repository available: the skills call its scripts and refer to its
documentation. Copying only the five `SKILL.md` files is insufficient. The
skills do not need our original installation or conversation history.

Skill formatting checks are useful, but successful formatting does not prove
that an agent can do the work. A release also needs independent runs where an
agent gets only the repo, a realistic request and an isolated test deployment.
The agent must record what worked, what failed and the resulting evidence. The
release report records those runs separately from unit tests and instructions.

The skills cover the supported operations. They do not promise automatic
conversion of every microscopy format, migration of edits to a different
segmentation, public authenticated hosting, or arbitrary database upgrades.
Those require an added implementation and a real test before the agent can
claim support.
