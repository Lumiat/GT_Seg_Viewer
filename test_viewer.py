"""Synthetic fixtures only; real input volumes are never modified."""
import csv
import json
from pathlib import Path
import struct
import tempfile
import unittest

import h5py
import numpy as np
from fastapi.testclient import TestClient

from viewer import FIELDS, Gallery, make_app, overlap_counts, plan_slices, read_clusters


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(FIELDS)
        writer.writerows(rows)


def make_fixture(root):
    large = 2**63 + 17
    gt = np.zeros((5, 24, 32), dtype=np.uint64)
    s0 = np.zeros_like(gt)
    gt[:3, 3:12, 3:12] = 1
    gt[0, 15:19, 2:6] = large
    gt[1, 15:19, 2:6] = large + 1
    s0[0, 3:7, 3:7] = 101
    s0[1, 3:7, 3:7] = 102
    s0[2, 3:7, 3:7] = 103
    s0[4, 3:7, 3:7] = 104  # Exists, never shares a Z with GT 1.
    s0[0, 15:19, 2:6] = large
    s0[0, 15:19, 6:8] = large + 1
    for i in range(2, 12):
        gt[3, i, 20:23] = i
    em = np.arange(np.prod(gt.shape), dtype=np.uint8).reshape(gt.shape)
    for name, key, data in [('em.h5', 'inputs', em), ('gt.h5', 'labels', gt)]:
        with h5py.File(root / name, 'w') as f:
            f[key] = data
    np.save(root / 's0.npy', s0)
    write_csv(root / 'cluster.csv', [
        [1, '1vn', 5, '101,102,103,104,999'],
        [large, '1v1', 0, str(large)],  # Legacy CSV count is allowed.
        [large + 1, '1vn', 2, f'{large},{large+1}'],
        [999, '1v1', 1, '101'],
        *[[i, '1v0', 0, ''] for i in range(2, 12)],
    ])
    return gt, s0


