#!/usr/bin/env python3
"""Read-only, paginated GT / S0 cluster gallery (ZYX volumes)."""
import argparse
from collections import OrderedDict
from contextlib import asynccontextmanager
import csv
import json
from pathlib import Path
import re
import struct
import threading

import h5py
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
import uvicorn

from palette import colour_table

HERE = Path(__file__).resolve().parent
DEFAULT_INPUTS = HERE.parents[1] / 'Proofread-data/inputs'
TAGS = ('1v0', '1v1', '1vn')
FIELDS = ('gt_id', 'tag', 's0_count_per_gt', 'covered_s0_id')
PAGE_SIZE = 8


def label_id(value):
    value = value.strip()
    if not re.fullmatch(r'[0-9]{1,20}', value) or not 0 < int(value) < 2**64:
        raise ValueError(f'Invalid nonzero uint64 label: {value!r}')
    return str(int(value))


def read_clusters(path):
    rows, seen = [], set()
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, strict=True)
        fields = reader.fieldnames or []
        if not set(FIELDS) <= set(fields) or len(fields) != len(set(fields)):
            raise ValueError('CSV requires unique columns: ' + ', '.join(FIELDS))
        for row in reader:
            try:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError('Column count mismatch; quote comma-separated covered_s0_id')
                gt = label_id(row['gt_id'])
                tag = row['tag'].strip()
                ids = list(dict.fromkeys(label_id(v) for v in row['covered_s0_id'].split(',') if v.strip()))
                count_text = row['s0_count_per_gt'].strip()
                if not re.fullmatch(r'[0-9]+', count_text):
                    raise ValueError('s0_count_per_gt must be a nonnegative integer')
                count = int(count_text)
                if tag not in TAGS or (tag == '1v0' and ids) or (tag == '1v1' and len(ids) != 1) or (tag == '1vn' and not ids):
                    raise ValueError('tag and covered_s0_id are inconsistent')
                if gt in seen:
                    raise ValueError(f'Duplicate gt_id: {gt}')
                seen.add(gt)
                rows.append(dict(gt_id=gt, tag=tag, s0_count_per_gt=count, covered_s0_id=ids))
            except ValueError as exc:
                raise ValueError(f'CSV line {reader.line_num}: {exc}') from exc
    return rows


def plan_slices(row, index):
    """All joint Zs + a greedy cover of every S0 that shares any Z with GT."""
    gg = index['gt'].get(int(row['gt_id']), {})
    ss = {s: index['s0'].get(int(s), {}) for s in row['covered_s0_id']}
    missing = [s for s, zs in ss.items() if not zs]
    disjoint = [s for s, zs in ss.items() if zs and not gg.keys() & zs.keys()]
    joint = [{'z': z, 's0_ids': [s for s, zs in ss.items() if z in zs]} for z in sorted(gg)]
    joint = [item for item in joint if item['s0_ids']]
    best_gt = max(gg, key=lambda z: (gg[z], -z)) if gg else None
    recommended, remaining = [], {s for item in joint for s in item['s0_ids']}
    while remaining:
        best = max(joint, key=lambda item: (
            len(set(item['s0_ids']) & remaining),
            sum(min(gg[item['z']], ss[s][item['z']]) for s in item['s0_ids']),
            gg[item['z']], -item['z']))
        recommended.append(best['z'])
        remaining.difference_update(best['s0_ids'])
    if not recommended and best_gt is not None:
        recommended = [best_gt]
    return dict(**row, gt_missing=not bool(gg), missing_s0=missing, disjoint_s0=disjoint,
                slices=joint if joint else [{'z': z, 's0_ids': []} for z in sorted(gg)],
                recommended_z=recommended, initial_z=recommended[0] if recommended else None,
                all_together=bool(ss) and any(len(item['s0_ids']) == len(ss) for item in joint),
                gt_slices=sorted(gg),
                s0_locations={s: max(zs, key=lambda z: (zs[z], -z)) for s, zs in ss.items() if zs})


