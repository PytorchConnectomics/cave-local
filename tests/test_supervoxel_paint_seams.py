"""Seam passes: one refinement per chosen piece, a per-lineage cap, and a resumable deadline message."""
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip('flask')
pytest.importorskip('pychunkedgraph')
from flask import g

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import supervoxel_paint_service as paint_service
from supervoxel_paint import PAINT_LIMITS, PaintError
from test_supervoxel_paint_service import app_for, body, specimen


def seam_graph(pairs):
    from pychunkedgraph.graph.edges import Edges
    pairs = np.asarray(pairs, np.uint64).reshape(-1, 2)
    atoms = np.unique(pairs)
    edges = Edges(pairs[:, 0], pairs[:, 1], affinities=np.full(len(pairs), np.inf, np.float32))
    return SimpleNamespace(get_subgraph=lambda *a, **k: ({1: SimpleNamespace(supervoxels=atoms)}, [edges]))


def context():
    limits = {**PAINT_LIMITS, 'enabled': True}
    return paint_service.PaintContext(specimen(), limits, time.monotonic() + 60, np.array([[1, 1, 1]]),
                                      np.array([[9, 1, 1]]), 0, set(), set(), np.array([2, 2, 2]), None)


def contacts_from(table, monkeypatch):
    """table[(target, neighbour)] lists the target's XYZ voxels touching the neighbour."""
    monkeypatch.setattr(paint_service, '_bounds', lambda context, ids: {int(a): None for a in ids})
    calls = []

    def contacts(context, edge, bounds, budget):
        calls.append(tuple(edge))
        return np.asarray(table.get(tuple(edge), []), np.int64).reshape(-1, 3)
    monkeypatch.setattr(paint_service, '_edge_contacts', contacts)
    return calls


def test_paths_through_one_piece_share_a_single_refinement(monkeypatch):
    # Reds 1 and 2 both reach blue 4 through unpainted piece 3: one target, seeds from both red neighbours.
    calls = contacts_from({(3, 1): [[1, 0, 0]], (3, 2): [[2, 0, 0]], (3, 4): [[5, 0, 0]]}, monkeypatch)
    found = paint_service._seam_targets(context(), seam_graph([[1, 3], [2, 3], [3, 4]]), 100, [1, 2], [4], {})
    assert [atom for atom, *_ in found] == [3]
    _, red, blue, _ = found[0]
    assert red.tolist() == [[1, 0, 0], [2, 0, 0]] and blue.tolist() == [[5, 0, 0]]
    assert calls.count((3, 4)) == 1  # contact reads are cached across paths


def test_disjoint_seam_paths_are_all_returned_in_one_pass(monkeypatch):
    contacts_from({(1, 2): [[1, 0, 0]], (3, 4): [[3, 0, 0]]}, monkeypatch)
    grouped = {1: {'source': np.array([[0, 0, 0]])}, 3: {'source': np.array([[2, 0, 0]])}}
    found = paint_service._seam_targets(context(), seam_graph([[1, 2], [3, 4]]), 100, [1, 3], [2, 4], grouped)
    assert [atom for atom, *_ in found] == [1, 3]
    assert all(len(red) and len(blue) for _, red, blue, _ in found)


def test_unseedable_path_is_returned_last_with_an_empty_side(monkeypatch):
    # Path 1-2 has no contacts at all; path 5-6 is fine.
    contacts_from({(5, 6): [[5, 0, 0]]}, monkeypatch)
    grouped = {5: {'source': np.array([[4, 0, 0]])}}
    found = paint_service._seam_targets(context(), seam_graph([[1, 2], [5, 6]]), 100, [1, 5], [2, 6], grouped)
    assert [atom for atom, *_ in found][0] == 5
    _, red, blue, _ = found[-1]
    assert not (len(red) and len(blue))


def test_no_seam_paths_returns_empty(monkeypatch):
    contacts_from({}, monkeypatch)
    assert paint_service._seam_targets(context(), seam_graph([[1, 2]]), 100, [1], [3], {}) == []


