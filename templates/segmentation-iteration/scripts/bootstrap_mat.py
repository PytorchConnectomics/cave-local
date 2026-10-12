import json
import os
from pathlib import Path
from sqlalchemy import create_engine
from dynamicannotationdb.models import Base

spec = json.loads(Path('/workspace/deployment.json').read_text())
uri = 'postgresql://cave:' + os.environ['POSTGRES_PASSWORD'] + '@postgres/' + spec['aligned_volume']
Base.metadata.create_all(create_engine(uri))
print('Aligned-volume annotation metadata initialized')
