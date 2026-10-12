"""Local deployment adapter; no changes to upstream graph algorithms.

Loopback-only gateway supplies a single local editor identity. For a shared
deployment replace this with MiddleAuth before opening the gateway externally.
"""
import os, json, time
from flask import g, request, jsonify
service=os.environ.get('CAVE_SERVICE','pcg')
release_database_sessions=None
if service in ('annotation','materialize'):
    from database_connections import install as install_database_connections
    release_database_sessions=install_database_connections()
    from segmentation_metadata import install_dynamic
    install_dynamic()
if service=='pcg':
    from pychunkedgraph.app import create_app
    from pychunkedgraph.app.segmentation import common
    def publish_edit(table_id,user_id,result,is_priority=True,remesh=True):
        import redis
        payload={'graph':table_id,'user_id':str(user_id),'operation_id':int(result.operation_id),'roots':[str(x) for x in result.new_root_ids],'time':time.time()}
        redis.Redis(host='redis').rpush('cave:edits',json.dumps(payload))
    common.publish_edit=publish_edit
    from source_label_policy import install
    install(common)
    old_info=common.handle_info
    def info(table_id):
        resp=old_info(table_id);v=resp.get_json();v['data_dir']=request.host_url.rstrip('/')+'/data/atomic';v['mesh']='../meshes';v['sharded_mesh']=False
        if os.environ.get('CAVE_UI_SANDBOX')=='1': v['proofreading_ui']=True
        return jsonify(v)
    common.handle_info=info
    from pychunkedgraph.app.meshing import common as meshes
    from local_mesh import manifest
    meshes.handle_get_manifest=manifest
elif service=='info':
    from annotationinfoservice import create_app
elif service=='schema':
    from emannotationschemas.app import create_app
elif service=='annotation':
    from annotationengine import create_app
elif service=='materialize':
    # Some upstream task decorators read config while importing the module.
    # Supply a configured context before importing those decorators.
    from flask import Flask
    from materializationengine.config import BaseConfig
    bootstrap=Flask('materialization-config-bootstrap')
    bootstrap.config.from_object(BaseConfig)
    bootstrap.config.from_pyfile('/workspace/config/common.py')
    with bootstrap.app_context():
        from materializationengine.app import create_app
app=create_app()
if release_database_sessions:
    app.teardown_request(release_database_sessions)
if service=='pcg' and os.environ.get('CAVE_UI_SANDBOX')=='1':
    # TLS can terminate at a shareable test-link proxy. Host is already forwarded
    # by nginx; use its forwarded scheme so volume URLs stay on the HTTPS origin.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app=ProxyFix(app.wsgi_app,x_for=0,x_proto=1,x_host=0,x_port=0,x_prefix=0)
    from proofreading_ui import register_proofreading_ui
    register_proofreading_ui(app)
if service=='materialize':
    from materializationengine.celery_worker import create_celery
    celery=create_celery(app)
    from segmentation_metadata import install_materialization
    with app.app_context():
        install_materialization()
    from celery.signals import task_postrun
    task_postrun.connect(release_database_sessions, weak=False,
                        dispatch_uid='cave-local-release-database-sessions')
@app.before_request
def local_identity():
    g.auth_user={'id':1,'admin':True,'service_account':False,'permissions':[]}
if service=='info':
    @app.after_request
    def public_addresses(response):
        if response.is_json:
            value=response.get_json()
            origin=request.host_url.rstrip('/')
            text=json.dumps(value).replace('http://gateway',origin).replace(os.environ['CAVE_LOCAL_URL'],origin)
            response.set_data(text)
        return response
