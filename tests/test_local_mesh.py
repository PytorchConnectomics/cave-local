import sys
import types
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import local_mesh
except ImportError as error:  # zmesh, DracoPy, Flask and PCG exist only in the pinned PCG image
    local_mesh = None
    SKIP = str(error)


class FakeVolume:
    def __init__(self, labels):
        self.labels = labels
        self.bounds = types.SimpleNamespace(maxpt=labels.shape)

    def __getitem__(self, key):
        return self.labels[key][..., None]


@unittest.skipIf(local_mesh is None, 'requires the PCG image')
class CoarseMeshTest(unittest.TestCase):
    def test_block_mode_ignores_background(self):
        a = np.zeros((4, 2, 2), np.uint64)
        a[:2] = 7
        a[2:, 0, 0] = 9
        self.assertEqual(local_mesh.block_mode(a, (2, 2, 2)).ravel().tolist(), [7, 9])

    def test_refined_blocks_follow_current_pieces(self):
        # Supervoxel 5 fills x < 6 and was refined into pieces 50 (x < 3) and 51 (3 <= x < 6) after the coarse copy.
        full = np.zeros((8, 2, 2), np.uint64)
        full[:3] = 50
        full[3:6] = 51
        full[6:] = 8
        coarse = local_mesh.block_mode(np.where(full >= 50, 5, full).astype(np.uint64), (2, 2, 2))
        self.assertEqual(coarse.ravel().tolist(), [5, 5, 5, 8])
        tables = types.SimpleNamespace(retired=frozenset({5}))
        original = local_mesh.CloudVolume
        local_mesh.CloudVolume = lambda *args, **kwargs: FakeVolume(full)
        try:
            out = local_mesh.refresh_refined(coarse.copy(), np.zeros(3, int), np.array([2, 2, 2]), tables)
        finally:
            local_mesh.CloudVolume = original
        # Block 1 holds two voxels-planes of 50 and two of 51: the first most frequent label wins, as in the copy script.
        self.assertEqual(out.ravel().tolist(), [50, 50, 51, 8])
        self.assertFalse(np.isin(out, [5]).any())

    def test_no_retired_supervoxels_leaves_the_copy(self):
        coarse = np.array([[[3]]], np.uint64)
        tables = types.SimpleNamespace(retired=frozenset())
        self.assertIs(local_mesh.refresh_refined(coarse, np.zeros(3, int), np.array([2, 2, 2]), tables), coarse)


if __name__ == '__main__':
    unittest.main()
