import os
from pathlib import Path

script_dir = Path(r'C:\Users\sagnik\Desktop\apex v-3\Apex100-apex100-2\artifacts\api-server\scripts')
snippet = """import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))
"""

for f in script_dir.glob('*.py'):
    content = f.read_text(encoding='utf-8')
    if 'sys.path.insert' not in content:
        f.write_text(snippet + '\n' + content, encoding='utf-8')
print("Done")
