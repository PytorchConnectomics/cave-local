import os
from pychunkedgraph.app.config import BaseConfig
for _key in dir(BaseConfig):
    if _key.isupper(): globals()[_key] = getattr(BaseConfig, _key)
DEBUG = False
LOGGING_LEVEL = 30
AUTH_DISABLED = True
AUTH_SERVICE_NAMESPACE = "pychunkedgraph"
PCG_GRAPH_IDS = [os.environ['PCG_GRAPH_IDS']]
USE_REDIS_JOBS = False
VIRTUAL_TABLES = {}