def painted_request(monkeypatch, tmp_path, seam_targets, refine, refused):
    """Drive painted_split up to the seam loop with every graph access faked."""
    from datetime import timedelta
    from pychunkedgraph.app import app_utils
    import supervoxel_history
    import supervoxel_paint_history as history
    import supervoxel_refine
    cg = SimpleNamespace(meta=SimpleNamespace(graph_config=SimpleNamespace(ROOT_LOCK_EXPIRY=timedelta(seconds=5))))
    monkeypatch.setattr(app_utils, 'get_cg', lambda *a, **k: cg)
    monkeypatch.setattr(history, 'recover_paint', lambda *a: None)
    monkeypatch.setattr(paint_service, '_fault', lambda *a, **k: None)
    grouped = {1: {'source': np.asarray([[2, 2, 2]]), 'sink': np.empty((0, 3), int)},
               2: {'source': np.empty((0, 3), int), 'sink': np.asarray([[11, 2, 2]])}}
    monkeypatch.setattr(paint_service, '_lookup', lambda *a: (100, [1], [2], grouped))
    monkeypatch.setattr(paint_service, '_seam_targets', seam_targets)
    monkeypatch.setattr(supervoxel_refine, 'refine', refine)
    ids = np.arange(1, 200, dtype=np.uint64)
    monkeypatch.setattr(supervoxel_history, 'load_atom_tables',
                        lambda *a: SimpleNamespace(ids=ids, bounds=np.zeros((len(ids), 6), np.int64)))
    recorded = {}
    journal = SimpleNamespace(data=recorded, update=lambda **v: recorded.update(v),
                              refuse=lambda error, **k: refused.append(error) or recorded)
    monkeypatch.setattr(history.PaintJournal, 'create', lambda *a, **k: journal)
    app = app_for(monkeypatch, specimen(), tmp_path, lambda table: pytest.fail('native cut reached'))

    @app.before_request
    def identity():
        g.auth_user = {'id': 1}
    return app.test_client().post('/split', json=body())


def refined_once(new):
    def refine(cg, requests, **kwargs):
        atom = requests[0]['atom']
        return SimpleNamespace(count=1, operation_ids=[str(atom)], new_ids={atom: new(atom)})
    return refine


def test_repeatedly_shaved_seam_piece_is_refused(monkeypatch, tmp_path):
    # The same lineage keeps coming back: 10 -> 11 -> 12 -> 13 would be the fourth split.
    seen = []
    targets = lambda *a: [(10 + len(seen), np.array([[0, 0, 0]]), np.array([[1, 0, 0]]), [1, 2])]

    def refine(cg, requests, **kwargs):
        seen.append(requests[0]['atom'])
        return refined_once(lambda atom: [atom + 1, 150])(cg, requests)
    refused = []
    response = painted_request(monkeypatch, tmp_path, targets, refine, refused)
    assert seen == [10, 11, 12]
    assert response.status_code == 400 and response.json['paint_refusal']['kind'] == 'seam'
    assert 'repeated refinement' in str(refused[-1])


def test_deadline_after_refinements_says_they_are_kept(monkeypatch, tmp_path):
    calls = []

    def refine(cg, requests, **kwargs):
        calls.append(requests[0]['atom'])
        if len(calls) == 3:
            raise PaintError('deadline', 'The paint deadline expired before refinement publication.')
        return refined_once(lambda atom: [atom + 100])(cg, requests)
    targets = lambda *a: [(atom, np.array([[0, 0, 0]]), np.array([[1, 0, 0]]), [1, 2]) for atom in (20, 21, 22)]
    refused = []
    response = painted_request(monkeypatch, tmp_path, targets, refine, refused)
    assert response.status_code == 400 and response.json['paint_refusal']['kind'] == 'deadline'
    assert calls == [20, 21, 22]
    message = str(refused[-1])
    assert 'The 2 supervoxel refinements already made are kept' in message and 'resend' in message


def test_whole_atom_refinement_makes_two_sides_not_grid_cells():
    from supervoxel_refine import _compute_pieces
    voxels = np.zeros((4, 4, 40), np.uint64)  # ZYX bar spanning three 16-voxel grid cells in X
    voxels[1:3, 1:3, :] = 7
    snapshot = {'voxels': voxels, 'atom': 7, 'lo': np.array([3, 0, 0])}
    request = {'source_vox': [[1, 1, 0]], 'sink_vox': [[1, 1, 39]]}
    _, gridded = _compute_pieces(snapshot, request, [16, 16, 16], [10, 10, 10])
    pieces, whole = _compute_pieces(snapshot, request, [16, 16, 16], [10, 10, 10], whole_atom=True)
    assert gridded == 3 and whole == 2
    assert pieces[1, 1, 0] != pieces[1, 1, 39]


def test_whole_atom_falls_back_to_grid_above_the_size_cap(monkeypatch):
    import supervoxel_refine
    monkeypatch.setattr(supervoxel_refine, 'WHOLE_ATOM_MAX_VOXELS', 10)
    voxels = np.zeros((4, 4, 40), np.uint64)
    voxels[1:3, 1:3, :] = 7
    snapshot = {'voxels': voxels, 'atom': 7, 'lo': np.array([3, 0, 0])}
    request = {'source_vox': [[1, 1, 0]], 'sink_vox': [[1, 1, 39]]}
    assert supervoxel_refine._compute_pieces(snapshot, request, [16, 16, 16], [10, 10, 10], whole_atom=True)[1] == 3