class Gallery:
    def __init__(self, directory, cluster, em_key='inputs', gt_key='labels'):
        self.directory = Path(directory).expanduser().resolve()
        self.cluster = Path(cluster).expanduser().resolve()
        self.rows = read_clusters(self.cluster)
        self.by_id = {row['gt_id']: row for row in self.rows}
        self.em_key, self.gt_key = em_key, gt_key
        self.s0 = np.load(self.directory / 's0.npy', mmap_mode='r', allow_pickle=False)
        try:
            self.validate_volumes()
        except Exception:
            self.s0._mmap.close()
            raise
        self.index = {'gt': {}, 's0': {}}
        self.state, self.done, self.error = 'idle', 0, None
        self.index_lock, self.frame_lock = threading.Lock(), threading.Lock()
        self.index_thread = None
        self.plans = {}
        self.cache = OrderedDict()
        self.colours = {'gt': {}, 's0': {}}

    def validate_volumes(self):
        em_key, gt_key = self.em_key, self.gt_key
        with h5py.File(self.directory / 'em.h5', 'r') as em, h5py.File(self.directory / 'gt.h5', 'r') as gt:
            self.shape = tuple(em[em_key].shape)
            if len(self.shape) != 3 or not all(self.shape) or gt[gt_key].shape != self.shape or self.s0.shape != self.shape:
                raise ValueError('EM, GT and S0 must have identical nonempty 3D ZYX shapes')
            if em[em_key].dtype != np.uint8:
                raise ValueError('EM must be uint8 grayscale')
            if not np.issubdtype(gt[gt_key].dtype, np.integer) or not np.issubdtype(self.s0.dtype, np.integer):
                raise ValueError('GT and S0 must have integer labels')

    def close(self):
        if self.index_thread is not None:
            self.index_thread.join()
        self.cache.clear()
        self.s0._mmap.close()

    def build_index(self):
        try:
            wanted = {'gt': {int(r['gt_id']) for r in self.rows},
                      's0': {int(s) for r in self.rows for s in r['covered_s0_id']}}
            index = {'gt': {}, 's0': {}}
            with h5py.File(self.directory / 'gt.h5', 'r') as gt:
                for z in range(self.shape[0]):
                    for source, plane in (('gt', gt[self.gt_key][z]), ('s0', self.s0[z])):
                        ids, counts = np.unique(plane, return_counts=True)
                        if ids[0] < 0:
                            raise ValueError('Negative segmentation labels are unsupported')
                        for label, count in zip(ids, counts):
                            if int(label) in wanted[source]:
                                index[source].setdefault(int(label), {})[z] = int(count)
                    with self.index_lock:
                        self.done = z + 1
            with self.index_lock:
                self.index, self.state = index, 'ready'
        except Exception as exc:
            with self.index_lock:
                self.state, self.error = 'error', str(exc)

    def status(self):
        with self.index_lock:
            if self.state == 'idle':
                self.state = 'building'
                self.index_thread = threading.Thread(target=self.build_index, daemon=True)
                self.index_thread.start()
            return dict(status=self.state, done=self.done, total=self.shape[0], error=self.error)

    def plan(self, gt_id):
        if gt_id not in self.by_id:
            raise HTTPException(404, 'GT is not in cluster CSV')
        if gt_id not in self.plans:
            self.plans[gt_id] = plan_slices(self.by_id[gt_id], self.index)
        return self.plans[gt_id]

    def frame(self, gt_id, z, focus_s0_id=None):
        if gt_id not in self.by_id:
            raise HTTPException(404, 'GT is not in cluster CSV')
        if not 0 <= z < self.shape[0]:
            raise HTTPException(400, 'Z out of range')
        row = self.by_id[gt_id]
        if focus_s0_id is not None and focus_s0_id not in row['covered_s0_id']:
            raise HTTPException(400, 'Focused S0 must belong to this CSV row')
        with self.frame_lock:
            if z not in self.cache:
                with h5py.File(self.directory / 'em.h5', 'r') as f:
                    em = f[self.em_key][z]
                with h5py.File(self.directory / 'gt.h5', 'r') as f:
                    gt = f[self.gt_key][z]
                self.cache[z] = (em, gt)
                if len(self.cache) > 2:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(z)
            em, gt = self.cache[z]
            gmask = gt == int(gt_id)
            ids = np.array([0] + [int(s) for s in row['covered_s0_id']], dtype=np.uint64)
            # Sorted lookup avoids scanning the full plane once per S0.
            order = np.argsort(ids)
            sorted_ids = ids[order]
            pos = np.searchsorted(sorted_ids, self.s0[z])
            pos = np.minimum(pos, len(ids) - 1)
            slots = np.where(sorted_ids[pos] == self.s0[z], order[pos], 0).astype('<u4')
            focus = None
            if focus_s0_id is not None:
                # Locate in the original full plane, before any display crop.
                yy, xx = np.nonzero(self.s0[z] == int(focus_s0_id))
                if not len(xx):
                    raise HTTPException(404, 'Focused S0 is absent from this Z slice')
                focus = dict(s0_id=focus_s0_id, pixel_count=len(xx),
                             bbox_xy=[int(xx.min()), int(yy.min()), int(xx.max())+1, int(yy.max())+1])
            else:
                yy, xx = np.nonzero(gmask | (slots != 0))
            if not len(xx):
                x0, y0, x1, y1 = 0, 0, self.shape[2], self.shape[1]
            else:
                pad = max(12, int(max(np.ptp(xx) + 1, np.ptp(yy) + 1) * .12))
                x0, y0 = max(0, int(xx.min()) - pad), max(0, int(yy.min()) - pad)
                x1, y1 = min(self.shape[2], int(xx.max()) + pad + 1), min(self.shape[1], int(yy.max()) + pad + 1)
            crop = np.s_[y0:y1, x0:x1]
            slots, gmask, em = slots[crop], gmask[crop], em[crop]
            sc = colour_table(slots, ids, self.colours['s0'])
            gc = colour_table(gmask.astype(np.uint32), np.array([0, int(gt_id)], dtype=np.uint64), self.colours['gt'])[1]
            present = set(np.unique(slots).tolist()) - {0}
            meta = dict(z=z, gt_id=gt_id, gt_present=bool(gmask.any()), gt_colour=gc,
                        s0_ids=[str(i) for i in ids], s0_colours=sc,
                        present_s0=[str(ids[i]) for i in sorted(present)], bbox_xy=[x0, y0, x1, y1],
                        focus=focus)
            header = json.dumps(meta, separators=(',', ':')).encode()
            return (struct.pack('<4sIII', b'GSV1', x1-x0, y1-y0, len(header)) + header
                    + em.tobytes() + gmask.astype(np.uint8).tobytes() + slots.tobytes())


