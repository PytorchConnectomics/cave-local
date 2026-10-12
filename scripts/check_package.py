"""Check shipped skill entry points and local Markdown references, without services."""
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    errors = []
    skill_files = sorted((root / 'skills').glob('*/SKILL.md'))
    if not skill_files:
        errors.append('No installable skill entry points found')
    for path in skill_files:
        text = path.read_text()
        frontmatter = re.match(r'\A---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
        if not frontmatter:
            errors.append(f'{path.relative_to(root)}: missing YAML frontmatter')
            continue
        try:
            metadata = yaml.safe_load(frontmatter.group(1))
        except yaml.YAMLError as exc:
            errors.append(f'{path.relative_to(root)}: invalid YAML: {exc}')
            continue
        if not isinstance(metadata, dict):
            errors.append(f'{path.relative_to(root)}: skill metadata must be a mapping')
            continue
        name = metadata.get('name')
        if not isinstance(name, str) or name != path.parent.name or not re.fullmatch(r'[a-z0-9-]{1,64}', name):
            errors.append(f'{path.relative_to(root)}: skill name must match its folder')
        description = metadata.get('description')
        if not isinstance(description, str) or not description.strip():
            errors.append(f'{path.relative_to(root)}: missing skill description')

    markdown = sorted(set(root.glob('*.md')) | set((root / 'docs').rglob('*.md'))
                      | set((root / 'skills').rglob('*.md')))
    checked_links = 0
    for path in markdown:
        text = re.sub(r'^```[^\n]*\n.*?^```[^\n]*$', '', path.read_text(),
                      flags=re.MULTILINE | re.DOTALL)
        for match in re.finditer(r'\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+"[^"\n]*")?\)', text):
            target = match.group(1).strip('<>')
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = (path.parent / unquote(parsed.path)).resolve()
            if not destination.is_relative_to(root.resolve()) or not destination.exists():
                errors.append(f'{path.relative_to(root)}: missing local reference {target}')
            checked_links += 1
    if errors:
        raise ValueError('\n'.join(errors))
    return {'skills': len(skill_files), 'markdown_files': len(markdown),
            'local_references': checked_links, 'passed': True}


if __name__ == '__main__':
    print(json.dumps(check(), indent=2))
