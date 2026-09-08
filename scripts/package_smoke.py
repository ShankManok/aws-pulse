"""Import each synthesized Python Lambda using only its deployment assets."""
import json
import os
from pathlib import Path
import subprocess
import sys

assembly = Path(sys.argv[1] if len(sys.argv) > 1 else 'infra/cdk.out').resolve()
count = 0
for template_path in sorted(assembly.glob('*.template.json')):
    resources = json.loads(template_path.read_text())['Resources']
    for logical_id, resource in resources.items():
        props = resource.get('Properties', {})
        if resource['Type'] != 'AWS::Lambda::Function' or props.get('Runtime') != 'python3.12':
            continue
        paths = [str(assembly / ('asset.' + props['Code']['S3Key'].removesuffix('.zip')))]
        for layer in props.get('Layers', []):
            key = resources[layer['Ref']]['Properties']['Content']['S3Key']
            paths.append(str(assembly / ('asset.' + key.removesuffix('.zip')) / 'python'))
        assert all(Path(path).is_dir() for path in paths), (logical_id, paths)
        module, handler = props['Handler'].rsplit('.', 1)
        program = f'import importlib; assert callable(getattr(importlib.import_module({module!r}), {handler!r}))'
        env = {**os.environ, 'AWS_DEFAULT_REGION':'us-east-1', 'AWS_ACCESS_KEY_ID':'testing', 'AWS_SECRET_ACCESS_KEY':'testing',
               'AWS_SESSION_TOKEN':'testing', 'AWS_EC2_METADATA_DISABLED':'true', 'PYTHONPATH':os.pathsep.join(paths)}
        subprocess.run([sys.executable, '-S', '-c', program], cwd=assembly, env=env, check=True, capture_output=True, text=True)
        count += 1
print(f'{count} packaged Python Lambda handlers imported successfully')
assert count > 0