def make_app(directory, cluster, em_key='inputs', gt_key='labels'):
    gallery = Gallery(directory, cluster, em_key, gt_key)
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            gallery.close()

    app = FastAPI(title='GT Seg Viewer', docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.gallery = gallery
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)

    @app.get('/')
    def home():
        return FileResponse(HERE / 'index.html', headers={'Cache-Control': 'no-store'})

    @app.get('/app.js')
    def script():
        return FileResponse(HERE / 'app.js', media_type='text/javascript', headers={'Cache-Control': 'no-store'})

    @app.get('/style.css')
    def style():
        return FileResponse(HERE / 'style.css', media_type='text/css', headers={'Cache-Control': 'no-store'})

    @app.get('/api/info')
    def info():
        return dict(shape=gallery.shape, cluster=gallery.cluster.name, page_size=PAGE_SIZE,
                    counts={tag: sum(row['tag'] == tag for row in gallery.rows) for tag in TAGS},
                    **gallery.status())

    @app.get('/api/clusters')
    def clusters(tag: str = '1v0', page: int = 1):
        if tag not in TAGS or page < 1:
            raise HTTPException(400, 'Invalid tag or page')
        status = gallery.status()
        if status['status'] != 'ready':
            return Response(json.dumps(status), status_code=500 if status['status'] == 'error' else 202,
                            media_type='application/json')
        rows = [row for row in gallery.rows if row['tag'] == tag]
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        if page > pages:
            raise HTTPException(400, 'Page out of range')
        return dict(status='ready', tag=tag, page=page, pages=pages, total=len(rows),
                    items=[gallery.plan(row['gt_id']) for row in rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]])

    @app.get('/api/frame/{gt_id}/{z}')
    def frame(gt_id: str, z: int, focus_s0_id: str | None = None):
        return Response(gallery.frame(gt_id, z, focus_s0_id), media_type='application/octet-stream')

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=DEFAULT_INPUTS)
    parser.add_argument('--cluster', type=Path, required=True, help='GT cluster CSV')
    parser.add_argument('--em-key', default='inputs')
    parser.add_argument('--gt-key', default='labels')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18769)
    args = parser.parse_args()
    try:
        app = make_app(args.inputs, args.cluster, args.em_key, args.gt_key)
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        parser.exit(1, f'Error: {exc}\n')
    print(f'Read-only inputs: {args.inputs.resolve()}\nCluster: {args.cluster.resolve()}\nhttp://localhost:{args.port}', flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