class ViewerTests(unittest.TestCase):
    def test_api_locates_overlap_outside_representative_slices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gt = np.zeros((3, 12, 12), dtype=np.uint64)
            gt[:, 4:8, 4:8] = 9
            s0 = np.zeros_like(gt)
            s0[0, :3, :3] = 1997  # Same Z as GT, no shared pixel.
            s0[0, 4:8, 4:8] = 12  # Makes Z=0 the sole representative slice.
            s0[1, 4:6, 4:6] = 1997
            s0[2, 4:8, 4:8] = 1997
            for filename, key, array in [('em.h5', 'inputs', np.zeros(gt.shape, dtype=np.uint8)),
                                         ('gt.h5', 'labels', gt)]:
                with h5py.File(root / filename, 'w') as f:
                    f[key] = array
            np.save(root / 's0.npy', s0)
            write_csv(root / 'cluster.csv', [[9, '1vn', 2, '1997,12']])
            app = make_app(root, root / 'cluster.csv')
            app.state.gallery.build_index()
            with TestClient(app) as client:
                row = client.get('/api/clusters?tag=1vn').json()['items'][0]
                self.assertEqual(row['recommended_z'], [0])
                self.assertEqual(row['s0_targets']['1997']['z'], 2)
                self.assertEqual(row['s0_targets']['1997']['overlap_by_z'], {'1': 4, '2': 16})
                self.assertEqual(row['s0_targets']['12']['z'], 0)
                response = client.get('/api/frame/9/2?focus_s0_id=1997')
                n = struct.unpack('<4sIII', response.content[:16])[3]
                self.assertEqual(json.loads(response.content[16:16+n])['focus']['overlap_pixels'], 16)

    def test_s0_location_prefers_actual_overlap_in_selected_slices(self):
        row = dict(gt_id='9', tag='1vn', s0_count_per_gt=5,
                   covered_s0_id=['1997', '12', '13', '14', '15'])
        index = {'gt': {9: {92: 100, 93: 100, 97: 100}}, 's0': {
            1997: {92: 200, 93: 300, 97: 400}, 12: {92: 40, 97: 90},
            13: {92: 4}, 14: {99: 8}}}
        plan = plan_slices(row, index, {1997: {93: 10, 97: 20}, 12: {92: 3, 97: 30}})
        self.assertEqual(plan['recommended_z'], [92])
        self.assertEqual(plan['s0_targets']['1997']['z'], 97)
        self.assertEqual(plan['s0_targets']['1997']['reason'], 'other_overlap')
        # A selected slice with a real intersection takes precedence over larger overlaps elsewhere.
        self.assertEqual(plan['s0_targets']['12']['z'], 92)
        self.assertEqual(plan['s0_targets']['12']['reason'], 'recommended_overlap')
        self.assertEqual(plan['s0_targets']['13']['reason'], 'no_overlap')
        self.assertEqual(plan['s0_targets']['14']['z'], 99)
        self.assertIsNone(plan['s0_targets']['15']['z'])
        self.assertEqual(plan['no_overlap_s0'], ['13', '14'])

    def test_overlap_index_counts_pixels_not_same_z_or_edge_contact(self):
        large = 2**63 + 17
        gt = np.array([[9, 9, 0, large], [0, 9, 0, large+1]], dtype=np.uint64)
        s0 = np.array([[1997, 0, 1997, large+1], [12, 12, 1997, large]], dtype=np.uint64)
        counts = overlap_counts(gt, s0, {9: {1997,12}, large: {large+1}, large+1: {large}})
        self.assertEqual(counts, {(9,1997):1, (9,12):1, (large,large+1):1, (large+1,large):1})
        self.assertEqual(overlap_counts(gt, np.zeros_like(s0), {9:{1997}}), {})

    def test_focused_frame_includes_all_pixels_and_disconnected_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, s0 = make_fixture(root)
            # S0 extends far beyond GT, with separate parts touching opposite edges.
            s0[0, 0, 0] = 101
            s0[0, 23, 31] = 101
            np.save(root / 's0.npy', s0)
            gallery = Gallery(root, root / 'cluster.csv')
            try:
                for gt_id, z, label in [('1', 0, '101'), ('1', 1, '102'), (str(2**63+17), 0, str(2**63+17))]:
                    payload = gallery.frame(gt_id, z, label)
                    _, w, h, n = struct.unpack('<4sIII', payload[:16])
                    meta = json.loads(payload[16:16+n])
                    focus = meta['focus']
                    expected = s0[z] == int(label)
                    yy, xx = np.nonzero(expected)
                    self.assertEqual(focus['pixel_count'], int(expected.sum()))
                    self.assertEqual(focus['bbox_xy'], [int(xx.min()), int(yy.min()), int(xx.max())+1, int(yy.max())+1])
                    x0, y0, x1, y1 = meta['bbox_xy']
                    slots = np.frombuffer(payload, dtype='<u4', offset=16+n+2*w*h).reshape(h,w)
                    mask = slots == meta['s0_ids'].index(label)
                    self.assertEqual(int(mask.sum()), int(expected.sum()))
                    self.assertTrue(np.array_equal(mask, expected[y0:y1,x0:x1]))
            finally:
                gallery.close()

    def test_joint_slice_cover_includes_every_coexistent_s0(self):
        row = dict(gt_id='1', tag='1vn', s0_count_per_gt=5, covered_s0_id=['11', '12', '13', '14', '15'])
        index = {'gt': {1: {0: 4, 1: 9, 2: 10}}, 's0': {
            11: {0: 8}, 12: {0: 2, 1: 8}, 13: {2: 6}, 14: {4: 8}}}
        plan = plan_slices(row, index)
        self.assertEqual(plan['initial_z'], 0)  # Most S0s beats greater GT area.
        self.assertEqual(plan['recommended_z'], [0, 2])
        self.assertEqual(plan['missing_s0'], ['15'])
        self.assertEqual(plan['disjoint_s0'], ['14'])
        covered = {s for item in plan['slices'] if item['z'] in plan['recommended_z'] for s in item['s0_ids']}
        self.assertEqual(covered, {'11', '12', '13'})
        self.assertFalse(plan['all_together'])
        row['covered_s0_id'] = ['11', '12']
        self.assertTrue(plan_slices(row, index)['all_together'])

    def test_csv_validation_and_exact_uint64(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cluster.csv'
            write_csv(path, [[2**64-1, '1vn', 2, f'{2**63},{2**63+1}']])
            row = read_clusters(path)[0]
            self.assertEqual(row['gt_id'], str(2**64-1))
            self.assertEqual(row['covered_s0_id'], [str(2**63), str(2**63+1)])
            for data in [[[0, '1v0', 0, '']], [[2**64, '1v0', 0, '']],
                         [[1, 'bad', 0, '']], [[1, '1v0', 1, '2']],
                         [[1, '1v1', 0, '']], [[1, '1v0', 0, ''], [1, '1v0', 0, '']]]:
                write_csv(path, data)
                with self.assertRaises(ValueError):
                    read_clusters(path)
            path.write_text('gt_id,tag,s0_count_per_gt,covered_s0_id\n1,1vn,2,2,3\n')
            with self.assertRaises(ValueError):
                read_clusters(path)

    def test_frames_preserve_coordinates_labels_and_input_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gt, s0 = make_fixture(root)
            v = Gallery(root, root / 'cluster.csv')
            v.build_index()
            self.assertEqual(v.state, 'ready')
            plan = v.plan('1')
            self.assertEqual(plan['recommended_z'], [0, 1, 2])
            self.assertEqual(plan['missing_s0'], ['999'])
            self.assertEqual(plan['disjoint_s0'], ['104'])
            self.assertEqual(plan['s0_targets']['101']['overlap_by_z'], {0: 16})
            self.assertEqual(plan['s0_targets']['104']['reason'], 'no_overlap')
            large = str(2**63 + 17)
            payload = v.frame(large, 0)
            magic, w, h, n = struct.unpack('<4sIII', payload[:16])
            meta = json.loads(payload[16:16+n])
            self.assertEqual(magic, b'GSV1')
            self.assertEqual(meta['present_s0'], [large])
            self.assertEqual(meta['s0_ids'], ['0', large])
            self.assertEqual(len(payload), 16+n+w*h*6)
            x0, y0, x1, y1 = meta['bbox_xy']
            gmask = np.frombuffer(payload, dtype=np.uint8, offset=16+n+w*h, count=w*h).reshape(h,w)
            self.assertTrue(np.array_equal(gmask, gt[0,y0:y1,x0:x1] == int(large)))
            slots = np.frombuffer(payload, dtype='<u4', offset=16+n+2*w*h).reshape(h,w)
            self.assertTrue(np.array_equal(slots == 1, s0[0,y0:y1,x0:x1] == int(large)))
            self.assertTrue(v.plan('999')['gt_missing'])
            self.assertIsNone(v.plan('999')['initial_z'])
            for z in range(5):
                v.frame('1', z)
            self.assertLessEqual(len(v.cache), 2)
            self.assertTrue(np.array_equal(np.load(root / 's0.npy'), s0))
            with h5py.File(root / 'gt.h5') as f:
                self.assertTrue(np.array_equal(f['labels'][:], gt))
            v.close()

    def test_api_pagination_filters_errors_and_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_fixture(root)
            app = make_app(root, root / 'cluster.csv')
            app.state.gallery.build_index()
            with TestClient(app) as client:
                info = client.get('/api/info').json()
                self.assertEqual(info['counts'], {'1v0': 10, '1v1': 2, '1vn': 2})
                first = client.get('/api/clusters?tag=1v0&page=1').json()
                last = client.get('/api/clusters?tag=1v0&page=2').json()
                self.assertEqual((len(first['items']), len(last['items'])), (8, 2))
                self.assertEqual({r['gt_id'] for r in first['items']} & {r['gt_id'] for r in last['items']}, set())
                self.assertEqual(first['pages'], 2)
                for url in ['/api/clusters?tag=bad', '/api/clusters?page=0', '/api/clusters?tag=1v0&page=3', '/api/frame/1/5']:
                    self.assertEqual(client.get(url).status_code, 400, url)
                self.assertEqual(client.get('/api/frame/10000/0').status_code, 404)
                self.assertEqual(client.get('/api/frame/1/0').content[:4], b'GSV1')
                focused = client.get('/api/frame/1/0?focus_s0_id=101')
                self.assertEqual(focused.status_code, 200)
                n = struct.unpack('<4sIII', focused.content[:16])[3]
                self.assertEqual(json.loads(focused.content[16:16+n])['focus']['pixel_count'], 16)
                self.assertEqual(json.loads(focused.content[16:16+n])['focus']['overlap_pixels'], 16)
                self.assertEqual(client.get('/api/frame/1/0?focus_s0_id=102').status_code, 404)
                self.assertEqual(client.get('/api/frame/1/0?focus_s0_id=12345').status_code, 400)
                for url in ['/', '/app.js', '/style.css']:
                    self.assertEqual(client.get(url).status_code, 200)

    def test_empty_category_and_wrong_volume_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_fixture(root)
            write_csv(root / 'cluster.csv', [[1, '1v0', 0, '']])
            app = make_app(root, root / 'cluster.csv')
            app.state.gallery.build_index()
            with TestClient(app) as client:
                empty = client.get('/api/clusters?tag=1vn').json()
                self.assertEqual((empty['items'], empty['total'], empty['pages']), ([], 0, 1))
            np.save(root / 's0.npy', np.zeros((2, 3, 4), dtype=np.uint16))
            with self.assertRaises(ValueError):
                Gallery(root, root / 'cluster.csv')


if __name__ == '__main__':
    unittest.main()
