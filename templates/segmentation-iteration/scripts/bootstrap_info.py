import json
from pathlib import Path
from annotationinfoservice import create_app, db
from annotationinfoservice.datasets.models import AlignedVolume, DataStack

spec = json.loads(Path('/workspace/deployment.json').read_text())
app = create_app()
with app.app_context():
    db.create_all()
    av = db.session.query(AlignedVolume).filter_by(name=spec['aligned_volume']).first()
    if av is None:
        av = AlignedVolume(name=spec['aligned_volume'], display_name=spec.get('image_display_name', spec['graph_id'] + ' image'),
            description=spec.get('image_description', 'Immutable source image; physical geometry preserved from metadata.'),
            image_source='zarr://' + spec['local_url'] + '/image/')
        db.session.add(av)
        db.session.flush()
    ds = db.session.query(DataStack).filter_by(name=spec['datastack']).first()
    if ds is None:
        x, y, z = spec['resolution_nm_xyz']
        ds = DataStack(name=spec['datastack'], display_name=spec.get('display_name', spec['graph_id']),
            aligned_volume_id=av.id,
            segmentation_source='graphene://' + spec['local_url'] + '/segmentation/table/' + spec['graph_id'],
            viewer_site=spec['local_url'] + '/viewer/', local_server='http://gateway',
            description='One immutable segmentation iteration. See ingestion provenance for the supervoxel policy.',
            viewer_resolution_x=x, viewer_resolution_y=y, viewer_resolution_z=z)
        db.session.add(ds)
    av.image_source = 'zarr://' + spec['local_url'] + '/image/'
    ds.segmentation_source = 'graphene://' + spec['local_url'] + '/segmentation/table/' + spec['graph_id']
    ds.viewer_site = spec['local_url'] + '/viewer/'
    ds.description = ('Source labels are immutable supervoxels; graph-chunk fragments of one label are joined by uncuttable edges.'
                      if spec.get('atom_policy', 'source_labels') == 'source_labels'
                      else 'Label-preserving connected spatial atoms for one immutable iteration. See ingestion provenance.')
    db.session.commit()
    print('Registered', spec['datastack'])
