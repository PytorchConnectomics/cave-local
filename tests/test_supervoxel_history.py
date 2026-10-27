"""Atom generation, time travel and native-translation race contracts."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace, ModuleType
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import supervoxel_history as history


def atom_row(atom, label=None):
    point = [atom % 97, atom % 23, atom % 11]
    return [atom, atom if label is None else label, *point]


def box(row):
    return [*row[2:], *(np.asarray(row[2:]) + 1)]


def legacy(tmp_path, ids=(100, 200, 300)):
    rows = np.array([atom_row(atom) for atom in ids], np.uint64)
    np.save(tmp_path / 'atom_index.npy', rows)
    np.save(tmp_path / 'atom_bounds.npy', np.array([box(row) for row in rows], np.uint64))
    np.save(tmp_path / 'initial_roots.npy', np.array([10000 + atom for atom in ids], np.uint64))
    return history.load_atom_tables(tmp_path)


def stage(tmp_path, old, new, timestamp, edges=(), destination=None):
    before = history.load_atom_tables(tmp_path)
    pos = int(np.searchsorted(before.ids, old))
    rows = np.array([atom_row(atom, int(before.index[pos, 1])) for atom in new], np.uint64)
    return history.stage_generation(
        tmp_path, new_index_rows=rows, new_bounds_rows=[box(row) for row in rows],
        new_initial_roots=[before.initial_roots[pos]] * len(new),
        new_lineage_rows=[(atom, old, timestamp, f'op-{timestamp}') for atom in new],
        new_edge_map_rows=[(*edge, f'op-{timestamp}', timestamp) for edge in edges],
        destination=destination,
    )


def publish(tmp_path, old, new, timestamp, edges=()):
    staged = stage(tmp_path, old, new, timestamp, edges)
    return history.publish_generation(staged, tmp_path)


def test_legacy_and_generation_zero_have_independent_sparse_lengths(tmp_path):
    tables = legacy(tmp_path)
    assert tables.generation == 'legacy'
    assert len(tables.index) == 3 and len(tables.lineage) == 0
    zero = history.initialize_atom_tables(tmp_path)
    assert zero.generation == '0'
    assert len(zero.lineage) == len(zero.edge_map) == 0
    for name in history.TABLE_FILES[:3]:
        assert (tmp_path / name).is_symlink()


def test_staging_does_not_publish_and_payload_survives_idempotent_publication(tmp_path):
    before = legacy(tmp_path)
    original = (tmp_path / 'atom_index.npy').read_bytes()
    staged = stage(tmp_path, 100, [101, 102], 1000)
    assert (tmp_path / 'atom_index.npy').read_bytes() == original
    assert not (tmp_path / 'atom_index.npy').is_symlink()
    assert not (tmp_path / 'atom_tables' / 'current').exists()
    tables = history.publish_generation(staged, tmp_path)
    assert len(tables.index) == 5 and len(tables.lineage) == 2
    assert np.array_equal(tables.ids, [100, 101, 102, 200, 300])
    assert before.retired == frozenset()
    assert tables.retired == {100}
    assert history.publish_generation(staged, tmp_path).generation == tables.generation
    assert (staged / 'lineage.npy').exists()
    assert (tmp_path / 'atom_tables' / '0').is_dir()


def test_temporary_symlink_cleanup_is_exact_and_does_not_follow_targets(tmp_path):
    from supervoxel_refine import mutation_lock
    data = tmp_path / 'data'
    tables = data / 'atom_tables'
    tables.mkdir(parents=True)
    external = tmp_path / 'external'
    external.write_text('must survive')
    removed = [data / ('.' + name + '.' + 'a' * 32) for name in history.TABLE_FILES[:3]]
    removed.append(tables / ('.current.' + 'b' * 32))
    for path in removed:
        path.symlink_to(external)
    # Include a dangling temporary link: exists() would miss it.
    dangling = tables / ('.current.' + 'c' * 32)
    dangling.symlink_to('missing-generation')
    removed.append(dangling)
    preserved = [
        data / ('.current.' + 'a' * 32),
        tables / ('.atom_index.npy.' + 'a' * 32),
        tables / ('.current.' + 'a' * 31),
        tables / ('.current.' + 'A' * 32),
        tables / ('.current.' + 'a' * 32 + '.extra'),
        data / ('.unrelated.npy.' + 'a' * 32),
    ]
    for path in preserved:
        path.symlink_to(external)
    regular = data / ('.atom_index.npy.' + 'd' * 32)
    regular.write_text('ordinary file')
    directory = tables / ('.current.' + 'e' * 32)
    directory.mkdir()
    nested = directory / ('.current.' + 'f' * 32)
    nested.symlink_to(external)
    with mutation_lock(data):
        assert set(history.cleanup_temporary_symlinks(data)) == set(removed)
        assert history.cleanup_temporary_symlinks(data) == []
    assert all(not path.is_symlink() for path in removed)
    assert all(path.is_symlink() for path in [*preserved, nested])
    assert external.read_text() == 'must survive'
    assert regular.read_text() == 'ordinary file'
    assert directory.is_dir()


@pytest.mark.parametrize('pointer', ['current', *history.TABLE_FILES[:3]])
def test_kill_between_symlink_creation_and_replace_replays_and_archives(tmp_path, pointer):
    import operations
    from supervoxel_refine import mutation_lock
    data = tmp_path / 'data'
    data.mkdir()
    legacy(data)
    staged = stage(data, 100, [101, 102], 1000)
    # A hard process kill leaves the actual filesystem artifact; no finally
    # block or mocked exception can clean it before replay inspects it.
    program = '''
import os
from pathlib import Path
import signal
import sys
sys.path.insert(0, sys.argv[1])
from supervoxel_history import publish_generation
from supervoxel_refine import mutation_lock
def kill_after_create(temporary, link):
    if link.name == sys.argv[4]:
        os.kill(os.getpid(), signal.SIGKILL)
with mutation_lock(sys.argv[3]):
    publish_generation(sys.argv[2], sys.argv[3], symlink_created=kill_after_create)
'''
    child = subprocess.run([sys.executable, '-c', program, str(Path(history.__file__).parent),
                            str(staged), str(data), pointer], capture_output=True, text=True,
                           timeout=30)
    assert child.returncode == -signal.SIGKILL, child.stderr
    parent = data / 'atom_tables' if pointer == 'current' else data
    orphans = list(parent.glob('.' + pointer + '.*'))
    assert len(orphans) == 1 and orphans[0].is_symlink()
    assert len(orphans[0].name.rsplit('.', 1)[1]) == 32
    with mutation_lock(data):
        tables = history.publish_generation(staged, data)
    assert not orphans[0].is_symlink()
    assert tables.retired == {100}
    archive = tmp_path / 'workspace.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        operations.tar_add(output, data, 'data', allow_atom_links=True)
    with tarfile.open(archive) as source:
        assert sum(item.issym() for item in source.getmembers()) == 4
    extracted = tmp_path / 'extracted'
    operations.extract_tar(archive, extracted, allow_atom_links=True)
    restored = history.load_atom_tables(extracted / 'data')
    np.testing.assert_array_equal(restored.lineage, tables.lineage)
    np.testing.assert_array_equal(history.ancestors_at([101, 102], 999, restored), [100, 100])


@pytest.mark.parametrize('order', [(100, 200), (200, 100)])
def test_generation_sort_keeps_each_dense_row_attached_to_its_id(tmp_path, order):
    tables = legacy(tmp_path)
    expected = {int(row[0]): (row.copy(), tables.bounds[i].copy(), int(tables.initial_roots[i]))
                for i, row in enumerate(tables.index)}
    for number, old in enumerate(order):
        new = old + 1
        tables = publish(tmp_path, old, [new], 1000 + number)
        row = np.array(atom_row(new, old), np.uint64)
        expected[new] = (row, np.array(box(row)), 10000 + old)
        assert np.all(tables.ids[1:] > tables.ids[:-1])
        for i, actual in enumerate(tables.index):
            wanted, bounds, root = expected[int(actual[0])]
            np.testing.assert_array_equal(actual, wanted)
            np.testing.assert_array_equal(tables.bounds[i], bounds)
            assert int(tables.initial_roots[i]) == root


def test_repeated_refinement_ancestry_and_sparse_roundtrip(tmp_path):
    legacy(tmp_path)
    publish(tmp_path, 100, [101, 102], 1000)
    tables = publish(tmp_path, 101, [103, 104], 2000)
    assert len(tables.index) == len(tables.bounds) == len(tables.initial_roots) == 7
    assert len(tables.lineage) == 4
    assert history.ancestor_at(103, 999, tables) == 100
    assert history.ancestor_at(103, 1000, tables) == 101
    assert history.ancestor_at(103, 1999, tables) == 101
    assert history.ancestor_at(103, 2000, tables) == 103
    assert history.ancestor_at(103, datetime.fromtimestamp(.5, timezone.utc), tables) == 100
    assert history.ancestor_at(103, np.datetime64('1970-01-01T00:00:00.500'), tables) == 100
    assert history.ancestor_at(0, 0, tables) == 0
    np.testing.assert_array_equal(history.current_descendants(100, tables), [102, 103, 104])
    np.testing.assert_array_equal(history.ancestors_at([103, 200, 0], 0, tables), [100, 200, 0])


@pytest.mark.parametrize('with_lineage', [False, True])
def test_large_current_root_batch_skips_lineage_search(tmp_path, monkeypatch, with_lineage):
    tables = legacy(tmp_path)
    if with_lineage:
        tables = publish(tmp_path, 100, [101, 102], 1000)
    atoms = np.resize(np.array([101, 200, 2**63 + 7], np.uint64), 200_000)
    def unexpected(*args, **kwargs):
        pytest.fail('current root batch must not perform per-id or lineage searches')
    monkeypatch.setattr(history, 'ancestor_at', unexpected)
    monkeypatch.setattr(history.np, 'searchsorted', unexpected)
    started = time.perf_counter()
    actual = history.ancestors_at(atoms, 1000, tables)
    elapsed = time.perf_counter() - started
    np.testing.assert_array_equal(actual, atoms)
    assert actual is not atoms
    print(f'200k current roots, lineage={with_lineage}: {elapsed:.6f}s')


@pytest.mark.parametrize('timestamp', [None, 0, 999, 1000, 1999, 2000, 3000])
def test_vectorized_ancestors_match_scalar_through_repeated_unsigned_lineage(tmp_path, timestamp):
    high = 2**63
    legacy(tmp_path, (high + 100, high + 200))
    publish(tmp_path, high + 100, [high + 101, high + 102], 1000)
    tables = publish(tmp_path, high + 101, [high + 103, high + 104], 2000)
    atoms = np.array([0, high + 100, high + 101, high + 102, high + 103,
                      high + 104, high + 105, 2**64 - 1, high + 103], np.uint64)
    expected = [history.ancestor_at(atom, timestamp, tables) for atom in atoms]
    np.testing.assert_array_equal(history.ancestors_at(atoms, timestamp, tables), expected)
    np.testing.assert_array_equal(history.ancestors_at([], timestamp, tables), [])


def test_concurrent_table_readers_during_generation_switch_and_cache_eviction(tmp_path):
    legacy(tmp_path)
    history.initialize_atom_tables(tmp_path)
    started, done = Event(), Event()
    def reader():
        started.set()
        reads = 0
        while not done.is_set() or reads < 50:
            tables = history.load_atom_tables(tmp_path)
            assert len(tables.index) == len(tables.bounds) == len(tables.initial_roots)
            assert len(tables.ids) == 3 + int(tables.generation)
            np.testing.assert_array_equal(tables.initial_roots[:int(tables.generation) + 1],
                                          np.full(int(tables.generation) + 1, 10100))
            reads += 1
        return reads
    with ThreadPoolExecutor(max_workers=5) as executor:
        readers = [executor.submit(reader) for _ in range(4)]
        assert started.wait(5)
        try:
            for number in range(1, 25):
                publish(tmp_path, 99 + number, [100 + number], number * 1000)
        finally:
            done.set()
        assert all(future.result() >= 50 for future in readers)


def test_table_cache_hit_does_not_use_check_then_index(tmp_path, monkeypatch):
    tables = legacy(tmp_path)
    class GetOnlyCache(dict):
        def __contains__(self, key):
            pytest.fail('cache lookup must be atomic, without a check-then-index race')
        def __getitem__(self, key):
            pytest.fail('cache lookup must use get')
    monkeypatch.setattr(history, '_table_cache', GetOnlyCache(history._table_cache))
    assert history.load_atom_tables(tmp_path) is tables


@pytest.mark.parametrize('bad_rows,match', [
    ([(999, 100, 10, 'op')], 'endpoints'),
    ([(101, 100, 10, 'op'), (101, 100, 11, 'op')], 'unique'),
    ([(100, 101, 20, 'op'), (101, 100, 10, 'op')], 'cycle|increase'),
    ([(101, 100, 10, 'op'), (102, 101, 10, 'op')], 'increase'),
    ([(101, 101, 10, 'op')], 'itself'),
])
def test_loader_rejects_invalid_lineage(tmp_path, bad_rows, match):
    tables = legacy(tmp_path, (100, 101, 102))
    with pytest.raises(ValueError, match=match):
        history.validate_atom_tables(replace(tables, lineage=np.array(bad_rows, history.LINEAGE_DTYPE)))


def test_edge_translation_both_endpoints_and_repeated_descendants(tmp_path):
    legacy(tmp_path)
    publish(tmp_path, 100, [101, 102], 1000,
            [(100, 200, 101, 200), (100, 200, 102, 200)])
    publish(tmp_path, 200, [201], 2000,
            [(101, 200, 101, 201), (102, 200, 102, 201)])
    tables = publish(tmp_path, 101, [103], 3000, [(101, 201, 103, 201)])
    np.testing.assert_array_equal(history.translate_edges([[200, 100]], tables=tables),
                                  [[102, 201], [103, 201]])
    np.testing.assert_array_equal(history.translate_edges([[300, 201]], tables=tables), [[201, 300]])
    with pytest.raises(ValueError, match='no provenance'):
        history.translate_edges([[100, 300]], tables=tables)


def test_edge_validation_handles_reversed_endpoint_order_after_new_ids(tmp_path):
    legacy(tmp_path)
    tables = publish(tmp_path, 100, [250], 1000, [(100, 200, 200, 250)])
    np.testing.assert_array_equal(history.translate_edges([[100, 200]], tables=tables), [[200, 250]])
    invalid = np.array([(100, 200, 250, 300, 'op', 1000)], history.EDGE_MAP_DTYPE)
    with pytest.raises(ValueError, match='lineage'):
        history.validate_atom_tables(replace(tables, edge_map=invalid))


def test_recorded_representative_alias_translates_without_creating_graph_edges(tmp_path):
    # Native merge may log representative (100, 200) while activating the real
    # contact (150, 200) elsewhere between those roots. Refinement must preserve
    # the recorded alias in edge_map without inventing a contact/fake edge.
    legacy(tmp_path, (100, 150, 200))
    np.save(tmp_path / 'initial_roots.npy', np.array([900, 900, 901], np.uint64))
    publish(tmp_path, 100, [101], 1000, [(100, 200, 101, 200)])
    tables = publish(tmp_path, 101, [102], 2000, [(101, 200, 102, 200)])
    assert not (tmp_path / 'edges').exists()
    np.testing.assert_array_equal(history.translate_edges([[100, 200]], tables=tables), [[102, 200]])
    np.testing.assert_array_equal(history.translate_edges([[150, 200]], tables=tables), [[150, 200]])
    operation = SimpleNamespace(superseded_operation=SimpleNamespace(
        added_edges=np.array([[100, 200]], np.uint64)))
    token = history.translated_ids.set(())
    try:
        history.translate_operation(operation, tables)
        np.testing.assert_array_equal(operation.added_edges, [[102, 200]])
        assert set(history.translated_ids.get()) == {102, 200}
    finally:
        history.translated_ids.reset(token)


def test_load_resolves_current_once_and_previous_generation_remains_valid(tmp_path):
    legacy(tmp_path)
    first = publish(tmp_path, 100, [101], 1000)
    second = publish(tmp_path, 200, [201], 2000)
    assert first.generation != second.generation
    assert first.ids.tolist() == [100, 101, 200, 300]
    assert history.load_atom_tables(tmp_path).ids.tolist() == [100, 101, 200, 201, 300]
    assert history.load_atom_tables(tmp_path, first.path).ids.tolist() == first.ids.tolist()


def test_publish_refuses_corrupted_replay_payload(tmp_path):
    legacy(tmp_path)
    staged = stage(tmp_path, 100, [101], 1000)
    with open(staged / 'atom_bounds.npy', 'ab') as handle:
        handle.write(b'corrupt')
    with pytest.raises(ValueError, match='hash mismatch'):
        history.publish_generation(staged, tmp_path)
    assert not (tmp_path / 'atom_tables' / 'current').exists()


def test_seqlock_waits_for_even_and_retries_changed_epoch(monkeypatch):
    epochs = iter([1, 2, 4, 4, 4])
    monkeypatch.setattr(history, 'read_epoch', lambda *args: next(epochs))
    calls = []
    result = history.snapshot_read(lambda: calls.append(1) or len(calls), poll_interval=0)
    assert result == 2 and len(calls) == 2


def test_seqlock_retries_reader_error_only_when_epoch_changed(monkeypatch):
    epochs = iter([0, 2, 2, 2])
    monkeypatch.setattr(history, 'read_epoch', lambda *args: next(epochs))
    calls = []
    def read():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError('temporary publication mismatch')
        return 9
    assert history.snapshot_read(read) == 9
    monkeypatch.setattr(history, 'read_epoch', lambda *args: 4)
    with pytest.raises(ValueError, match='stable error'):
        history.snapshot_read(lambda: (_ for _ in ()).throw(ValueError('stable error')))


def test_seqlock_has_wait_and_attempt_bounds(monkeypatch):
    monkeypatch.setattr(history, 'read_epoch', lambda *args: 1)
    with pytest.raises(history.PublicationBusy, match='progress'):
        history.snapshot_read(lambda: None, timeout=0)
    epochs = iter(range(0, 20, 2))
    monkeypatch.setattr(history, 'read_epoch', lambda *args: next(epochs))
    with pytest.raises(history.PublicationBusy, match='changed'):
        history.snapshot_read(lambda: None, max_attempts=3)


def test_seqlock_attempts_idle_recovery_before_reading(tmp_path):
    directory = tmp_path / 'supervoxel_splits'
    directory.mkdir()
    epoch = directory / 'epoch'
    epoch.write_text('1')
    calls = []
    def recover():
        calls.append('recover')
        epoch.write_text('2')
    assert history.snapshot_read(lambda: calls.append('read') or 17,
                                 tmp_path, recover=recover) == 17
    assert calls == ['recover', 'read']


def test_seqlock_limits_idle_recovery_and_retries_on_next_read(monkeypatch):
    monkeypatch.setattr(history, 'read_epoch', lambda *args: 1)
    calls = []
    for _ in range(2):
        with pytest.raises(history.PublicationBusy):
            history.snapshot_read(lambda: pytest.fail('must not read odd epoch'),
                                  timeout=.005, poll_interval=0,
                                  recover=lambda: calls.append('try'))
    assert calls == ['try', 'try']


def test_translation_revalidation_rejects_only_retired_context_ids(tmp_path):
    legacy(tmp_path)
    token = history.translated_ids.set((200,))
    try:
        publish(tmp_path, 100, [101], 1000)
        history.check_translation_current(tmp_path)
        publish(tmp_path, 200, [201], 2000)
        with pytest.raises(history.StaleTranslation):
            history.check_translation_current(tmp_path)
    finally:
        history.translated_ids.reset(token)


def test_retry_retranslates_after_retirement_and_cleans_context(tmp_path):
    legacy(tmp_path)
    calls = []
    def operation():
        tables = history.load_atom_tables(tmp_path)
        current = history.current_descendants(100, tables)
        history.translated_ids.set(tuple(map(int, current)))
        calls.append(current.tolist())
        if len(calls) == 1:
            publish(tmp_path, 100, [101], 1000)
        history.check_translation_current(tmp_path)
        return 'done'
    assert history.retry_translated_operation(operation, tmp_path) == 'done'
    assert calls == [[100], [101]]
    assert history.translated_ids.get() == ()


def test_retry_catches_stale_native_precheck_and_caps_three_attempts(tmp_path):
    legacy(tmp_path)
    class PreconditionError(Exception):
        pass
    calls = []
    def precheck():
        calls.append(1)
        if len(calls) == 1:
            history.translated_ids.set((100,))
            publish(tmp_path, 100, [101], 1000)
            raise PreconditionError('All edges must exist')
        return 'ok'
    assert history.retry_translated_operation(precheck, tmp_path,
        precondition_errors=(PreconditionError,)) == 'ok'
    retries = []
    def always_stale():
        raise history.StaleTranslation()
    with pytest.raises(history.PublicationBusy, match='retry'):
        history.retry_translated_operation(always_stale, tmp_path, on_retry=retries.append)
    assert retries == [1, 2, 3]


def test_translate_fake_log_operation_edges_seeds_and_coordinate_fallback(tmp_path):
    legacy(tmp_path)
    tables = publish(tmp_path, 100, [101, 102], 1000,
                     [(100, 200, 101, 200), (100, 200, 102, 200)])
    effective = SimpleNamespace(
        added_edges=np.array([[100, 200]], np.uint64), affinities=np.array([.75]),
        source_ids=np.array([100]), sink_ids=np.array([200]),
        source_coords=np.array([[8, 8, 8]]), sink_coords=np.array([[16, 16, 16]]),
    )
    outer = SimpleNamespace(superseded_operation=effective)
    token = history.translated_ids.set(())
    try:
        history.translate_operation(outer, tables, coordinate_lookup=lambda coordinate: 102)
        np.testing.assert_array_equal(effective.added_edges, [[101, 200], [102, 200]])
        np.testing.assert_array_equal(outer.added_edges, effective.added_edges)
        np.testing.assert_array_equal(effective.source_ids, [102])
        np.testing.assert_allclose(effective.affinities, [.75, .75])
        assert set(history.translated_ids.get()) == {101, 102, 200}
        fallback = SimpleNamespace(source_ids=np.array([100]), source_coords=np.array([[8, 8, 8]]))
        history.translate_operation(fallback, tables, coordinate_lookup=lambda coordinate: 300)
        np.testing.assert_array_equal(fallback.source_ids, [101, 102])
        assert len(fallback.source_coords) == 2
    finally:
        history.translated_ids.reset(token)


def test_disabled_install_does_not_import_pcg(monkeypatch):
    # This service-free suite has no PCG dependency; disabled means no adapter work.
    history.install_history(enabled=False)


@pytest.mark.parametrize('requested', [100, 101])
@pytest.mark.parametrize('bridge', [[200, 300], [300, 200]])
def test_undo_after_redo_uses_actual_bridge_with_refined_history(tmp_path, monkeypatch, requested, bridge):
    legacy(tmp_path)
    tables = publish(tmp_path, 100, [101], 1000, [(100, 200, 101, 200)])
    # The requested 100--200 merge activated 300--200 instead. The translated
    # 101--200 pair is a log alias only; it cannot be passed to native split.
    # Same-chunk fake edges can be stored in reverse order only. Native undo's
    # ordered existence precheck must receive the actual stored orientation.
    actual = np.array([bridge], np.uint64)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    calls = []
    cg = SimpleNamespace(
        client=SimpleNamespace(read_log_entry=lambda operation_id: ({'added': np.array([[requested, 200]])}, stamp)),
        get_roots=lambda ids: np.full(len(ids), 9000, np.uint64),
        get_subgraph=lambda roots, **kwargs: actual,
    )
    inverse = SimpleNamespace(removed_edges=np.array([[100, 200]], np.uint64),
                              source_coords=[[1, 1, 1]], sink_coords=[[2, 2, 2]], bbox_offset=[120, 120, 12])
    operation = SimpleNamespace(cg=cg, superseded_operation_id=37, inverse_superseded_operation=inverse)
    for name in ('pychunkedgraph', 'pychunkedgraph.graph', 'pychunkedgraph.graph.utils'):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    def choose(cg, **kwargs):
        calls.append(kwargs)
        return actual
    for name, members in {
        'pychunkedgraph.graph.attributes': {'OperationLogs': SimpleNamespace(AddedEdge='added')},
        'pychunkedgraph.graph.edits': {'merge_preprocess': choose},
        'pychunkedgraph.graph.utils.generic': {'get_bounding_box': lambda *args: 'bbox'},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
    token = history.translated_ids.set(())
    try:
        history.refresh_merge_undo(operation, tables)
        history.translate_operation(operation, tables)
        np.testing.assert_array_equal(operation.removed_edges, actual)
        np.testing.assert_array_equal(inverse.removed_edges, actual)
        np.testing.assert_array_equal(calls[0]['supervoxels'], [101, 200])
        assert (stamp - calls[0]['parent_ts']).total_seconds() == .1
        assert history.translated_ids.get() == (200, 300)
    finally:
        history.translated_ids.reset(token)


def test_translate_operation_preserves_current_edge_orientation_and_affinity(tmp_path):
    legacy(tmp_path)
    tables = publish(tmp_path, 100, [101], 1000, [(100, 200, 101, 200)])
    operation = SimpleNamespace(
        added_edges=np.array([[300, 200], [200, 300], [200, 100]], np.uint64),
        affinities=np.array([.75, .75, .5]),
    )
    token = history.translated_ids.set(())
    try:
        history.translate_operation(operation, tables)
        np.testing.assert_array_equal(operation.added_edges, [[101, 200], [300, 200]])
        np.testing.assert_allclose(operation.affinities, [.5, .75])
        # The public edge-map function remains canonical and de-duplicates both
        # orientations; preserving native orientation is specific to operations.
        np.testing.assert_array_equal(history.translate_edges([[300, 200], [200, 300]], tables=tables),
                                      [[200, 300]])
    finally:
        history.translated_ids.reset(token)


def test_recorded_coordinates_are_voxels_not_nanometers(tmp_path, monkeypatch):
    lookups = []
    class FakeVolume:
        shape = (32, 32, 32, 1)
        def __init__(self, *args, **kwargs):
            pass
        def __getitem__(self, slices):
            lookups.append(tuple(part.start for part in slices))
            return np.array([[[[123]]]], np.uint64)
    module = ModuleType('cloudvolume')
    module.CloudVolume = FakeVolume
    monkeypatch.setitem(sys.modules, 'cloudvolume', module)
    cg = SimpleNamespace(segmentation_resolution=np.array([8, 8, 24]))
    assert history._coordinate_atom([8, 12, 3], cg, tmp_path) == 123
    assert lookups == [(8, 12, 3)]


def test_installed_history_covers_internal_single_and_batch_roots_and_factory(tmp_path, monkeypatch):
    legacy(tmp_path)
    publish(tmp_path, 100, [101], 1000, [(100, 200, 101, 200)])
    pauses = []
    faults = ModuleType('supervoxel_refine')
    faults.fixture_fault = lambda point, **context: pauses.append((point, context))
    monkeypatch.setitem(sys.modules, 'supervoxel_refine', faults)
    monkeypatch.setenv('CAVE_SVSPLIT_FAULT', 'fixture-only-test')

    class FakeGraphEdit:
        @classmethod
        def _resolve_undo_chain(cls, cg, **kwargs):
            return SimpleNamespace(inverse_superseded_operation=SimpleNamespace(
                removed_edges=np.array([[100, 200]], np.uint64)))

    class FakeCG:
        def get_roots(self, ids, **kwargs):
            return np.asarray(ids, np.uint64) + np.uint64(10000)
        def get_root(self, atom, **kwargs):
            return np.uint64(atom) + np.uint64(10000)
        def undo_operation(self, operation_id):
            operation = FakeGraphEdit._resolve_undo_chain(self, is_undo=True)
            history.check_translation_current(tmp_path)
            return operation.inverse_superseded_operation.removed_edges
        def redo_operation(self, operation_id):
            return self.undo_operation(operation_id)

    for name in ('pychunkedgraph', 'pychunkedgraph.graph'):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, members in {
        'pychunkedgraph.graph.chunkedgraph': {'ChunkedGraph': FakeCG},
        'pychunkedgraph.graph.operation': {'GraphEditOperation': FakeGraphEdit},
        'pychunkedgraph.graph.exceptions': {'PreconditionError': ValueError},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
    history.install_history(tmp_path)
    cg = FakeCG()
    np.testing.assert_array_equal(cg.get_roots([101, 200], time_stamp=0), [10100, 10200])
    np.testing.assert_array_equal(cg.get_roots([101, 200]), [10101, 10200])
    assert cg.get_root(101, time_stamp=0) == 10100
    assert cg.get_root(101) == 10101
    np.testing.assert_array_equal(cg.undo_operation(123), [[101, 200]])
    assert len(pauses) == 1
    assert pauses[0][0] == 'after_translation'
    assert pauses[0][1]['translated_ids'] == [101, 200]
    assert history.translated_ids.get() == ()
